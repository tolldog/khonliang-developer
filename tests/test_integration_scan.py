"""Tests for the integration-point scanner (fr_developer_82fe7309).

Covers the four MVP scan sources (FR store, agent-registered skills,
bus events without subscribers, FR-body adoption-keyword mentions) plus
the distill helper's no-rescan contract and LLM-rationale cap.
"""

from __future__ import annotations

import json

import pytest
from khonliang_bus.testing import AgentTestHarness

from developer.agent import DeveloperAgent
from developer import integration_scan


@pytest.fixture
def harness(temp_config_file):
    return AgentTestHarness(DeveloperAgent, config_path=str(temp_config_file()))


# ---------------------------------------------------------------------------
# Helpers for stubbing bus-facing I/O. Each test patches only what it needs
# so the unused paths return empty (matching the real production fallback
# when the bus isn't reachable).
# ---------------------------------------------------------------------------


async def _empty_skills(self):
    return []


async def _empty_subscribers(self, topics):
    return {}


def _install_empty_bus(harness):
    """Default: no remote skills, no subscribers — exercise FR-store only."""
    harness.agent._fetch_remote_skills = _empty_skills.__get__(harness.agent)
    harness.agent._fetch_subscriber_counts = _empty_subscribers.__get__(harness.agent)


# ---------------------------------------------------------------------------
# Feature surface extraction
# ---------------------------------------------------------------------------


def test_surface_from_fr_extracts_skills_events_types():
    class StubFR:
        id = "fr_developer_abc12345"
        title = "Wire bug_opened event into triage loop"
        description = (
            "Adds a new skill wire_bug_triage that consumes bug.opened events "
            "and produces a TriageReport. See also bus.event_system."
        )
        concept = "bug triage, event-driven"
    surface = integration_scan.extract_feature_surface_from_fr(StubFR())
    assert surface.kind == "fr"
    assert surface.id == "fr_developer_abc12345"
    assert "wire_bug_triage" in surface.new_skills
    # event topic has a dot
    assert "bus.event_system" in surface.new_events or "bug.opened" in surface.new_events
    assert "TriageReport" in surface.new_types
    # concept → topic_concepts
    assert "bug triage" in surface.topic_concepts
    # tokens filter stopwords, strip "the"
    assert "triage" in surface.tokens
    assert "the" not in surface.tokens


def test_parse_pr_id_accepts_shorthand_and_url():
    assert integration_scan.parse_pr_id("tolldog/khonliang-developer#42") == (
        "tolldog", "khonliang-developer", 42,
    )
    assert integration_scan.parse_pr_id(
        "https://github.com/tolldog/khonliang-developer/pull/42"
    ) == ("tolldog", "khonliang-developer", 42)
    assert integration_scan.parse_pr_id("random-string") is None
    assert integration_scan.parse_pr_id("") is None


def test_compute_scan_id_is_stable_for_same_source_and_seed():
    src = {"kind": "fr", "id": "fr_developer_abc"}
    assert integration_scan.compute_scan_id(src, seed=1000) == integration_scan.compute_scan_id(src, seed=1000)
    # Different seed → different id.
    assert integration_scan.compute_scan_id(src, seed=1000) != integration_scan.compute_scan_id(src, seed=2000)


