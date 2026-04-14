"""Tests for developer's FRStore.

Focused on the merge-aware read/write framework (PR 1 scope). The actual
``merge_frs`` write operation lands in a follow-up PR; these tests seed
merge state manually to exercise the read-side redirect logic before the
write-side exists.
"""

from __future__ import annotations

import time

import pytest

from khonliang.knowledge.store import (
    EntryStatus,
    KnowledgeEntry,
    KnowledgeStore,
    Tier,
)

from developer.fr_store import (
    ACTIVE_STATUSES,
    ALL_STATUSES,
    FR_STATUS_ARCHIVED,
    FR_STATUS_COMPLETED,
    FR_STATUS_IN_PROGRESS,
    FR_STATUS_MERGED,
    FR_STATUS_OPEN,
    FR_STATUS_PLANNED,
    FR,
    FRError,
    FRStore,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    knowledge = KnowledgeStore(str(tmp_path / "test.db"))
    return FRStore(knowledge=knowledge)


def _seed_merged_fr(store: FRStore, *, source_id: str, target_id: str, role: str = "") -> None:
    """Manually write a record in `merged` status with merged_into set.

    Simulates state that will be produced by merge_frs (follow-up PR).
    Tests the read-side redirect without depending on the write-side.
    """
    knowledge = store.knowledge
    now = time.time()
    entry = KnowledgeEntry(
        id=source_id,
        tier=Tier.DERIVED,
        title="source",
        content="source description",
        source="test",
        scope="development",
        tags=["fr", "target:developer", "app"],
        status=EntryStatus.DISTILLED,
        metadata={
            "fr_status": FR_STATUS_MERGED,
            "priority": "medium",
            "target": "developer",
            "classification": "app",
            "merged_into": target_id,
            "merge_role": role,
        },
        created_at=now,
        updated_at=now,
    )
    knowledge.add(entry)


# ---------------------------------------------------------------------------
# promote
# ---------------------------------------------------------------------------


def test_promote_creates_fr_in_open_state(store):
    fr = store.promote(
        target="developer",
        title="Test feature",
        description="Does a thing",
        priority="high",
        concept="test concept",
    )
    assert fr.status == FR_STATUS_OPEN
    assert fr.target == "developer"
    assert fr.priority == "high"
    assert fr.id.startswith("fr_developer_")
    assert len(fr.notes_history) == 1
    assert fr.notes_history[0]["status"] == FR_STATUS_OPEN


def test_promote_rejects_missing_fields(store):
    with pytest.raises(FRError, match="non-empty"):
        store.promote(target="", title="t", description="d")
    with pytest.raises(FRError, match="non-empty"):
        store.promote(target="x", title="", description="d")


def test_promote_rejects_invalid_priority(store):
    with pytest.raises(FRError, match="priority"):
        store.promote(target="developer", title="t", description="d", priority="urgent")


def test_promote_is_deterministic_on_id(store):
    """Same (target, title, concept) → same id. Re-promote errors cleanly."""
    fr1 = store.promote(
        target="developer", title="Test", description="d1", concept="c"
    )
    with pytest.raises(FRError, match="already exists"):
        store.promote(
            target="developer", title="Test", description="different", concept="c"
        )
    # And different concept produces different id
    fr2 = store.promote(
        target="developer", title="Test", description="d2", concept="other"
    )
    assert fr1.id != fr2.id


# ---------------------------------------------------------------------------
# get + merge redirect
# ---------------------------------------------------------------------------


def test_get_returns_fr(store):
    fr = store.promote(target="developer", title="X", description="x")
    out = store.get(fr.id)
    assert out is not None
    assert out.id == fr.id
    assert out.redirected_from is None


def test_get_returns_none_for_unknown_id(store):
    assert store.get("fr_developer_00000000") is None


def test_get_follows_merge_redirect(store):
    target = store.promote(target="developer", title="Merged", description="d")
    _seed_merged_fr(store, source_id="fr_developer_sourced", target_id=target.id, role="original concept")
    out = store.get("fr_developer_sourced")
    assert out is not None
    assert out.id == target.id
    assert out.redirected_from == "fr_developer_sourced"


def test_get_with_follow_redirect_false_returns_raw(store):
    target = store.promote(target="developer", title="Merged2", description="d")
    _seed_merged_fr(store, source_id="fr_developer_raw", target_id=target.id)
    out = store.get("fr_developer_raw", follow_redirect=False)
    assert out is not None
    assert out.id == "fr_developer_raw"
    assert out.status == FR_STATUS_MERGED
    assert out.merged_into == target.id
    assert out.redirected_from is None


def test_get_handles_chain_of_redirects(store):
    """A → B → C should resolve A to C."""
    c = store.promote(target="developer", title="C", description="d")
    _seed_merged_fr(store, source_id="fr_developer_b", target_id=c.id)
    _seed_merged_fr(store, source_id="fr_developer_a", target_id="fr_developer_b")
    out = store.get("fr_developer_a")
    assert out is not None
    assert out.id == c.id
    assert out.redirected_from == "fr_developer_a"


def test_get_detects_redirect_cycle(store):
    """A → B → A should raise FRError."""
    _seed_merged_fr(store, source_id="fr_developer_a", target_id="fr_developer_b")
    _seed_merged_fr(store, source_id="fr_developer_b", target_id="fr_developer_a")
    with pytest.raises(FRError, match="cycle"):
        store.get("fr_developer_a")


def test_get_tolerates_dangling_pointer_single_hop(store):
    """A → missing_id: A is the last well-formed FR; return it with hint."""
    _seed_merged_fr(store, source_id="fr_developer_a", target_id="fr_developer_gone")
    out = store.get("fr_developer_a")
    assert out is not None
    # A itself is the last well-formed FR in the chain — its pointer is dangling
    assert out.id == "fr_developer_a"
    assert out.redirected_from == "fr_developer_a"


def test_get_tolerates_dangling_pointer_multi_hop(store):
    """A → B → missing: B is the last well-formed FR; return it, not A."""
    _seed_merged_fr(store, source_id="fr_developer_b", target_id="fr_developer_gone")
    _seed_merged_fr(store, source_id="fr_developer_a", target_id="fr_developer_b")
    out = store.get("fr_developer_a")
    assert out is not None
    # B is the last well-formed FR we reached, not A
    assert out.id == "fr_developer_b"
    assert out.redirected_from == "fr_developer_a"


def test_get_tolerates_merged_record_with_empty_merged_into(store):
    """status=merged with no merged_into pointer is partially-formed; return it as terminal."""
    # Manually write a merged record with empty merged_into
    import time as _t
    from khonliang.knowledge.store import KnowledgeEntry, Tier, EntryStatus
    now = _t.time()
    store.knowledge.add(KnowledgeEntry(
        id="fr_developer_broken",
        tier=Tier.DERIVED,
        title="broken",
        content="d",
        source="test",
        scope="development",
        tags=["fr", "target:developer", "app"],
        status=EntryStatus.DISTILLED,
        metadata={
            "fr_status": FR_STATUS_MERGED,
            "priority": "medium",
            "target": "developer",
            "classification": "app",
            "merged_into": "",   # missing/empty
        },
        created_at=now, updated_at=now,
    ))
    # Should NOT raise exceeded-depth; should return the broken record itself
    # as the terminal-with-hint.
    out = store.get("fr_developer_broken")
    assert out is not None
    assert out.id == "fr_developer_broken"
    assert out.redirected_from == "fr_developer_broken"


# ---------------------------------------------------------------------------
# resolve_id
# ---------------------------------------------------------------------------


def test_resolve_id_returns_same_for_non_merged(store):
    fr = store.promote(target="developer", title="R", description="d")
    assert store.resolve_id(fr.id) == fr.id


def test_resolve_id_walks_chain(store):
    c = store.promote(target="developer", title="C2", description="d")
    _seed_merged_fr(store, source_id="fr_developer_bb", target_id=c.id)
    _seed_merged_fr(store, source_id="fr_developer_aa", target_id="fr_developer_bb")
    assert store.resolve_id("fr_developer_aa") == c.id


def test_resolve_id_returns_input_for_missing_id(store):
    assert store.resolve_id("fr_developer_zzzzzzzz") == "fr_developer_zzzzzzzz"


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_filters_terminal_by_default(store):
    a = store.promote(target="developer", title="Active", description="d")
    b = store.promote(target="developer", title="ToArchive", description="d")
    store.update_status(b.id, FR_STATUS_ARCHIVED, notes="scope dropped")
    ids = [f.id for f in store.list()]
    assert a.id in ids
    assert b.id not in ids


def test_list_include_all_returns_terminal_too(store):
    a = store.promote(target="developer", title="A", description="d")
    b = store.promote(target="developer", title="B", description="d")
    store.update_status(b.id, FR_STATUS_ARCHIVED)
    ids = [f.id for f in store.list(include_all=True)]
    assert a.id in ids
    assert b.id in ids


def test_list_filters_by_status(store):
    a = store.promote(target="developer", title="X", description="d")
    b = store.promote(target="developer", title="Y", description="d")
    store.update_status(b.id, FR_STATUS_PLANNED)
    planned = [f.id for f in store.list(status=FR_STATUS_PLANNED)]
    assert planned == [b.id]


def test_list_filters_by_target(store):
    a = store.promote(target="developer", title="A", description="d")
    b = store.promote(target="researcher", title="B", description="d")
    dev_only = [f.id for f in store.list(target="developer")]
    assert a.id in dev_only
    assert b.id not in dev_only


def test_list_orders_by_priority_then_created_at(store):
    low = store.promote(target="developer", title="Low", description="d", priority="low")
    high = store.promote(target="developer", title="High", description="d", priority="high")
    medium = store.promote(target="developer", title="Med", description="d", priority="medium")
    ids = [f.id for f in store.list()]
    assert ids.index(high.id) < ids.index(medium.id) < ids.index(low.id)


# ---------------------------------------------------------------------------
# update_status
# ---------------------------------------------------------------------------


def test_update_status_happy_path(store):
    fr = store.promote(target="developer", title="U", description="d")
    updated = store.update_status(fr.id, FR_STATUS_PLANNED, notes="taken")
    assert updated.status == FR_STATUS_PLANNED
    assert len(updated.notes_history) == 2
    assert updated.notes_history[-1]["notes"] == "taken"


def test_update_status_idempotent_same_status(store):
    fr = store.promote(target="developer", title="I", description="d")
    store.update_status(fr.id, FR_STATUS_PLANNED)
    before = store.get(fr.id)
    again = store.update_status(fr.id, FR_STATUS_PLANNED)
    assert again.status == FR_STATUS_PLANNED
    # Without notes, idempotent call doesn't append history
    assert len(again.notes_history) == len(before.notes_history)


def test_update_status_idempotent_with_notes_appends_history_and_bumps_ts(store):
    fr = store.promote(target="developer", title="I2", description="d")
    store.update_status(fr.id, FR_STATUS_IN_PROGRESS)
    before = store.get(fr.id)
    before_len = len(before.notes_history)
    before_ts = before.updated_at
    # Ensure clock advances (sub-second resolution is fine, but sleep a hair)
    import time as _t
    _t.sleep(0.01)
    again = store.update_status(fr.id, FR_STATUS_IN_PROGRESS, notes="still working")
    assert len(again.notes_history) == before_len + 1
    # updated_at must bump so consumers watching for changes see the delta
    assert again.updated_at > before_ts
    # And the history entry's timestamp matches updated_at
    assert again.notes_history[-1]["at"] == again.updated_at


def test_update_status_rejects_invalid_status_name(store):
    fr = store.promote(target="developer", title="X", description="d")
    with pytest.raises(FRError, match="status must be one of"):
        store.update_status(fr.id, "bogus")


def test_update_status_rejects_illegal_transition(store):
    fr = store.promote(target="developer", title="X", description="d")
    store.update_status(fr.id, FR_STATUS_IN_PROGRESS)
    store.update_status(fr.id, FR_STATUS_COMPLETED)
    # completed is terminal
    with pytest.raises(FRError, match="illegal transition"):
        store.update_status(fr.id, FR_STATUS_IN_PROGRESS)


def test_update_status_rejects_unknown_id(store):
    with pytest.raises(FRError, match="unknown"):
        store.update_status("fr_developer_missing", FR_STATUS_PLANNED)


def test_update_status_follows_redirect(store):
    target = store.promote(target="developer", title="T", description="d")
    _seed_merged_fr(store, source_id="fr_developer_redir", target_id=target.id)
    # Call with the merged-away id — should update the terminal FR
    updated = store.update_status("fr_developer_redir", FR_STATUS_PLANNED)
    assert updated.id == target.id
    assert updated.status == FR_STATUS_PLANNED


def test_update_status_records_branch(store):
    fr = store.promote(target="developer", title="B", description="d")
    updated = store.update_status(fr.id, FR_STATUS_IN_PROGRESS, branch="feat/foo")
    assert updated.branch == "feat/foo"


# ---------------------------------------------------------------------------
# capability tracking
# ---------------------------------------------------------------------------


def test_capability_created_on_planned(store):
    fr = store.promote(target="developer", title="CapA", description="d")
    assert store.capabilities_for("developer") == []
    store.update_status(fr.id, FR_STATUS_PLANNED)
    caps = store.capabilities_for("developer")
    assert len(caps) == 1
    assert caps[0]["name"] == "CapA"
    assert caps[0]["status"] == "planned"
    assert caps[0]["fr_id"] == fr.id


def test_capability_updated_on_completed(store):
    fr = store.promote(target="developer", title="CapB", description="d")
    store.update_status(fr.id, FR_STATUS_IN_PROGRESS)
    store.update_status(fr.id, FR_STATUS_COMPLETED, notes="shipped")
    caps = store.capabilities_for("developer")
    assert len(caps) == 1
    assert caps[0]["status"] == "exists"


def test_capability_marked_abandoned_on_archive(store):
    fr = store.promote(target="developer", title="CapC", description="d")
    store.update_status(fr.id, FR_STATUS_PLANNED)
    store.update_status(fr.id, FR_STATUS_ARCHIVED)
    caps = store.capabilities_for("developer")
    assert caps[0]["status"] == "abandoned"


def test_capability_not_created_for_open_promote(store):
    """Just-promoted (status=open) FRs don't record a capability."""
    store.promote(target="developer", title="D", description="d")
    assert store.capabilities_for("developer") == []


# ---------------------------------------------------------------------------
# set_dependency
# ---------------------------------------------------------------------------


def test_set_dependency_happy_path(store):
    a = store.promote(target="developer", title="A", description="d")
    b = store.promote(target="developer", title="B", description="d")
    updated = store.set_dependency(a.id, [b.id])
    assert updated.depends_on == [b.id]


def test_set_dependency_resolves_merged_away_id(store):
    target = store.promote(target="developer", title="New", description="d")
    _seed_merged_fr(store, source_id="fr_developer_old", target_id=target.id)
    downstream = store.promote(target="developer", title="Down", description="d")
    # Depend on the old id — should auto-forward to target.id
    updated = store.set_dependency(downstream.id, ["fr_developer_old"])
    assert updated.depends_on == [target.id]


def test_set_dependency_rejects_self_cycle(store):
    a = store.promote(target="developer", title="Self", description="d")
    with pytest.raises(FRError, match="cycle"):
        store.set_dependency(a.id, [a.id])


def test_set_dependency_rejects_transitive_cycle(store):
    a = store.promote(target="developer", title="A", description="d")
    b = store.promote(target="developer", title="B", description="d")
    store.set_dependency(a.id, [b.id])  # a depends on b
    with pytest.raises(FRError, match="transitive cycle"):
        store.set_dependency(b.id, [a.id])  # would make b depend on a (cycle)


def test_set_dependency_rejects_unknown_dep(store):
    a = store.promote(target="developer", title="A", description="d")
    with pytest.raises(FRError, match="unknown dependency"):
        store.set_dependency(a.id, ["fr_developer_missing"])


def test_set_dependency_rejects_unknown_fr(store):
    with pytest.raises(FRError, match="unknown fr id"):
        store.set_dependency("fr_developer_missing", [])


def test_set_dependency_deduplicates(store):
    a = store.promote(target="developer", title="A", description="d")
    b = store.promote(target="developer", title="B", description="d")
    updated = store.set_dependency(a.id, [b.id, b.id, b.id])
    assert updated.depends_on == [b.id]


def test_set_dependency_ignores_empty_strings(store):
    a = store.promote(target="developer", title="A", description="d")
    b = store.promote(target="developer", title="B", description="d")
    updated = store.set_dependency(a.id, ["", b.id, "  "])
    assert updated.depends_on == [b.id]


# ---------------------------------------------------------------------------
# Roundtrip
# ---------------------------------------------------------------------------


def test_full_lifecycle_roundtrip(store):
    """Full arc: promote → planned → in_progress → completed → capability exists."""
    fr = store.promote(
        target="developer", title="Full", description="d",
        priority="high", concept="lifecycle",
    )
    store.update_status(fr.id, FR_STATUS_PLANNED, notes="claimed")
    store.update_status(fr.id, FR_STATUS_IN_PROGRESS, branch="feat/full", notes="coding")
    final = store.update_status(fr.id, FR_STATUS_COMPLETED, notes="merged PR #42")

    # Four entries in history: open (promote), planned, in_progress, completed
    assert len(final.notes_history) == 4
    statuses = [h["status"] for h in final.notes_history]
    assert statuses == [FR_STATUS_OPEN, FR_STATUS_PLANNED, FR_STATUS_IN_PROGRESS, FR_STATUS_COMPLETED]

    # Capability is present and status=exists
    caps = store.capabilities_for("developer")
    assert len(caps) == 1
    assert caps[0]["status"] == "exists"
    assert caps[0]["fr_id"] == fr.id

    # List default (active) no longer includes it
    active_ids = [f.id for f in store.list()]
    assert fr.id not in active_ids

    # But include_all does
    all_ids = [f.id for f in store.list(include_all=True)]
    assert fr.id in all_ids


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def test_to_public_dict_includes_all_fields(store):
    fr = store.promote(
        target="developer", title="Ser", description="d",
        priority="high", concept="c", classification="library",
        backing_papers=["paper1", "paper2"],
    )
    d = fr.to_public_dict()
    assert d["id"] == fr.id
    assert d["target"] == "developer"
    assert d["priority"] == "high"
    assert d["classification"] == "library"
    assert d["backing_papers"] == ["paper1", "paper2"]
    assert d["merged_into"] is None
    assert d["merged_from"] == []
    assert "redirected_from" in d
