"""Tests for the integration-point scanner (fr_developer_82fe7309).

Covers the four MVP scan sources (FR store, agent-registered skills,
bus events without subscribers, FR-body adoption-keyword mentions) plus
the distill helper's no-rescan contract and LLM-rationale cap.
"""

from __future__ import annotations

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