# ---------------------------------------------------------------------------
# FR-store scan signals (migrate + direct_replace + refactor_to_primitive)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_suggest_integration_points_fr_source_returns_candidates(harness):
    """Planted FR with heavy token overlap surfaces as a migrate candidate."""
    _install_empty_bus(harness)
    # Feature being adopted.
    feature = harness.agent.pipeline.frs.promote(
        target="developer",
        title="BugStore structured bug records",
        description=(
            "Introduce a BugStore for structured bug records, bug_triage, "
            "and bug_opened events across developer."
        ),
        priority="high",
        concept="bug tracking, structured records",
    )
    # Planted adoption site — overlaps on "bug" / "tracking" / "records".
    target = harness.agent.pipeline.frs.promote(
        target="developer",
        title="Ad-hoc bug records in dogfood",
        description=(
            "Current dogfood-triage flow tracks bug records inline. "
            "TODO: migrate to BugStore once the structured records skill lands."
        ),
        priority="medium",
        concept="bug tracking",
    )

    result = await harness.call(
        "suggest_integration_points",
        {"source": {"kind": "fr", "id": feature.id}},
    )
    assert "error" not in result
    assert result["scan_id"].startswith("scan_integration_")
    assert result["source"]["id"] == feature.id

    signals = {c["signal"] for c in result["top_candidates"]}
    target_hits = [c for c in result["top_candidates"] if c["target_id"] == target.id]
    # Planted FR must surface via at least one signal — migrate or
    # refactor_to_primitive depending on which threshold triggers first.
    assert target_hits, f"planted FR not surfaced; got {result['top_candidates']!r}"
    assert any(c["signal"] in (
        integration_scan.SIGNAL_MIGRATE,
        integration_scan.SIGNAL_REFACTOR_TO_PRIMITIVE,
    ) for c in target_hits)
    # The feature itself must never surface as its own adoption site.
    assert all(c["target_id"] != feature.id for c in result["top_candidates"])
    # At least one signal surfaced.
    assert signals


# ---------------------------------------------------------------------------
# Agent skill duplication → direct_replace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_suggest_integration_points_skill_source_finds_duplicate_agents(harness):
    _install_empty_bus(harness)

    # Stub bus skill registry: agent B exposes `bug_triage`, which is
    # also the skill we just landed on agent A. Overlap must surface.
    async def fake_skills(self):
        return [
            {
                "agent_id": "developer-secondary",
                "name": "bug_triage",
                "description": "Triage a bug report — legacy impl on secondary",
            },
            {
                "agent_id": "researcher-primary",
                "name": "classify_paper",
                "description": "Classify a paper into the taxonomy tree",
            },
        ]
    harness.agent._fetch_remote_skills = fake_skills.__get__(harness.agent)

    # Stub bus skill lookup: resolving the skill surface itself needs
    # the registry to contain the source skill too. We reuse the same
    # stub for the resolve call by adding it.
    async def fake_skills_with_source(self):
        base = await fake_skills(self)
        base.insert(0, {
            "agent_id": "developer-primary",
            "name": "bug_triage",
            "description": "Triage a bug record — new primary impl",
        })
        return base
    harness.agent._fetch_remote_skills = fake_skills_with_source.__get__(harness.agent)

    result = await harness.call(
        "suggest_integration_points",
        {"source": {"kind": "skill", "id": "developer-primary.bug_triage"}},
    )
    assert "error" not in result
    duplicates = [
        c for c in result["top_candidates"]
        if c["signal"] == integration_scan.SIGNAL_DIRECT_REPLACE
        and c["kind"] == "skill_callsite"
        and c["target_id"] == "developer-secondary.bug_triage"
    ]
    assert duplicates, f"expected direct_replace hit; got {result['top_candidates']!r}"
    assert duplicates[0]["score"] == 1.0


# ---------------------------------------------------------------------------
# Event published with no subscribers → wire_subscriber
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_suggest_integration_points_event_without_subscribers_flags_wire_subscriber(harness):
    _install_empty_bus(harness)
    # FR whose body mentions publishing a topic pattern. The extractor
    # pulls dotted.lowercase identifiers out of the description.
    fr = harness.agent.pipeline.frs.promote(
        target="developer",
        title="Publish bug.opened events from BugStore",
        description=(
            "BugStore publishes bug.opened to the bus. Any agent interested "
            "in bug triage can subscribe. No current consumer."
        ),
        priority="high",
        concept="event publishing",
    )

    async def fake_subs(self, topics):
        # Zero subscribers → wire_subscriber.
        return {t: 0 for t in topics}
    harness.agent._fetch_subscriber_counts = fake_subs.__get__(harness.agent)

    result = await harness.call(
        "suggest_integration_points",
        {"source": {"kind": "fr", "id": fr.id}},
    )
    assert "error" not in result
    wire = [
        c for c in result["top_candidates"]
        if c["signal"] == integration_scan.SIGNAL_WIRE_SUBSCRIBER
    ]
    assert wire, f"wire_subscriber missing from {result['top_candidates']!r}"
    assert any(c["target_id"] == "bug.opened" for c in wire)


