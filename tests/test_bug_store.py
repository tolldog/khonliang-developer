"""Tests for developer's BugStore (Phase 1 — CRUD-only)."""

from __future__ import annotations

import asyncio

import pytest

from khonliang.knowledge.store import KnowledgeStore

from developer.bug_store import (
    BUG_SEVERITY_HIGH,
    BUG_SEVERITY_LOW,
    BUG_SEVERITY_MEDIUM,
    BUG_STATUS_DUPLICATE,
    BUG_STATUS_FIXED,
    BUG_STATUS_IN_PROGRESS,
    BUG_STATUS_OPEN,
    BUG_STATUS_TRIAGED,
    BUG_STATUS_WONTFIX,
    BugError,
    BugStore,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    """Empty BugStore with no seed data (for focused unit tests)."""
    knowledge = KnowledgeStore(str(tmp_path / "test.db"))
    return BugStore(knowledge=knowledge, seed=False)


@pytest.fixture
def seeded_store(tmp_path):
    """BugStore with default seed data loaded."""
    knowledge = KnowledgeStore(str(tmp_path / "seeded.db"))
    return BugStore(knowledge=knowledge)  # seed=True by default


# ---------------------------------------------------------------------------
# file_bug / get_bug
# ---------------------------------------------------------------------------


def test_file_bug_creates_open(store):
    bug = store.file_bug(
        target="researcher",
        title="Something broke",
        description="long description",
        reproduction="steps",
        observed_entity="researcher/fetcher.py",
        severity=BUG_SEVERITY_HIGH,
        reporter="user",
    )
    assert bug.status == BUG_STATUS_OPEN
    assert bug.severity == BUG_SEVERITY_HIGH
    assert bug.target == "researcher"
    assert bug.id.startswith("bug_researcher_")
    assert bug.observed_at > 0
    assert bug.linked_frs == []
    assert bug.linked_pr == ""
    assert bug.duplicate_of == ""
    # source field defaults to None per fr_developer_47271f34 forward-compat
    assert bug.source is None
    assert len(bug.notes_history) == 1


def test_file_bug_roundtrip_through_get(store):
    bug = store.file_bug(
        target="developer",
        title="T",
        description="d",
        reporter="user",
    )
    out = store.get_bug(bug.id)
    assert out is not None
    assert out.id == bug.id
    assert out.title == "T"
    assert out.reporter == "user"


def test_bug_observed_at_epoch_zero_roundtrips(store):
    bug = store.file_bug(
        target="developer",
        title="T",
        description="d",
        reporter="user",
        observed_at=0.0,
    )
    assert bug.observed_at == 0.0
    out = store.get_bug(bug.id)
    assert out is not None
    assert out.observed_at == 0.0, (
        "epoch-0 observed_at must survive round-trip; "
        "prior impl used `or` which treats 0.0 as falsy"
    )


def test_file_bug_rejects_missing_fields(store):
    with pytest.raises(BugError, match="non-empty"):
        store.file_bug(target="", title="t", description="d")
    with pytest.raises(BugError, match="non-empty"):
        store.file_bug(target="x", title="", description="d")


def test_file_bug_rejects_empty_description(store):
    """``description`` feeds into ``_derive_bug_id``, so an empty or
    whitespace-only description would produce a distinct id from the same
    conceptual bug filed with any real description — poisoning dedup.
    Must be rejected the same as target/title.
    """
    with pytest.raises(BugError, match="non-empty"):
        store.file_bug(target="developer", title="t", description="")
    with pytest.raises(BugError, match="non-empty"):
        store.file_bug(target="developer", title="t", description="   ")


def test_file_bug_rejects_invalid_severity(store):
    with pytest.raises(BugError, match="severity"):
        store.file_bug(target="developer", title="t", description="d",
                       severity="urgent")


def test_file_bug_deterministic_id_collision(store):
    first = store.file_bug(target="developer", title="T", description="d",
                           observed_entity="x")
    with pytest.raises(BugError, match="already exists"):
        store.file_bug(target="developer", title="T", description="d",
                       observed_entity="x")
    # Different observed_entity → different id
    other = store.file_bug(
        target="developer", title="T", description="d", observed_entity="y"
    )
    assert other.id != first.id


def test_file_bug_rejects_collision_with_non_bug_entry(store):
    """A non-bug KnowledgeEntry at the derived bug id must not be silently
    overwritten. Prior impl only raised on bug-tagged collisions; any other
    pre-existing entry (corrupted data or an unrelated entry type reusing the
    id) would be clobbered by ``knowledge.add()``. Enforce refusal.
    """
    from khonliang.knowledge.store import KnowledgeEntry, Tier
    from developer.bug_store import _derive_bug_id

    target = "developer"
    title = "T"
    description = "d"
    observed_entity = "x"
    bug_id = _derive_bug_id(target, title, description, observed_entity)

    # Seed a KnowledgeEntry at the same id but with unrelated tags.
    store.knowledge.add(KnowledgeEntry(
        id=bug_id,
        tier=Tier.DERIVED,
        title="pre-existing non-bug entry",
        content="some other content",
        tags=["fr", "unrelated"],
    ))

    with pytest.raises(BugError, match="id collision with non-bug entry"):
        store.file_bug(
            target=target,
            title=title,
            description=description,
            observed_entity=observed_entity,
        )

    # Pre-existing entry must be untouched.
    survivor = store.knowledge.get(bug_id)
    assert survivor is not None
    assert survivor.title == "pre-existing non-bug entry"
    assert "bug" not in (survivor.tags or [])


def test_get_returns_none_for_unknown_id(store):
    assert store.get_bug("bug_developer_00000000") is None


def test_source_field_accepts_attribution_struct(store):
    """Phase 2's GH-issue ingest will populate source; Phase 1 accepts the shape."""
    src = {
        "kind": "github_issue",
        "url": "https://github.com/x/y/issues/1",
        "repo": "x/y",
        "number": 1,
        "author": "alice",
        "labels": ["bug"],
        "created_at": 0.0,
        "issue_title": "…",
        "body_hash": "abcdef",
    }
    bug = store.file_bug(
        target="developer", title="T", description="d",
        source=src,
    )
    # The keyword-only `source` arg isn't yet exposed via the MCP skill
    # (Phase 2 ingest will wire it); we test it's structurally accepted
    # and round-trips through storage.
    round_tripped = store.get_bug(bug.id)
    assert round_tripped.source == src


# ---------------------------------------------------------------------------
# list_bugs — default filter, severity, status, target
# ---------------------------------------------------------------------------


def test_list_excludes_terminal_by_default(store):
    open_bug = store.file_bug(target="developer", title="Open", description="d")
    closed = store.file_bug(target="developer", title="Closed", description="d")
    store.close_bug(closed.id, resolution=BUG_STATUS_FIXED)

    ids = [b.id for b in store.list_bugs()]
    assert open_bug.id in ids
    assert closed.id not in ids


def test_list_status_all_includes_terminal(store):
    a = store.file_bug(target="developer", title="A", description="d")
    b = store.file_bug(target="developer", title="B", description="d")
    store.close_bug(b.id, resolution=BUG_STATUS_FIXED)
    ids = [x.id for x in store.list_bugs(status="all")]
    assert a.id in ids
    assert b.id in ids


def test_list_filter_by_target(store):
    a = store.file_bug(target="researcher", title="A", description="d")
    b = store.file_bug(target="developer", title="B", description="d")
    ids = [x.id for x in store.list_bugs(target="researcher")]
    assert a.id in ids
    assert b.id not in ids


def test_list_filter_by_status_name(store):
    a = store.file_bug(target="developer", title="A", description="d")
    b = store.file_bug(target="developer", title="B", description="d")
    store.update_bug_status(b.id, BUG_STATUS_TRIAGED)
    triaged = [x.id for x in store.list_bugs(status="triaged")]
    assert triaged == [b.id]


def test_list_filter_by_severity_min_keeps_high_and_above(store):
    low = store.file_bug(target="developer", title="Low", description="d",
                         severity=BUG_SEVERITY_LOW, observed_entity="a")
    med = store.file_bug(target="developer", title="Med", description="d",
                         severity=BUG_SEVERITY_MEDIUM, observed_entity="b")
    high = store.file_bug(target="developer", title="High", description="d",
                          severity=BUG_SEVERITY_HIGH, observed_entity="c")
    kept = {x.id for x in store.list_bugs(severity_min="high")}
    assert kept == {high.id}
    kept_med = {x.id for x in store.list_bugs(severity_min="medium")}
    assert kept_med == {med.id, high.id}


def test_list_bugs_raises_on_unknown_severity_min(store):
    """Unknown severity_min must raise rather than silently ignore the filter."""
    store.file_bug(target="developer", title="L", description="d",
                   severity=BUG_SEVERITY_LOW, observed_entity="a")
    with pytest.raises(BugError, match="severity_min"):
        store.list_bugs(severity_min="urgent")
    # Empty string is the valid "no filter" signal and must NOT raise.
    assert len(store.list_bugs(severity_min="")) >= 1


def test_list_ordering_newest_first(store):
    import time as _t
    a = store.file_bug(target="developer", title="A", description="d",
                       observed_entity="a")
    _t.sleep(0.01)
    b = store.file_bug(target="developer", title="B", description="d",
                       observed_entity="b")
    ids = [x.id for x in store.list_bugs()]
    assert ids.index(b.id) < ids.index(a.id)


# ---------------------------------------------------------------------------
# update_status / lifecycle
# ---------------------------------------------------------------------------


def test_update_status_advances_lifecycle(store):
    bug = store.file_bug(target="developer", title="U", description="d")
    triaged = store.update_bug_status(bug.id, BUG_STATUS_TRIAGED, notes="t")
    assert triaged.status == BUG_STATUS_TRIAGED
    in_prog = store.update_bug_status(bug.id, BUG_STATUS_IN_PROGRESS, notes="claimed")
    assert in_prog.status == BUG_STATUS_IN_PROGRESS
    assert len(in_prog.notes_history) == 3


def test_update_status_idempotent_without_notes(store):
    bug = store.file_bug(target="developer", title="I", description="d")
    store.update_bug_status(bug.id, BUG_STATUS_TRIAGED)
    before = store.get_bug(bug.id)
    again = store.update_bug_status(bug.id, BUG_STATUS_TRIAGED)
    assert again.status == BUG_STATUS_TRIAGED
    assert len(again.notes_history) == len(before.notes_history)


def test_update_status_rejects_invalid_status_name(store):
    bug = store.file_bug(target="developer", title="X", description="d")
    with pytest.raises(BugError, match="status must be one of"):
        store.update_bug_status(bug.id, "bogus")


def test_update_status_rejects_on_terminal(store):
    bug = store.file_bug(target="developer", title="X", description="d")
    store.close_bug(bug.id, resolution=BUG_STATUS_FIXED)
    with pytest.raises(BugError, match="terminal"):
        store.update_bug_status(bug.id, BUG_STATUS_IN_PROGRESS)


def test_update_bug_status_rejects_any_mutation_on_terminal_bug(store):
    """Terminal bugs are fully immutable via update_bug_status.

    Covers the R2 finding: previously ``bug.status != status`` let callers
    pass ``status=<same terminal>`` + ``notes=...`` to append to
    ``notes_history`` and bump ``updated_at``. Terminal means terminal —
    no same-status note appends, no ``updated_at`` bumps, no history
    growth. Duplicates still route via ``mark_duplicate``.
    """
    bug = store.file_bug(target="developer", title="T", description="d")
    store.close_bug(bug.id, resolution=BUG_STATUS_FIXED)

    before = store.get_bug(bug.id)
    assert before.status == BUG_STATUS_FIXED
    history_len_before = len(before.notes_history)
    updated_at_before = before.updated_at

    # Same-status "update" with notes must be refused.
    with pytest.raises(BugError, match="terminal"):
        store.update_bug_status(bug.id, BUG_STATUS_FIXED, notes="sneak note")

    # Different terminal target also refused (unchanged behavior).
    with pytest.raises(BugError, match="terminal"):
        store.update_bug_status(bug.id, BUG_STATUS_WONTFIX, notes="flip")

    # Non-terminal target still refused (unchanged behavior).
    with pytest.raises(BugError, match="terminal"):
        store.update_bug_status(bug.id, BUG_STATUS_TRIAGED)

    # Verify no mutation leaked through any of the rejected calls.
    after = store.get_bug(bug.id)
    assert after.status == BUG_STATUS_FIXED
    assert len(after.notes_history) == history_len_before
    assert after.updated_at == updated_at_before
    # Ensure the attempted note text never landed in history.
    assert all("sneak note" not in (n.get("notes") or "") for n in after.notes_history)


def test_update_status_rejects_unknown_id(store):
    with pytest.raises(BugError, match="unknown"):
        store.update_bug_status("bug_developer_missing", BUG_STATUS_TRIAGED)


def test_update_bug_status_rejects_duplicate_transition(store):
    """Duplicate transitions require a ``duplicate_of`` pointer — force callers
    onto ``mark_duplicate`` rather than let them file half-formed records."""
    bug = store.file_bug(target="developer", title="D", description="d")
    with pytest.raises(BugError, match="mark_duplicate"):
        store.update_bug_status(bug.id, BUG_STATUS_DUPLICATE)
    # The bug must not have been mutated by the rejected call.
    still = store.get_bug(bug.id)
    assert still.status == BUG_STATUS_OPEN
    assert still.duplicate_of == ""


# ---------------------------------------------------------------------------
# link_bug_pr
# ---------------------------------------------------------------------------


def test_link_bug_pr_records_url(store):
    bug = store.file_bug(target="developer", title="L", description="d")
    updated = store.link_bug_pr(bug.id,
                                "https://github.com/foo/bar/pull/42")
    assert updated.linked_pr == "https://github.com/foo/bar/pull/42"


def test_link_bug_pr_rejects_empty_url(store):
    bug = store.file_bug(target="developer", title="L", description="d")
    with pytest.raises(BugError, match="pr_url"):
        store.link_bug_pr(bug.id, "")


def test_link_bug_pr_rejects_terminal(store):
    bug = store.file_bug(target="developer", title="L", description="d")
    store.close_bug(bug.id, resolution=BUG_STATUS_FIXED)
    with pytest.raises(BugError, match="terminal"):
        store.link_bug_pr(bug.id, "https://x/y/pull/1")


# ---------------------------------------------------------------------------
# close_bug
# ---------------------------------------------------------------------------


def test_close_bug_fixed(store):
    bug = store.file_bug(target="developer", title="C", description="d")
    closed = store.close_bug(bug.id, resolution=BUG_STATUS_FIXED)
    assert closed.status == BUG_STATUS_FIXED


def test_close_bug_wontfix(store):
    bug = store.file_bug(target="developer", title="Cw", description="d")
    closed = store.close_bug(bug.id, resolution=BUG_STATUS_WONTFIX)
    assert closed.status == BUG_STATUS_WONTFIX


def test_close_bug_rejects_invalid_resolution(store):
    bug = store.file_bug(target="developer", title="C2", description="d")
    with pytest.raises(BugError, match="fixed|wontfix"):
        store.close_bug(bug.id, resolution="bogus")


def test_close_bug_rejects_duplicate_via_close(store):
    """close_bug routes callers to mark_duplicate for that case."""
    bug = store.file_bug(target="developer", title="C3", description="d")
    with pytest.raises(BugError, match="mark_duplicate"):
        store.close_bug(bug.id, resolution=BUG_STATUS_DUPLICATE)


def test_close_bug_rejects_already_terminal(store):
    bug = store.file_bug(target="developer", title="C4", description="d")
    store.close_bug(bug.id, resolution=BUG_STATUS_FIXED)
    with pytest.raises(BugError, match="terminal"):
        store.close_bug(bug.id, resolution=BUG_STATUS_WONTFIX)


# ---------------------------------------------------------------------------
# mark_duplicate
# ---------------------------------------------------------------------------


def test_mark_duplicate_sets_status_and_pointer(store):
    a = store.file_bug(target="developer", title="A", description="d",
                       observed_entity="a")
    b = store.file_bug(target="developer", title="B", description="d",
                       observed_entity="b")
    marked = store.mark_duplicate(b.id, a.id)
    assert marked.status == BUG_STATUS_DUPLICATE
    assert marked.duplicate_of == a.id


def test_mark_duplicate_rejects_self(store):
    a = store.file_bug(target="developer", title="A", description="d")
    with pytest.raises(BugError, match="itself"):
        store.mark_duplicate(a.id, a.id)


def test_mark_duplicate_rejects_unknown_target(store):
    a = store.file_bug(target="developer", title="A", description="d")
    with pytest.raises(BugError, match="duplicate target"):
        store.mark_duplicate(a.id, "bug_developer_missing")


def test_mark_duplicate_rejects_terminal(store):
    a = store.file_bug(target="developer", title="A", description="d",
                       observed_entity="a")
    b = store.file_bug(target="developer", title="B", description="d",
                       observed_entity="b")
    store.close_bug(b.id, resolution=BUG_STATUS_FIXED)
    with pytest.raises(BugError, match="terminal"):
        store.mark_duplicate(b.id, a.id)


# ---------------------------------------------------------------------------
# Seed data idempotence
# ---------------------------------------------------------------------------


def test_seed_data_present_on_first_init(tmp_path):
    """Fresh store gets the two curated seed entries."""
    knowledge = KnowledgeStore(str(tmp_path / "seeded.db"))
    store = BugStore(knowledge=knowledge)
    bugs = store.list_bugs(status="all")
    # Two seed bugs — distiller RL-mis-tag (low) + Substack 403 (medium)
    assert len(bugs) == 2
    titles = sorted(b.title for b in bugs)
    # Loose match on distinguishing substrings (verbatim strings are tested
    # below via the initial content check).
    assert any("Distiller" in t for t in titles)
    assert any("Substack" in t or "fetch_paper" in t for t in titles)
    # Both seed bugs target researcher and were reported by the user.
    assert all(b.target == "researcher" for b in bugs)
    assert all(b.reporter == "user" for b in bugs)


def test_seed_data_idempotent_on_second_init(tmp_path):
    """Constructing a second BugStore against the same DB doesn't duplicate."""
    db_path = str(tmp_path / "idempotent.db")
    knowledge_a = KnowledgeStore(db_path)
    BugStore(knowledge=knowledge_a)  # seeds two
    first_count = len(BugStore(knowledge=knowledge_a, seed=False).list_bugs(status="all"))

    # Fresh instance against the same DB (simulating a restart).
    knowledge_b = KnowledgeStore(db_path)
    BugStore(knowledge=knowledge_b)  # would-seed but shouldn't
    second_count = len(BugStore(knowledge=knowledge_b, seed=False).list_bugs(status="all"))

    assert first_count == 2
    assert second_count == 2


def test_seed_data_skipped_when_store_already_has_rows(tmp_path):
    """If user-filed bugs exist before a seeded init, seeds don't add more."""
    db_path = str(tmp_path / "prepopulated.db")
    knowledge = KnowledgeStore(db_path)
    store_no_seed = BugStore(knowledge=knowledge, seed=False)
    store_no_seed.file_bug(target="developer", title="User bug", description="d")

    # Now construct with seed=True — should not add the 2 seeds since the
    # store already has >0 bug rows.
    knowledge2 = KnowledgeStore(db_path)
    store_seeded = BugStore(knowledge=knowledge2)
    assert len(store_seeded.list_bugs(status="all")) == 1


# ---------------------------------------------------------------------------
# MCP skill wiring (pipeline-level happy path per skill)
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def _make_agent(pipeline):
    """Construct a DeveloperAgent backed by an injected pipeline.

    BaseAgent.__init__ requires agent_id / bus_url / config_path, but we
    never connect to the bus — we only call handler coroutines directly.
    """
    from developer.agent import DeveloperAgent
    agent = DeveloperAgent(
        agent_id="test-developer",
        bus_url="http://localhost:8787",
        config_path="unused",
    )
    agent._pipeline = pipeline
    return agent


def test_mcp_file_bug_happy_path(pipeline):
    agent = _make_agent(pipeline)
    result = _run(agent.handle_file_bug({
        "target": "developer",
        "title": "MCP filed",
        "description": "via skill",
        "severity": "high",
        "reporter": "user",
    }))
    assert "bug_id" in result
    assert result["severity"] == "high"
    assert result["status"] == BUG_STATUS_OPEN


def test_mcp_list_bugs_happy_path(pipeline):
    agent = _make_agent(pipeline)
    pipeline.bugs.file_bug(target="developer", title="Listed", description="d")
    result = _run(agent.handle_list_bugs({
        "target": "developer",
        "detail": "brief",
    }))
    assert "bugs" in result
    assert result["count"] >= 1
    assert all("severity" not in b for b in result["bugs"])  # brief mode


def test_mcp_get_bug_happy_path(pipeline):
    agent = _make_agent(pipeline)
    bug = pipeline.bugs.file_bug(target="developer", title="Get me", description="d")
    result = _run(agent.handle_get_bug({"bug_id": bug.id, "detail": "full"}))
    assert result["id"] == bug.id
    assert result["target"] == "developer"


def test_mcp_update_bug_status_happy_path(pipeline):
    agent = _make_agent(pipeline)
    bug = pipeline.bugs.file_bug(target="developer", title="U", description="d")
    result = _run(agent.handle_update_bug_status({
        "bug_id": bug.id,
        "status": BUG_STATUS_TRIAGED,
        "notes": "seen",
    }))
    assert result["status"] == BUG_STATUS_TRIAGED


def test_mcp_link_bug_pr_happy_path(pipeline):
    agent = _make_agent(pipeline)
    bug = pipeline.bugs.file_bug(target="developer", title="LP", description="d")
    result = _run(agent.handle_link_bug_pr({
        "bug_id": bug.id,
        "pr_url": "https://github.com/x/y/pull/1",
    }))
    assert result["linked_pr"] == "https://github.com/x/y/pull/1"


def test_mcp_close_bug_happy_path(pipeline):
    agent = _make_agent(pipeline)
    bug = pipeline.bugs.file_bug(target="developer", title="CB", description="d")
    result = _run(agent.handle_close_bug({
        "bug_id": bug.id,
        "resolution": BUG_STATUS_FIXED,
    }))
    assert result["status"] == BUG_STATUS_FIXED


# ---------------------------------------------------------------------------
# Phase 2A: escalate_to_fr / update_severity / triage handlers
# ---------------------------------------------------------------------------


def test_escalate_to_fr_appends_to_linked_frs(store):
    bug = store.file_bug(target="developer", title="T", description="d")
    out = store.escalate_to_fr(bug.id, "fr_developer_abc12345")
    assert out.linked_frs == ["fr_developer_abc12345"]
    # notes_history records the escalation with caller-facing action text
    assert any("escalated to fr_developer_abc12345" in entry["notes"]
               for entry in out.notes_history)


def test_escalate_to_fr_is_idempotent(store):
    """Re-linking the same pair is a no-op — no duplicate, no audit noise."""
    bug = store.file_bug(target="developer", title="T", description="d")
    store.escalate_to_fr(bug.id, "fr_x_1")
    notes_before = len(store.get_bug(bug.id).notes_history)
    out = store.escalate_to_fr(bug.id, "fr_x_1")
    assert out.linked_frs == ["fr_x_1"]
    assert len(out.notes_history) == notes_before


def test_escalate_to_fr_supports_multiple_distinct_frs(store):
    bug = store.file_bug(target="developer", title="T", description="d")
    store.escalate_to_fr(bug.id, "fr_x_1")
    out = store.escalate_to_fr(bug.id, "fr_x_2")
    assert out.linked_frs == ["fr_x_1", "fr_x_2"]


def test_escalate_to_fr_refuses_terminal(store):
    bug = store.file_bug(target="developer", title="T", description="d")
    store.close_bug(bug.id, "fixed")
    with pytest.raises(BugError, match="terminal"):
        store.escalate_to_fr(bug.id, "fr_x_1")


def test_escalate_to_fr_requires_non_empty_fr_id(store):
    bug = store.file_bug(target="developer", title="T", description="d")
    with pytest.raises(BugError, match="non-empty"):
        store.escalate_to_fr(bug.id, "")


def test_update_severity_happy_path(store):
    bug = store.file_bug(
        target="developer", title="T", description="d",
        severity=BUG_SEVERITY_LOW,
    )
    out = store.update_severity(bug.id, BUG_SEVERITY_HIGH)
    assert out.severity == BUG_SEVERITY_HIGH
    assert any("severity low->high" in e["notes"] for e in out.notes_history)


def test_update_severity_no_change_is_noop_unless_noted(store):
    bug = store.file_bug(
        target="developer", title="T", description="d",
        severity=BUG_SEVERITY_MEDIUM,
    )
    before = len(store.get_bug(bug.id).notes_history)
    out = store.update_severity(bug.id, BUG_SEVERITY_MEDIUM)
    assert len(out.notes_history) == before


def test_update_severity_refuses_terminal(store):
    bug = store.file_bug(target="developer", title="T", description="d")
    store.close_bug(bug.id, "wontfix")
    with pytest.raises(BugError, match="terminal"):
        store.update_severity(bug.id, BUG_SEVERITY_HIGH)


# -- triage_bug handler --


def test_triage_bug_escalate_round_trip(pipeline):
    """Bug filed → triaged → FR created → bug.linked_frs populated → FR description references bug."""
    agent = _make_agent(pipeline)
    filed = _run(agent.handle_file_bug({
        "target": "developer",
        "title": "escalation title",
        "description": "body of the bug",
        "reproduction": "steps A then B",
        "severity": "high",
    }))
    result = _run(agent.handle_triage_bug({
        "bug_id": filed["bug_id"],
        "status": BUG_STATUS_TRIAGED,
        "escalate_to_fr": True,
    }))
    assert result["bug_id"] == filed["bug_id"]
    assert result["escalated_fr_id"].startswith("fr_developer_")
    assert result["escalation_reused"] is False
    assert result["linked_frs"] == [result["escalated_fr_id"]]

    fr = pipeline.frs.get(result["escalated_fr_id"])
    assert fr is not None
    # FR description seeded from the bug — must reference the bug id
    # so provenance survives a reader of the FR alone.
    assert filed["bug_id"] in fr.description
    assert "body of the bug" in fr.description


def test_triage_bug_idempotent_re_escalation(pipeline):
    """Second triage_bug(escalate_to_fr=True) returns existing, doesn't double-create."""
    agent = _make_agent(pipeline)
    filed = _run(agent.handle_file_bug({
        "target": "developer",
        "title": "idem",
        "description": "body",
    }))
    first = _run(agent.handle_triage_bug({
        "bug_id": filed["bug_id"],
        "escalate_to_fr": True,
    }))
    # All developer FRs before the second call
    frs_before = len(pipeline.frs.list(include_all=True))
    second = _run(agent.handle_triage_bug({
        "bug_id": filed["bug_id"],
        "escalate_to_fr": True,
    }))
    assert second["escalated_fr_id"] == first["escalated_fr_id"]
    assert second["escalation_reused"] is True
    assert len(pipeline.frs.list(include_all=True)) == frs_before


def test_triage_bug_rejects_terminal_status_with_escalate(pipeline):
    """status=<terminal> + escalate_to_fr=true must be refused up-front.

    Without this guard the handler would close the bug first (making it
    terminal), then call escalate_to_fr which refuses terminal bugs —
    but the FR has already been created, leaving an orphan FR and an
    inconsistent state. The up-front rejection must keep the FR count
    unchanged AND leave the bug status un-mutated.
    """
    agent = _make_agent(pipeline)
    filed = _run(agent.handle_file_bug({
        "target": "developer",
        "title": "terminal race",
        "description": "would orphan an FR",
    }))
    frs_before = len(pipeline.frs.list(include_all=True))
    status_before = pipeline.bugs.get_bug(filed["bug_id"]).status

    for terminal in (BUG_STATUS_FIXED, BUG_STATUS_WONTFIX):
        result = _run(agent.handle_triage_bug({
            "bug_id": filed["bug_id"],
            "status": terminal,
            "escalate_to_fr": True,
        }))
        assert "error" in result
        assert "escalate" in result["error"].lower()
        assert "terminal" in result["error"].lower()

    # No FR was created, and the bug's status was not advanced to the
    # rejected terminal target.
    assert len(pipeline.frs.list(include_all=True)) == frs_before
    assert pipeline.bugs.get_bug(filed["bug_id"]).status == status_before


def test_triage_bug_rejects_escalate_when_bug_already_terminal(pipeline):
    """escalate_to_fr=True on an already-terminal bug must be refused up-front.

    Covers the gap left by the R1 fix: when status="" (no status change
    requested) and the bug is ALREADY in a terminal status with no
    linked_frs, promote() would create the FR but
    BugStore.escalate_to_fr() refuses terminal bugs — leaving an orphan
    FR. Up-front rejection keeps the FR count unchanged.
    """
    agent = _make_agent(pipeline)
    filed = _run(agent.handle_file_bug({
        "target": "developer",
        "title": "already closed",
        "description": "this bug was closed before escalation was tried",
    }))
    # Close the bug first (two-step legit flow).
    _run(agent.handle_update_bug_status({
        "bug_id": filed["bug_id"],
        "status": BUG_STATUS_FIXED,
    }))
    frs_before = len(pipeline.frs.list(include_all=True))

    result = _run(agent.handle_triage_bug({
        "bug_id": filed["bug_id"],
        "escalate_to_fr": True,
        # status intentionally omitted — the R1 guard only trips when
        # status is set to a terminal value. This path must ALSO be
        # rejected when the bug is already terminal.
    }))
    assert "error" in result
    assert "terminal" in result["error"].lower()
    # No orphan FR created — count unchanged.
    assert len(pipeline.frs.list(include_all=True)) == frs_before
    # Bug still terminal (unchanged by the rejected call).
    assert pipeline.bugs.get_bug(filed["bug_id"]).status == BUG_STATUS_FIXED
    # linked_frs still empty — no stray attachment.
    assert pipeline.bugs.get_bug(filed["bug_id"]).linked_frs == []


def test_triage_bug_without_escalation_updates_severity_and_status(pipeline):
    agent = _make_agent(pipeline)
    filed = _run(agent.handle_file_bug({
        "target": "developer",
        "title": "raw",
        "description": "body",
        "severity": "low",
    }))
    result = _run(agent.handle_triage_bug({
        "bug_id": filed["bug_id"],
        "severity": "high",
        "status": BUG_STATUS_TRIAGED,
        "notes": "raised after repro",
    }))
    assert result["severity"] == "high"
    assert result["status"] == BUG_STATUS_TRIAGED
    assert result["linked_frs"] == []
    # No escalation key when escalate_to_fr is false
    assert "escalated_fr_id" not in result


def test_triage_bug_unknown_id_returns_error(pipeline):
    agent = _make_agent(pipeline)
    result = _run(agent.handle_triage_bug({"bug_id": "bug_nope_12345678"}))
    assert "error" in result


# -- link_bug_fr handler --


def test_link_bug_fr_manual_attach(pipeline):
    agent = _make_agent(pipeline)
    filed = _run(agent.handle_file_bug({
        "target": "developer",
        "title": "manual link",
        "description": "body",
    }))
    fr = pipeline.frs.promote(
        target="developer", title="Existing FR", description="pre-existing"
    )
    result = _run(agent.handle_link_bug_fr({
        "bug_id": filed["bug_id"],
        "fr_id": fr.id,
    }))
    assert result["linked_frs"] == [fr.id]


def test_link_bug_fr_idempotent_reattach(pipeline):
    agent = _make_agent(pipeline)
    filed = _run(agent.handle_file_bug({
        "target": "developer",
        "title": "t",
        "description": "body",
    }))
    fr = pipeline.frs.promote(
        target="developer", title="FR A", description="d"
    )
    _run(agent.handle_link_bug_fr({"bug_id": filed["bug_id"], "fr_id": fr.id}))
    _run(agent.handle_link_bug_fr({"bug_id": filed["bug_id"], "fr_id": fr.id}))
    bug = pipeline.bugs.get_bug(filed["bug_id"])
    assert bug.linked_frs == [fr.id]


def test_link_bug_fr_rejects_unknown_fr(pipeline):
    agent = _make_agent(pipeline)
    filed = _run(agent.handle_file_bug({
        "target": "developer",
        "title": "t",
        "description": "body",
    }))
    result = _run(agent.handle_link_bug_fr({
        "bug_id": filed["bug_id"],
        "fr_id": "fr_developer_deadbeef",
    }))
    assert "error" in result


# -- report_gap(bug=True) --


def test_report_gap_bug_creates_bug_and_fires_event(pipeline):
    """report_gap(bug=True) files a bug in BugStore AND returns telemetry ack.

    Since the test harness runs the agent without a live bus, the
    telemetry side falls through to ``gap.not_sent`` in the ack — but
    the bug filing is unconditional and must succeed.
    """
    agent = _make_agent(pipeline)
    result = _run(agent.handle_report_gap({
        "operation": "promote_fr",
        "reason": "backing_papers parse failure",
        "bug": True,
        "severity": "high",
    }))
    assert "bug_id" in result
    bug = pipeline.bugs.get_bug(result["bug_id"])
    assert bug is not None
    assert bug.severity == "high"
    assert "promote_fr" in bug.title
    # event key present regardless of bus connectivity (gap.observed
    # when connected, gap.not_sent when not). The test_harness doesn't
    # connect, so gap.not_sent is expected.
    assert result["event"] in ("gap.observed", "gap.not_sent")


def test_report_gap_bug_false_is_legacy_behavior(pipeline):
    """Default bug=False does NOT file a bug — Phase 1 callers unaffected."""
    agent = _make_agent(pipeline)
    before = len(pipeline.bugs.list_bugs(status="all"))
    result = _run(agent.handle_report_gap({
        "operation": "run_tests",
        "reason": "timeout",
    }))
    after = len(pipeline.bugs.list_bugs(status="all"))
    assert "bug_id" not in result
    assert after == before


def test_report_gap_requires_operation_and_reason(pipeline):
    agent = _make_agent(pipeline)
    result = _run(agent.handle_report_gap({"operation": "", "reason": ""}))
    assert "error" in result


def test_report_gap_bug_propagates_cancellation(pipeline):
    """asyncio.CancelledError must NOT be swallowed by the bug-filing except.

    Same async-cancellation pattern as PR #39 R4 for pr_watcher:
    the broad ``except Exception`` around BugStore.file_bug must
    re-raise CancelledError so the enclosing async task can terminate.
    If the bug path catches CancelledError (because it's a subclass of
    BaseException in 3.8+ but of Exception in earlier versions, OR
    because someone accidentally re-orders the clauses), cancellation
    propagation breaks.
    """
    import asyncio

    agent = _make_agent(pipeline)

    # Monkeypatch BugStore.file_bug to raise CancelledError — this
    # simulates what happens if the caller's enclosing task is
    # cancelled while file_bug is awaiting (e.g. on a DB lock via
    # future work). The report_gap handler must propagate, not
    # swallow into bug_error.
    def _raise_cancelled(*args, **kwargs):
        raise asyncio.CancelledError()

    pipeline.bugs.file_bug = _raise_cancelled  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError):
        _run(agent.report_gap(
            operation="cancel_probe",
            reason="task was cancelled mid-file_bug",
            bug=True,
            severity="high",
        ))




