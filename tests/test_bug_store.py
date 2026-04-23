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