# ---------------------------------------------------------------------------
# LLM rationale cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_suggest_integration_points_caps_llm_calls_to_top_n(harness, monkeypatch):
    _install_empty_bus(harness)
    # Plant enough candidates to exceed the 20-call cap — 30 FRs that
    # all share heavy token overlap with the feature.
    feature = harness.agent.pipeline.frs.promote(
        target="developer",
        title="Tracking primitive for dogfood friction",
        description="Tracking primitive for dogfood friction records and events.",
        priority="high",
        concept="tracking friction dogfood",
    )
    for i in range(30):
        harness.agent.pipeline.frs.promote(
            target="developer",
            title=f"Dogfood friction tracker {i}",
            description=(
                f"Dogfood friction tracker #{i} — tracking primitive for "
                "friction records and events within developer workflow."
            ),
            priority="low",
            concept="tracking friction dogfood",
        )

    call_count = {"n": 0}

    async def fake_rationale(candidate, surface):
        call_count["n"] += 1
        return f"rationale-{call_count['n']}"

    # Install the optional LLM rationale hook.
    harness.agent._llm_rationale = fake_rationale

    # Override MAX_LLM_RATIONALES to a smaller cap so the test runs fast
    # and the assertion is tight.
    monkeypatch.setattr(integration_scan, "MAX_LLM_RATIONALES", 5)

    result = await harness.call(
        "suggest_integration_points",
        {"source": {"kind": "fr", "id": feature.id}, "top_n": 50},
    )
    assert "error" not in result
    # Call count must be ≤ the cap even though many more candidates exist.
    assert call_count["n"] <= 5
    assert result["llm_rationale_calls"] == call_count["n"]
    assert result["total_candidates"] >= 10


# ---------------------------------------------------------------------------
# Distill helper — no rescan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_distill_integration_points_filters_without_rescan(harness):
    _install_empty_bus(harness)
    feature = harness.agent.pipeline.frs.promote(
        target="developer",
        title="Integration scanner primitive",
        description="Integration scanner primitive for developer workflow.",
        priority="high",
        concept="integration scanning",
    )
    # Plant a couple of weakly-matching FRs so the scan produces output.
    for i in range(3):
        harness.agent.pipeline.frs.promote(
            target="developer",
            title=f"Adopt integration scanner {i}",
            description=(
                f"Adopt the integration scanner primitive #{i} in developer. "
                "TODO: follow-up once it lands."
            ),
            priority="medium",
            concept="integration scanning",
        )

    scan = await harness.call(
        "suggest_integration_points",
        {"source": {"kind": "fr", "id": feature.id}},
    )
    assert "error" not in scan
    scan_id = scan["scan_id"]
    assert scan["total_candidates"] >= 1

    # Ensure rescan does NOT happen — rebind the FR-store list so any
    # new scan would blow up.
    def _boom(*a, **kw):
        raise AssertionError("distill must not rescan")
    harness.agent.pipeline.frs.list = _boom

    distilled = await harness.call(
        "distill_integration_points",
        {"scan_id": scan_id, "top_n": 100},
    )
    assert distilled["scan_id"] == scan_id
    assert distilled["from_artifact"] is True
    assert distilled["total_candidates"] == scan["total_candidates"]

    # Filtered distill with a specific signal preserves the id and only
    # keeps matching-signal rows.
    filtered = await harness.call(
        "distill_integration_points",
        {
            "scan_id": scan_id,
            "signal": integration_scan.SIGNAL_MIGRATE,
        },
    )
    assert filtered["scan_id"] == scan_id
    assert all(
        c["signal"] == integration_scan.SIGNAL_MIGRATE
        for c in filtered["top_candidates"]
    )