# ---------------------------------------------------------------------------
# Phase 3 of fr_developer_5d0a8711 — project dimension
# ---------------------------------------------------------------------------


def test_bug_project_defaults_to_khonliang(store):
    from developer.project_store import DEFAULT_PROJECT
    bug = store.file_bug(
        target="developer", title="t", description="d", observed_entity="x",
    )
    assert bug.project == DEFAULT_PROJECT
    assert store.knowledge.get(bug.id).metadata["project"] == DEFAULT_PROJECT


def test_bug_project_passes_through_file_bug(store):
    bug = store.file_bug(
        target="developer", title="t", description="d",
        observed_entity="x", project="genealogy",
    )
    assert bug.project == "genealogy"
    assert store.knowledge.get(bug.id).metadata["project"] == "genealogy"


def test_bug_list_filters_by_project(store):
    a = store.file_bug(
        target="developer", title="A", description="a",
        observed_entity="x", project="alpha",
    )
    b = store.file_bug(
        target="developer", title="B", description="b",
        observed_entity="y", project="beta",
    )
    alpha_only = store.list_bugs(project="alpha")
    ids = {bug.id for bug in alpha_only}
    assert a.id in ids
    assert b.id not in ids


def test_bug_migrate_records_to_project_is_idempotent(store):
    from developer.project_store import DEFAULT_PROJECT
    from khonliang.knowledge.store import EntryStatus, KnowledgeEntry, Tier
    import time
    now = time.time()
    store.knowledge.add(KnowledgeEntry(
        id="bug_developer_legacy01",
        tier=Tier.DERIVED,
        title="Legacy Bug",
        content="body",
        source="developer.bug_store",
        scope="development",
        confidence=1.0,
        status=EntryStatus.DISTILLED,
        tags=["bug", "target:developer", "severity:medium"],
        metadata={
            "bug_status": "open",
            "severity": "medium",
            "target": "developer",
        },
        created_at=now, updated_at=now,
    ))
    assert store.migrate_records_to_project(DEFAULT_PROJECT) == 1
    assert store.migrate_records_to_project(DEFAULT_PROJECT) == 0
    raw = store.knowledge.get("bug_developer_legacy01")
    assert raw.metadata["project"] == DEFAULT_PROJECT