# ---------------------------------------------------------------------------
# PR source
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_suggest_integration_points_handles_pr_source(harness):
    _install_empty_bus(harness)

    # Plant an FR that overlaps with the PR title so there's at least
    # one candidate surfaced from the PR surface extraction.
    harness.agent.pipeline.frs.promote(
        target="developer",
        title="Introduce BugStore primitive",
        description="BugStore primitive for structured bug records.",
        priority="high",
        concept="bug tracking",
    )

    class FakeGH:
        async def get_pr(self, repo, pr_number):
            assert repo == "tolldog/khonliang-developer"
            assert pr_number == 99
            return {
                "number": 99,
                "title": "feat(developer): BugStore primitive for bug records",
                "state": "closed",
                "merged": True,
                "merged_at": "2026-04-22T00:00:00+00:00",
                "body": "Introduces BugStore primitive.",
            }

    harness.agent._github_client = lambda: FakeGH()

    result = await harness.call(
        "suggest_integration_points",
        {"source": {"kind": "pr", "id": "tolldog/khonliang-developer#99"}},
    )
    assert "error" not in result
    assert result["source"]["kind"] == "pr"
    assert result["source"]["surface"]["title"].startswith("feat(developer)")
    # At least the planted FR surfaces via token overlap.
    assert result["total_candidates"] >= 1


@pytest.mark.asyncio
async def test_suggest_integration_points_rejects_unmerged_pr(harness):
    _install_empty_bus(harness)

    class FakeGH:
        async def get_pr(self, repo, pr_number):
            return {
                "number": 99, "title": "WIP", "state": "open",
                "merged": False, "merged_at": None, "body": "",
            }
    harness.agent._github_client = lambda: FakeGH()

    result = await harness.call(
        "suggest_integration_points",
        {"source": {"kind": "pr", "id": "tolldog/khonliang-developer#99"}},
    )
    assert "error" in result
    assert "not merged" in result["error"]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_suggest_integration_points_rejects_bad_source(harness):
    _install_empty_bus(harness)
    # Missing source
    result = await harness.call("suggest_integration_points", {})
    assert "error" in result

    # Wrong kind
    result = await harness.call(
        "suggest_integration_points",
        {"source": {"kind": "repository", "id": "foo"}},
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_suggest_integration_points_rejects_missing_fr(harness):
    _install_empty_bus(harness)
    result = await harness.call(
        "suggest_integration_points",
        {"source": {"kind": "fr", "id": "fr_developer_nonexistent"}},
    )
    assert "error" in result
    assert "not found" in result["error"]


@pytest.mark.asyncio
async def test_distill_integration_points_errors_on_missing_scan(harness):
    _install_empty_bus(harness)
    result = await harness.call(
        "distill_integration_points",
        {"scan_id": "scan_integration_deadbeef"},
    )
    assert "error" in result
    assert "not found" in result["error"]


# ---------------------------------------------------------------------------
# Copilot R1 — word-boundary keyword matching
# ---------------------------------------------------------------------------


def test_scan_fr_store_word_boundary_rejects_substring_matches():
    """_ADOPT_KEYWORDS must match whole words only — "adopt" must not
    register on "adoptable", "todo" must not register on "todolist".

    Regression for Copilot R1 finding #1: the docstring documents
    whole-word matching but the original implementation used substring
    containment, generating false positives.
    """
    class StubFR:
        def __init__(self, id_, title, description, concept=""):
            self.id = id_
            self.title = title
            self.description = description
            self.concept = concept

    # Surface has strong token overlap so the "shared tokens" gate passes
    # for both the false-positive FR and the true-positive FR. This makes
    # the assertion isolate the keyword-matching change specifically.
    surface = integration_scan.FeatureSurface(
        kind="fr", id="fr_developer_source",
        title="Primitive for structured adoption records",
        description="Adoption primitive for records.",
        tokens={"adoption", "primitive", "records", "structured"},
    )

    # FRs whose bodies contain substrings of the keywords only. "adoptable"
    # contains "adopt" as a substring; "todolist" contains "todo"; the FR
    # must NOT surface under refactor_to_primitive.
    substring_only = StubFR(
        id_="fr_developer_substr",
        title="Adoptable records todolist",
        description=(
            "Adoptable records here; todolist is already managed. No "
            "migration needed."
        ),
        concept="adoption records",
    )
    # FR with a real whole-word keyword hit — "TODO: adopt" — must surface.
    real_hit = StubFR(
        id_="fr_developer_real",
        title="Real keyword hit",
        description=(
            "TODO: adopt the new primitive for these records once it lands."
        ),
        concept="adoption records",
    )

    candidates = integration_scan.scan_fr_store(
        surface, [substring_only, real_hit],
    )
    by_signal = [
        (c.target_id, c.signal) for c in candidates
    ]
    # The substring-only FR must not surface under refactor_to_primitive.
    assert ("fr_developer_substr", integration_scan.SIGNAL_REFACTOR_TO_PRIMITIVE) not in by_signal
    # The real keyword hit must still surface.
    assert ("fr_developer_real", integration_scan.SIGNAL_REFACTOR_TO_PRIMITIVE) in by_signal


# ---------------------------------------------------------------------------
# Copilot R1 — to_full preserves unrounded score
# ---------------------------------------------------------------------------


def test_candidate_full_preserves_unrounded_score():
    """to_full() must preserve full precision; to_brief() still rounds.

    Regression for Copilot R1 finding #2: distill rehydrates from the
    stored full projection and re-ranks. Rounding at persist time
    corrupts close-score orderings.
    """
    c = integration_scan.IntegrationCandidate(
        kind="fr", target_id="fr_developer_xyz",
        signal=integration_scan.SIGNAL_MIGRATE,
        score=0.123456789,
        rationale="x",
        metadata={"body_sim": 0.321},
    )
    brief = c.to_brief()
    full = c.to_full()
    assert brief["score"] == 0.123  # rounded
    assert full["score"] == 0.123456789  # unrounded
    # Full still carries metadata.
    assert full["metadata"] == {"body_sim": 0.321}


@pytest.mark.asyncio
async def test_distill_reranks_with_unrounded_scores(harness):
    """Close-score ordering survives persist + rehydrate.

    Directly inject two candidates whose scores differ only past the third
    decimal, persist + rehydrate via the distill path, and assert the
    stable ordering is preserved.
    """
    _install_empty_bus(harness)

    # Build a synthetic scan artifact with two candidates whose scores
    # differ in the 4th decimal. Under the pre-fix behaviour both stored
    # to 0.500 and the tiebreaker on target_id alphabetised them; under
    # the fix the higher-precision score wins.
    from khonliang.knowledge.store import (
        EntryStatus, KnowledgeEntry, Tier,
    )
    import json as _json

    hi = integration_scan.IntegrationCandidate(
        kind="fr", target_id="fr_developer_zzz_higher",
        signal=integration_scan.SIGNAL_MIGRATE,
        score=0.50049,
        rationale="hi",
    )
    lo = integration_scan.IntegrationCandidate(
        kind="fr", target_id="fr_developer_aaa_lower",
        signal=integration_scan.SIGNAL_MIGRATE,
        score=0.50001,
        rationale="lo",
    )
    scan_id = "scan_integration_test01"
    payload = {
        "source": {"kind": "fr", "id": "fr_developer_synth"},
        "surface": {},
        "candidates": [hi.to_full(), lo.to_full()],
        "audience": "",
        "generated_at": 0,
    }
    entry = KnowledgeEntry(
        id=scan_id,
        tier=Tier.DERIVED,
        title="synth",
        content=_json.dumps(payload),
        source="developer.integration_scan",
        scope="development",
        confidence=1.0,
        status=EntryStatus.DISTILLED,
        tags=["scan_integration", "kind:fr"],
        metadata={},
    )
    harness.agent.pipeline.knowledge.add(entry)

    distilled = await harness.call(
        "distill_integration_points",
        {"scan_id": scan_id, "top_n": 10},
    )
    assert "error" not in distilled
    ordered = [c["target_id"] for c in distilled["top_candidates"]]
    # Higher unrounded score wins despite alphabetic tiebreak being against it.
    assert ordered.index("fr_developer_zzz_higher") < ordered.index("fr_developer_aaa_lower")


# ---------------------------------------------------------------------------
# Copilot R1 — multi-signal per FR with dedupe only at (target_id, signal)
# ---------------------------------------------------------------------------


def test_scan_fr_store_emits_multiple_signals_per_fr():
    """A single FR that matches direct_replace AND refactor_to_primitive
    must surface under both signals; dedupe happens only at the
    (target_id, signal) tuple level.

    Regression for Copilot R1 finding #3: an early ``continue`` in the
    direct_replace branch prevented downstream signals from being
    evaluated on the same FR.
    """
    class StubFR:
        id = "fr_developer_both"
        title = "BugStore primitive"
        description = (
            "BugStore primitive — TODO: adopt the new primitive for the "
            "structured records workflow."
        )
        concept = "bug tracking"

    # Surface title matches the FR title verbatim → direct_replace fires.
    # Surface also shares tokens + FR body contains "TODO" + "adopt" → the
    # refactor_to_primitive path must ALSO fire on the same FR.
    surface = integration_scan.FeatureSurface(
        kind="fr", id="fr_developer_source",
        title="BugStore primitive",
        description="BugStore primitive for structured bug records.",
        tokens={"bugstore", "primitive", "structured", "records"},
    )

    candidates = integration_scan.scan_fr_store(surface, [StubFR()])
    signals_for_fr = {
        c.signal for c in candidates if c.target_id == "fr_developer_both"
    }
    assert integration_scan.SIGNAL_DIRECT_REPLACE in signals_for_fr
    assert integration_scan.SIGNAL_REFACTOR_TO_PRIMITIVE in signals_for_fr

    # Dedupe still holds: calling scan_fr_store again on a single-FR input
    # must not produce duplicate (target_id, signal) pairs.
    keys = [(c.target_id, c.signal) for c in candidates]
    assert len(keys) == len(set(keys))


# ---------------------------------------------------------------------------
# Copilot R1 — compact projection is distinct from brief
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_suggest_integration_points_compact_projection_is_distinct(harness):
    """``detail='compact'`` must return something distinct from ``brief``.

    Regression for Copilot R1 finding #4: the skill schema advertised
    brief/compact/full but the projection only distinguished full vs
    not-full, silently collapsing compact into brief.
    """
    _install_empty_bus(harness)
    feature = harness.agent.pipeline.frs.promote(
        target="developer",
        title="Compact projection test primitive",
        description="Compact projection test primitive for structured records.",
        priority="high",
        concept="compact testing",
    )
    harness.agent.pipeline.frs.promote(
        target="developer",
        title="Adopt compact projection primitive",
        description=(
            "Existing workflow — TODO: adopt the compact projection "
            "primitive once it lands. Tracks structured records."
        ),
        priority="medium",
        concept="compact testing",
    )

    brief = await harness.call(
        "suggest_integration_points",
        {"source": {"kind": "fr", "id": feature.id}, "detail": "brief"},
    )
    compact = await harness.call(
        "suggest_integration_points",
        {"source": {"kind": "fr", "id": feature.id}, "detail": "compact"},
    )
    full = await harness.call(
        "suggest_integration_points",
        {"source": {"kind": "fr", "id": feature.id}, "detail": "full"},
    )

    assert brief["top_candidates"], "test needs at least one candidate to compare projections"
    b = brief["top_candidates"][0]
    c = compact["top_candidates"][0]
    f = full["top_candidates"][0]

    # brief lacks metadata entirely.
    assert "metadata" not in b
    # full carries full metadata.
    assert "metadata" in f
    assert isinstance(f["metadata"], dict)

    # compact is distinct from brief AND from full. If the candidate has
    # any compact-eligible metadata fields, compact must carry them; and
    # compact must remain leaner than full (fewer or equal metadata keys).
    # Any candidate surfacing from FR-store scan carries "status" and
    # "target" in its metadata, both of which are in the compact set.
    assert "metadata" in c, f"compact projection must include lean metadata; got {c!r}"
    assert c != b, "compact projection must differ from brief"
    # compact metadata is a subset of full metadata.
    assert set(c["metadata"].keys()).issubset(set(f["metadata"].keys()))
    assert len(c["metadata"]) <= len(f["metadata"])


# ---------------------------------------------------------------------------
# Copilot R1 — scan_id nanosecond resolution
# ---------------------------------------------------------------------------


def test_compute_scan_id_same_second_distinct_sources_unique():
    """Two scans of distinct sources in the same nanosecond seed collide
    only if their source payloads match — unchanged behaviour.

    The primary fix is about same-source same-second collisions (see the
    next test); this one asserts the ``source`` dict still participates
    in the hash so changing it changes the id.
    """
    src1 = {"kind": "fr", "id": "fr_developer_abc"}
    src2 = {"kind": "fr", "id": "fr_developer_xyz"}
    seed = 1_700_000_000_000_000_000
    assert integration_scan.compute_scan_id(src1, seed=seed) != integration_scan.compute_scan_id(src2, seed=seed)


def test_compute_scan_id_same_source_distinct_seconds_unique(monkeypatch):
    """Two scans of the same source with nanosecond-adjacent seeds produce
    distinct ids — catches the pre-fix int(epoch) same-second collision.

    Regression for Copilot R1 finding #5: two scans of the same FR within
    the same wall-clock second used to hash to the same ``scan_id`` and
    overwrite each other's KnowledgeEntry. ``time.time_ns()`` + integer
    seeding avoids the collision.

    The default-seeded branch monkeypatches ``time.time_ns`` to a counter
    so the assertion that "distinct ns seeds → distinct ids" is exercised
    deterministically. Earlier the test called ``time.time_ns()`` three
    times and asserted the results diverged on wall-clock resolution —
    flaky on low-resolution platforms where three back-to-back calls can
    return the same value.
    """
    src = {"kind": "fr", "id": "fr_developer_abc"}
    # Two seeds 1 ns apart — would collide under int(epoch) second-resolution.
    seed_a = 1_700_000_000_000_000_000
    seed_b = 1_700_000_000_000_000_001
    assert integration_scan.compute_scan_id(src, seed=seed_a) != integration_scan.compute_scan_id(src, seed=seed_b)

    # Default-seeded (no explicit seed) calls also differ across nanosecond
    # boundaries. Inject a deterministic counter into ``time.time_ns`` so
    # the regression assertion isn't sensitive to host-clock resolution.
    counter = iter([
        1_700_000_000_000_000_000,
        1_700_000_000_000_000_001,
        1_700_000_000_000_000_002,
    ])
    monkeypatch.setattr(integration_scan.time, "time_ns", lambda: next(counter))
    id1 = integration_scan.compute_scan_id(src)
    id2 = integration_scan.compute_scan_id(src)
    id3 = integration_scan.compute_scan_id(src)
    # All three seeds are distinct → all three ids must be distinct
    # (hash collisions on 8-hex prefixes aren't feasible at this scale).
    assert len({id1, id2, id3}) == 3


# ---------------------------------------------------------------------------
# Copilot R2 — _resolve_feature_surface wraps bus / network errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_feature_surface_skill_source_wraps_bus_error_as_lookup_error(
    harness,
):
    """Bus / network failures while fetching the skill registry surface
    as ``LookupError`` from ``_resolve_feature_surface``, matching the
    docstring contract. The handler's ``except (ValueError, LookupError)``
    then converts that into a clean error-dict rather than a 500-equivalent
    unhandled-exception unwind.

    Regression for Copilot R2 finding #1: ``_fetch_remote_skills`` raises
    ``httpx.HTTPError`` / ``RuntimeError`` shapes that escape the caller's
    narrow exception set.
    """
    # Patch _fetch_remote_skills to raise a representative bus error.
    async def _boom(self):
        raise RuntimeError("bus unreachable — connection refused")
    harness.agent._fetch_remote_skills = _boom.__get__(harness.agent)

    with pytest.raises(LookupError) as exc_info:
        await harness.agent._resolve_feature_surface(
            "skill", "developer-primary.something",
        )
    assert "registry unavailable" in str(exc_info.value)
    assert "developer-primary.something" in str(exc_info.value)

    # End-to-end: the handler surfaces the LookupError as an error-dict
    # with no unhandled exception escaping.
    result = await harness.call(
        "suggest_integration_points",
        {"source": {"kind": "skill", "id": "developer-primary.something"}},
    )
    assert "error" in result
    assert "registry unavailable" in result["error"]