def test_bug_migrate_preserves_unknown_metadata_keys(store):
    from developer.project_store import DEFAULT_PROJECT
    from khonliang.knowledge.store import EntryStatus, KnowledgeEntry, Tier
    import time
    now = time.time()
    store.knowledge.add(KnowledgeEntry(
        id="bug_developer_extrakeys",
        tier=Tier.DERIVED,
        title="has extras",
        content="body",
        source="developer.bug_store",
        scope="development",
        confidence=1.0,
        status=EntryStatus.DISTILLED,
        tags=["bug", "target:developer", "severity:medium", "custom:legacy"],
        metadata={
            "bug_status": "open",
            "severity": "medium",
            "target": "developer",
            "legacy_extra": "keep_it",
        },
        created_at=now, updated_at=now,
    ))
    assert store.migrate_records_to_project(DEFAULT_PROJECT) == 1
    raw = store.knowledge.get("bug_developer_extrakeys")
    assert raw.metadata["project"] == DEFAULT_PROJECT
    assert raw.metadata["legacy_extra"] == "keep_it"
    assert "custom:legacy" in raw.tags


def test_bug_legacy_record_reads_as_default_project(store):
    from developer.project_store import DEFAULT_PROJECT
    from khonliang.knowledge.store import EntryStatus, KnowledgeEntry, Tier
    import time
    now = time.time()
    store.knowledge.add(KnowledgeEntry(
        id="bug_developer_legacyread",
        tier=Tier.DERIVED,
        title="legacy bug",
        content="body",
        source="developer.bug_store",
        scope="development",
        confidence=1.0,
        status=EntryStatus.DISTILLED,
        tags=["bug", "target:developer", "severity:medium"],
        metadata={
            "bug_status": "open",
            "severity": "medium",
            "target": "developer",
        },
        created_at=now, updated_at=now,
    ))
    bug = store.get_bug("bug_developer_legacyread")
    assert bug is not None
    assert bug.project == DEFAULT_PROJECT
    assert any(b.id == "bug_developer_legacyread" and b.project == DEFAULT_PROJECT
               for b in store.list_bugs())


def test_list_bugs_empty_string_project_filters_for_default(store):
    from developer.project_store import DEFAULT_PROJECT
    default_bug = store.file_bug(
        target="developer", title="A", description="d1", observed_entity="e1",
    )
    other = store.file_bug(
        target="developer", title="B", description="d2",
        observed_entity="e2", project="alpha",
    )
    filtered = store.list_bugs(project="")
    ids = {b.id for b in filtered}
    assert default_bug.id in ids
    assert other.id not in ids