# ---------------------------------------------------------------------------
# Copilot R2 — distill_integration_points handles malformed payloads
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_distill_integration_points_handles_malformed_payload(harness):
    """Older / hand-edited / truncated KnowledgeEntry artifacts must not
    crash ``distill_integration_points``. Three failure modes are covered:

    * payload is not a dict (e.g. a bare list)
    * payload['candidates'] is not a list (e.g. a string)
    * individual candidates in the list are not dicts (e.g. None)

    Regression for Copilot R2 finding #2: the handler assumed the stored
    shape verbatim and threw ``AttributeError`` on malformed rows.
    """
    from khonliang.knowledge.store import EntryStatus, KnowledgeEntry, Tier

    _install_empty_bus(harness)

    def _put_artifact(scan_id: str, content: str) -> None:
        entry = KnowledgeEntry(
            id=scan_id,
            tier=Tier.DERIVED,
            title=f"Malformed artifact {scan_id}",
            content=content,
            source="developer.integration_scan",
            scope="development",
            confidence=1.0,
            status=EntryStatus.DISTILLED,
            tags=["scan_integration"],
            metadata={},
        )
        harness.agent.pipeline.knowledge.add(entry)

    # 1. payload is a list, not a dict.
    _put_artifact("scan_integration_bad1", json.dumps(["not", "a", "dict"]))
    r1 = await harness.call(
        "distill_integration_points",
        {"scan_id": "scan_integration_bad1"},
    )
    assert "error" in r1
    assert "not a dict" in r1["error"] or "expected dict" in r1["error"]

    # 2. payload is a dict but candidates is not a list.
    _put_artifact(
        "scan_integration_bad2",
        json.dumps({"source": {"kind": "fr", "id": "x"}, "candidates": "oops"}),
    )
    r2 = await harness.call(
        "distill_integration_points",
        {"scan_id": "scan_integration_bad2"},
    )
    assert "error" in r2
    assert "candidates" in r2["error"]

    # 3. Mixed valid + invalid candidate entries — invalid ones skipped,
    # valid ones survive. Artifact must successfully distil into a
    # non-error response with the good rows intact.
    _put_artifact(
        "scan_integration_bad3",
        json.dumps({
            "source": {"kind": "fr", "id": "fr_developer_abc"},
            "surface": {"kind": "fr", "id": "fr_developer_abc"},
            "candidates": [
                None,
                "also-not-a-dict",
                {
                    "kind": "fr",
                    "target_id": "fr_developer_xyz",
                    "signal": integration_scan.SIGNAL_MIGRATE,
                    "score": 0.7,
                    "rationale": "valid",
                    "metadata": {"status": "open"},
                },
            ],
            "audience": "",
        }),
    )
    r3 = await harness.call(
        "distill_integration_points",
        {"scan_id": "scan_integration_bad3"},
    )
    assert "error" not in r3, f"valid rows should survive: {r3!r}"
    assert r3["total_candidates"] == 1
    assert r3["top_candidates"][0]["target_id"] == "fr_developer_xyz"


# ---------------------------------------------------------------------------
# Copilot R2 — migrate rationale reports full overlap, not truncated
# ---------------------------------------------------------------------------


def test_scan_fr_store_migrate_rationale_reports_full_overlap_count():
    """The migrate-signal rationale must reflect the FULL intersection
    size, not the truncated preview slice. Pre-fix, two FRs with 9 and
    20 shared tokens both read as "overlaps on 8 tokens" because the
    count was taken after the ``[:8]`` slice.

    Regression for Copilot R2 finding #3: separate preview from count.
    """
    class StubFR:
        def __init__(self, id_, title, description):
            self.id = id_
            self.title = title
            self.description = description
            self.concept = ""

    # Build a surface with many tokens; FR shares >> 8 of them.
    many_shared = {
        "alpha", "bravo", "charlie", "delta", "echo",
        "foxtrot", "golf", "hotel", "india", "juliet",
        "kilo", "lima",
    }
    surface = integration_scan.FeatureSurface(
        kind="fr", id="fr_developer_source",
        title="Token overlap test",
        description="Token overlap test.",
        tokens=many_shared | {"source_only"},
    )
    fr = StubFR(
        id_="fr_developer_dup",
        title="Alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima extra",
        description="Alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima.",
    )

    candidates = integration_scan.scan_fr_store(surface, [fr])
    migrate = [c for c in candidates if c.signal == integration_scan.SIGNAL_MIGRATE]
    assert migrate, "test expects a migrate candidate"
    cand = migrate[0]
    # Rationale reports the full 12-token overlap, not "8".
    assert "12 tokens" in cand.rationale, cand.rationale
    # Preview slice in metadata is still capped at 8.
    assert len(cand.metadata["shared_tokens"]) == 8
    # Full count lives in its own field.
    assert cand.metadata["shared_token_count"] == 12
