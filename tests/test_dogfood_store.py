"""Tests for developer's DogfoodStore (Phase 1 — CRUD-only)."""

from __future__ import annotations

import asyncio

import pytest

from khonliang.knowledge.store import KnowledgeStore

from developer.dogfood_store import (
    DOGFOOD_KIND_BUG,
    DOGFOOD_KIND_DOCS,
    DOGFOOD_KIND_FRICTION,
    DOGFOOD_KIND_OTHER,
    DOGFOOD_KIND_UX,
    DOGFOOD_STATUS_DISMISSED,
    DOGFOOD_STATUS_DUPLICATE,
    DOGFOOD_STATUS_OBSERVED,
    DOGFOOD_STATUS_PROMOTED,
    DOGFOOD_STATUS_TRIAGED,
    DogfoodError,
    DogfoodStore,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    """Empty DogfoodStore with no seed data."""
    knowledge = KnowledgeStore(str(tmp_path / "dogfood.db"))
    return DogfoodStore(knowledge=knowledge, seed=False)


# ---------------------------------------------------------------------------
# log_dogfood / get_dogfood
# ---------------------------------------------------------------------------


def test_log_dogfood_creates_observed(store):
    dog = store.log_dogfood(
        "Ran into Substack 403",
        kind=DOGFOOD_KIND_FRICTION,
        target="researcher",
        reporter="user",
    )
    assert dog.status == DOGFOOD_STATUS_OBSERVED
    assert dog.kind == DOGFOOD_KIND_FRICTION
    assert dog.target == "researcher"
    assert dog.id.startswith("dog_")
    assert dog.observed_at > 0
    assert dog.promoted_to == []
    assert dog.duplicate_of == ""
    # source field defaults to None per fr_developer_47271f34 forward-compat
    assert dog.source is None


def test_log_dogfood_roundtrip(store):
    dog = store.log_dogfood("observation text", target="developer")
    out = store.get_dogfood(dog.id)
    assert out is not None
    assert out.id == dog.id
    assert out.observation == "observation text"


def test_dogfood_observed_at_epoch_zero_roundtrips(store):
    dog = store.log_dogfood(
        "observation",
        target="developer",
        observed_at=0.0,
    )
    assert dog.observed_at == 0.0
    out = store.get_dogfood(dog.id)
    assert out is not None
    assert out.observed_at == 0.0, (
        "epoch-0 observed_at must survive round-trip; "
        "prior impl used `or` which treats 0.0 as falsy"
    )


def test_log_dogfood_rejects_empty_observation(store):
    with pytest.raises(DogfoodError, match="non-empty"):
        store.log_dogfood("")
    with pytest.raises(DogfoodError, match="non-empty"):
        store.log_dogfood("    ")


def test_log_dogfood_rejects_invalid_kind(store):
    with pytest.raises(DogfoodError, match="kind"):
        store.log_dogfood("x", kind="other_bogus")


def test_log_dogfood_is_pure_local(store, monkeypatch):
    """``log_dogfood`` must be a pure local write — no LLM call, no network,
    no subprocess. Prior version of this test measured wall-clock elapsed
    (<1s for 10 writes), which flaked under CI load. Replace the timing
    smoke with a deterministic invariant: patch ``time.time`` on the
    module to a fixed fake clock and assert the store consumes that fake
    value verbatim. If the code path reached out to a real clock (let
    alone an LLM / HTTP / subprocess), the stored ``observed_at`` /
    ``created_at`` would diverge from the fake value.
    """
    from developer import dogfood_store as mod

    FAKE_NOW = 424242.0
    monkeypatch.setattr(mod.time, "time", lambda: FAKE_NOW)

    # observed_at omitted → store must fall back to ``time.time()``, which
    # is now the fake. Any real syscall would produce a wall-clock value.
    dog = store.log_dogfood("pure-local observation")

    assert dog.observed_at == FAKE_NOW, (
        "observed_at fallback must consume the patched time.time(); "
        "a non-FAKE value means log_dogfood reached a real clock / "
        "external I/O path"
    )
    assert dog.created_at == FAKE_NOW
    assert dog.updated_at == FAKE_NOW


def test_log_dogfood_same_observation_distinct_times_separate_ids(store):
    """Recurring friction should produce distinct entries, not be deduped."""
    a = store.log_dogfood("recurring thing", observed_at=100.0)
    b = store.log_dogfood("recurring thing", observed_at=200.0)
    assert a.id != b.id


def test_log_dogfood_source_roundtrip(store):
    """Phase 2 ingest will populate source; Phase 1 accepts + persists the shape."""
    src = {
        "kind": "github_issue",
        "url": "https://github.com/x/y/issues/2",
        "repo": "x/y",
        "number": 2,
        "author": "bob",
        "labels": ["friction"],
        "created_at": 0.0,
        "issue_title": "…",
        "body_hash": "hex",
    }
    dog = store.log_dogfood("ingested friction", source=src)
    round_tripped = store.get_dogfood(dog.id)
    assert round_tripped.source == src


def test_log_dogfood_rejects_collision_with_non_dogfood_entry(store):
    """A non-dogfood KnowledgeEntry at the derived dog id must not be silently
    overwritten. Prior impl only raised on dogfood-tagged collisions; any
    other pre-existing entry (corrupted data or an unrelated entry type
    reusing the id) would be clobbered by ``knowledge.add()``. Enforce
    refusal.
    """
    from khonliang.knowledge.store import KnowledgeEntry, Tier
    from developer.dogfood_store import _derive_dog_id

    observation = "collision-candidate observation"
    observed = 1234.0
    dog_id = _derive_dog_id(observation, observed)

    # Seed a KnowledgeEntry at the same id but with unrelated tags.
    store.knowledge.add(KnowledgeEntry(
        id=dog_id,
        tier=Tier.DERIVED,
        title="pre-existing non-dogfood entry",
        content="some other content",
        tags=["fr", "unrelated"],
    ))

    with pytest.raises(DogfoodError, match="id collision with non-dogfood entry"):
        store.log_dogfood(observation, observed_at=observed)

    # Pre-existing entry must be untouched.
    survivor = store.knowledge.get(dog_id)
    assert survivor is not None
    assert survivor.title == "pre-existing non-dogfood entry"
    assert "dogfood" not in (survivor.tags or [])


# ---------------------------------------------------------------------------
# list_dogfood — ordering, kind/target/since filters, terminal default
# ---------------------------------------------------------------------------


def test_list_newest_first(store):
    a = store.log_dogfood("first", observed_at=1.0)
    b = store.log_dogfood("second", observed_at=2.0)
    c = store.log_dogfood("third", observed_at=3.0)
    ids = [d.id for d in store.list_dogfood()]
    assert ids[:3] == [c.id, b.id, a.id]


def test_list_filters_by_kind(store):
    f = store.log_dogfood("friction 1", kind=DOGFOOD_KIND_FRICTION, observed_at=1.0)
    u = store.log_dogfood("ux 1", kind=DOGFOOD_KIND_UX, observed_at=2.0)
    ids = {d.id for d in store.list_dogfood(kind=DOGFOOD_KIND_FRICTION)}
    assert ids == {f.id}


def test_list_filters_by_target(store):
    a = store.log_dogfood("x", target="researcher", observed_at=1.0)
    b = store.log_dogfood("y", target="developer", observed_at=2.0)
    ids = {d.id for d in store.list_dogfood(target="developer")}
    assert ids == {b.id}


def test_list_filters_by_since(store):
    a = store.log_dogfood("old", observed_at=1.0)
    b = store.log_dogfood("new", observed_at=100.0)
    ids = [d.id for d in store.list_dogfood(since=50.0)]
    assert ids == [b.id]


def test_list_excludes_terminal_by_default(store):
    a = store.log_dogfood("active", observed_at=1.0)
    b = store.log_dogfood("dismissed", observed_at=2.0)
    store.mark_dismissed(b.id)
    ids = [d.id for d in store.list_dogfood()]
    assert a.id in ids
    assert b.id not in ids


def test_list_status_all_includes_terminal(store):
    a = store.log_dogfood("active", observed_at=1.0)
    b = store.log_dogfood("dismissed", observed_at=2.0)
    store.mark_dismissed(b.id)
    ids = {d.id for d in store.list_dogfood(status="all")}
    assert a.id in ids
    assert b.id in ids


def test_list_filters_by_status_name(store):
    a = store.log_dogfood("a", observed_at=1.0)
    b = store.log_dogfood("b", observed_at=2.0)
    store.mark_dismissed(b.id)
    ids = [d.id for d in store.list_dogfood(status=DOGFOOD_STATUS_DISMISSED)]
    assert ids == [b.id]


def test_list_limit_caps_result(store):
    for i in range(5):
        store.log_dogfood(f"obs {i}", observed_at=float(i))
    dogs = store.list_dogfood(limit=3)
    assert len(dogs) == 3


def test_list_limit_none_means_no_cap(store):
    for i in range(5):
        store.log_dogfood(f"obs {i}", observed_at=float(i))
    dogs = store.list_dogfood(limit=None)
    assert len(dogs) == 5


def test_list_dogfood_rejects_or_normalizes_negative_limit(store):
    """Negative limits are normalized to 0 (empty) — matches MCP handler.

    Covers the R2 finding: docstring says ``limit=None`` is the escape
    hatch for no cap, but the previous ``limit is not None and limit >= 0``
    implementation silently returned all rows for negative values (e.g.
    ``-1``) — contradicting both the docstring and the MCP handler which
    clamps negatives to 0. Store now matches handler normalization.
    """
    for i in range(5):
        store.log_dogfood(f"obs {i}", observed_at=float(i))

    # Negative limit must return empty (normalized to 0), not all rows.
    assert store.list_dogfood(limit=-1) == []
    assert store.list_dogfood(limit=-100) == []
    # Zero limit returns empty.
    assert store.list_dogfood(limit=0) == []
    # None still means no cap.
    assert len(store.list_dogfood(limit=None)) == 5
    # Positive limit still caps normally.
    assert len(store.list_dogfood(limit=2)) == 2


# ---------------------------------------------------------------------------
# mark_dismissed
# ---------------------------------------------------------------------------


def test_mark_dismissed_sets_terminal(store):
    dog = store.log_dogfood("x")
    out = store.mark_dismissed(dog.id, notes="not a real issue")
    assert out.status == DOGFOOD_STATUS_DISMISSED


def test_mark_dismissed_rejects_unknown(store):
    with pytest.raises(DogfoodError, match="unknown"):
        store.mark_dismissed("dog_missing")


def test_mark_dismissed_rejects_terminal(store):
    dog = store.log_dogfood("x")
    store.mark_dismissed(dog.id)
    with pytest.raises(DogfoodError, match="terminal"):
        store.mark_dismissed(dog.id)


# ---------------------------------------------------------------------------
# mark_duplicate
# ---------------------------------------------------------------------------


def test_mark_duplicate_sets_status_and_pointer(store):
    a = store.log_dogfood("canonical", observed_at=1.0)
    b = store.log_dogfood("same thing again", observed_at=2.0)
    out = store.mark_duplicate(b.id, a.id)
    assert out.status == DOGFOOD_STATUS_DUPLICATE
    assert out.duplicate_of == a.id


def test_mark_duplicate_rejects_self(store):
    a = store.log_dogfood("x")
    with pytest.raises(DogfoodError, match="itself"):
        store.mark_duplicate(a.id, a.id)


def test_mark_duplicate_rejects_unknown_target(store):
    a = store.log_dogfood("x")
    with pytest.raises(DogfoodError, match="duplicate target"):
        store.mark_duplicate(a.id, "dog_missing")


def test_mark_duplicate_rejects_terminal(store):
    a = store.log_dogfood("canon", observed_at=1.0)
    b = store.log_dogfood("dup", observed_at=2.0)
    store.mark_dismissed(b.id)
    with pytest.raises(DogfoodError, match="terminal"):
        store.mark_duplicate(b.id, a.id)


# ---------------------------------------------------------------------------
# Seed data idempotence
# ---------------------------------------------------------------------------


def test_seed_data_present_on_first_init(tmp_path):
    """Fresh store gets the 5 curated seed entries verbatim."""
    knowledge = KnowledgeStore(str(tmp_path / "seeded.db"))
    store = DogfoodStore(knowledge=knowledge)  # seed=True default
    dogs = store.list_dogfood(status="all", limit=None)
    assert len(dogs) == 5
    observations = sorted(d.observation for d in dogs)
    # Verbatim distinguishing substrings from fr_developer_1324440c
    assert any("Substack 403" in o for o in observations)
    assert any("JSON-escaped JSON-in-JSON" in o for o in observations)
    assert any("without launching Claude Code" in o for o in observations)
    assert any("non-RL article" in o for o in observations)
    assert any("promote_fr returns only the id" in o for o in observations)


def test_seed_data_idempotent_on_second_init(tmp_path):
    """Constructing a second DogfoodStore against the same DB doesn't duplicate."""
    db_path = str(tmp_path / "idempotent.db")
    knowledge_a = KnowledgeStore(db_path)
    DogfoodStore(knowledge=knowledge_a)
    first_count = len(
        DogfoodStore(knowledge=knowledge_a, seed=False).list_dogfood(status="all", limit=None)
    )

    knowledge_b = KnowledgeStore(db_path)
    DogfoodStore(knowledge=knowledge_b)  # re-seed attempt
    second_count = len(
        DogfoodStore(knowledge=knowledge_b, seed=False).list_dogfood(status="all", limit=None)
    )
    assert first_count == 5
    assert second_count == 5


def test_seed_data_skipped_when_store_already_has_rows(tmp_path):
    """If user-logged entries exist before a seeded init, seeds don't add more."""
    db_path = str(tmp_path / "prepop.db")
    knowledge = KnowledgeStore(db_path)
    store_no_seed = DogfoodStore(knowledge=knowledge, seed=False)
    store_no_seed.log_dogfood("user observation")
    knowledge2 = KnowledgeStore(db_path)
    store_seeded = DogfoodStore(knowledge=knowledge2)
    assert len(store_seeded.list_dogfood(status="all", limit=None)) == 1


# ---------------------------------------------------------------------------
# MCP skill wiring
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def _make_agent(pipeline):
    from developer.agent import DeveloperAgent
    agent = DeveloperAgent(
        agent_id="test-developer",
        bus_url="http://localhost:8787",
        config_path="unused",
    )
    agent._pipeline = pipeline
    return agent


def test_mcp_log_dogfood_happy_path(pipeline):
    agent = _make_agent(pipeline)
    result = _run(agent.handle_log_dogfood({
        "observation": "new friction",
        "kind": DOGFOOD_KIND_FRICTION,
        "target": "developer",
    }))
    assert "dog_id" in result
    assert result["kind"] == DOGFOOD_KIND_FRICTION
    assert result["status"] == DOGFOOD_STATUS_OBSERVED


def test_mcp_list_dogfood_happy_path(pipeline):
    agent = _make_agent(pipeline)
    pipeline.dogfood.log_dogfood("obs via MCP", target="developer",
                                 observed_at=9_999_999_999.0)
    result = _run(agent.handle_list_dogfood({
        "target": "developer",
        "detail": "compact",
        "limit": 5,
    }))
    assert result["count"] >= 1
    assert all(d.get("kind") for d in result["dogfood"])  # compact has kind


def test_mcp_get_dogfood_happy_path(pipeline):
    agent = _make_agent(pipeline)
    dog = pipeline.dogfood.log_dogfood("get me", observed_at=12345.0)
    result = _run(agent.handle_get_dogfood({"dog_id": dog.id}))
    assert result["id"] == dog.id
    assert result["observation"] == "get me"


def test_get_dogfood_honors_detail_brief(pipeline):
    """Default detail=brief returns the narrow 5-field shape."""
    agent = _make_agent(pipeline)
    dog = pipeline.dogfood.log_dogfood(
        "brief case",
        kind=DOGFOOD_KIND_FRICTION,
        target="developer",
        context="ctx",
        observed_at=42.0,
    )
    result = _run(agent.handle_get_dogfood({"dog_id": dog.id, "detail": "brief"}))
    assert set(result) == {"id", "observation", "status", "observed_at", "updated_at"}
    # brief drops kind / target / context / notes_history / source
    assert "kind" not in result
    assert "target" not in result
    assert "context" not in result


def test_get_dogfood_honors_detail_full(pipeline):
    """detail=full returns the complete public dict (context / kind / target / source)."""
    agent = _make_agent(pipeline)
    dog = pipeline.dogfood.log_dogfood(
        "full case",
        kind=DOGFOOD_KIND_FRICTION,
        target="developer",
        context="from PR review",
        observed_at=43.0,
    )
    result = _run(agent.handle_get_dogfood({"dog_id": dog.id, "detail": "full"}))
    assert result["id"] == dog.id
    assert result["observation"] == "full case"
    assert result["kind"] == DOGFOOD_KIND_FRICTION
    assert result["target"] == "developer"
    assert result["context"] == "from PR review"
    assert result["status"] == DOGFOOD_STATUS_OBSERVED
    assert "notes_history" in result
    assert result["source"] is None


def test_get_dogfood_honors_detail_compact(pipeline):
    """detail=compact adds kind + target on top of brief."""
    agent = _make_agent(pipeline)
    dog = pipeline.dogfood.log_dogfood(
        "compact case",
        kind=DOGFOOD_KIND_UX,
        target="researcher",
        observed_at=44.0,
    )
    result = _run(agent.handle_get_dogfood({"dog_id": dog.id, "detail": "compact"}))
    assert result["kind"] == DOGFOOD_KIND_UX
    assert result["target"] == "researcher"
    # compact still omits context / notes_history / source
    assert "context" not in result
    assert "notes_history" not in result


def test_mcp_list_dogfood_since_filter(pipeline):
    agent = _make_agent(pipeline)
    pipeline.dogfood.log_dogfood("older", observed_at=1.0)
    pipeline.dogfood.log_dogfood("newer", observed_at=1_000_000_000.0)
    result = _run(agent.handle_list_dogfood({
        "since": "500000000",
    }))
    observations = [d.get("observation") for d in result["dogfood"]]
    assert "newer" in observations
    assert "older" not in observations


def test_handle_list_dogfood_preserves_explicit_none_for_no_cap(pipeline):
    """MCP handler must pass through explicit ``limit=None`` as "no cap",
    while still defaulting omitted limit to 20 and honoring integer limits.

    Prior impl coerced ``None`` via ``int(None)`` → TypeError → fallback 20,
    making the "no cap" API expressible by the store but not by the MCP
    surface.
    """
    agent = _make_agent(pipeline)

    # Seed >20 observations — enough to see the cap effect and the
    # no-cap effect clearly.
    n_extra = 25
    for i in range(n_extra):
        pipeline.dogfood.log_dogfood(f"obs-{i}", observed_at=float(1000 + i))

    # Total rows in the store (includes seed data).
    total = len(pipeline.dogfood.list_dogfood(status="all", limit=None))
    assert total > 20, "test setup should push past the default cap of 20"

    # limit=None → unbounded.
    result_none = _run(agent.handle_list_dogfood({
        "limit": None,
        "status": "all",
    }))
    assert result_none["count"] == total

    # limit=5 → exactly 5.
    result_five = _run(agent.handle_list_dogfood({
        "limit": 5,
        "status": "all",
    }))
    assert result_five["count"] == 5

    # limit omitted → default cap of 20.
    result_default = _run(agent.handle_list_dogfood({
        "status": "all",
    }))
    assert result_default["count"] == 20


# ---------------------------------------------------------------------------
# Phase 2A: record_promotion / triage_queue / triage handlers
# ---------------------------------------------------------------------------


def test_record_promotion_preserves_observation_verbatim(store):
    """Per-FR acceptance criterion: observation survives triage unchanged."""
    dog = store.log_dogfood(
        "the exact observation text",
        kind=DOGFOOD_KIND_FRICTION,
        target="researcher",
        context="ctx",
    )
    original_observation = dog.observation
    out = store.record_promotion(dog.id, "bug_x_1", "bug")
    assert out.observation == original_observation
    # Status and promoted_to flip; everything else stays.
    assert out.status == DOGFOOD_STATUS_PROMOTED
    assert out.promoted_to == ["bug_x_1"]


def test_record_promotion_idempotent(store):
    dog = store.log_dogfood("obs", observed_at=1.0)
    store.record_promotion(dog.id, "bug_x_1", "bug")
    before_notes = len(store.get_dogfood(dog.id).notes_history)
    out = store.record_promotion(dog.id, "bug_x_1", "bug")
    assert out.promoted_to == ["bug_x_1"]
    assert len(out.notes_history) == before_notes


def test_record_promotion_supports_multiple_targets(store):
    dog = store.log_dogfood("obs", observed_at=1.0)
    store.record_promotion(dog.id, "bug_a_1", "bug")
    out = store.record_promotion(dog.id, "fr_a_2", "fr")
    assert out.promoted_to == ["bug_a_1", "fr_a_2"]
    assert out.status == DOGFOOD_STATUS_PROMOTED


def test_record_promotion_refuses_dismissed(store):
    dog = store.log_dogfood("obs", observed_at=1.0)
    store.mark_dismissed(dog.id)
    with pytest.raises(DogfoodError, match="terminal"):
        store.record_promotion(dog.id, "bug_x_1", "bug")


def test_record_promotion_rejects_bad_kind(store):
    dog = store.log_dogfood("obs", observed_at=1.0)
    with pytest.raises(DogfoodError, match="target_kind"):
        store.record_promotion(dog.id, "xxx", "epic")


def test_record_promotion_requires_non_empty_target(store):
    dog = store.log_dogfood("obs", observed_at=1.0)
    with pytest.raises(DogfoodError, match="target_id"):
        store.record_promotion(dog.id, "", "bug")


def test_triage_queue_observed_only(store):
    # Observed (eligible)
    obs1 = store.log_dogfood("obs1", kind=DOGFOOD_KIND_FRICTION, observed_at=10.0)
    # Dismissed (not eligible)
    obs2 = store.log_dogfood("obs2", observed_at=20.0)
    store.mark_dismissed(obs2.id)
    # Promoted (not eligible)
    obs3 = store.log_dogfood("obs3", observed_at=30.0)
    store.record_promotion(obs3.id, "bug_a_1", "bug")
    queue = store.triage_queue(limit=10)
    ids = [d.id for d in queue]
    assert obs1.id in ids
    assert obs2.id not in ids
    assert obs3.id not in ids


def test_triage_queue_orders_by_kind_priority_then_recency(store):
    # Oldest docs entry — low kind priority.
    d_docs_old = store.log_dogfood("docs old", kind=DOGFOOD_KIND_DOCS, observed_at=1.0)
    # Newer bug entry — highest kind priority, newer recency.
    d_bug_new = store.log_dogfood("bug new", kind=DOGFOOD_KIND_BUG, observed_at=100.0)
    # Older bug entry — highest kind priority, older recency.
    d_bug_old = store.log_dogfood("bug old", kind=DOGFOOD_KIND_BUG, observed_at=50.0)
    # ux entry — middle priority.
    d_ux = store.log_dogfood("ux", kind=DOGFOOD_KIND_UX, observed_at=200.0)
    queue = store.triage_queue(limit=10)
    ids = [d.id for d in queue]
    # bugs come first (by kind priority), oldest-first within tier.
    assert ids[0] == d_bug_old.id
    assert ids[1] == d_bug_new.id
    # then ux/friction tier (ux here).
    assert d_ux.id in ids[2:]
    # docs is lowest priority, comes after ux.
    assert ids.index(d_ux.id) < ids.index(d_docs_old.id)


def test_triage_queue_respects_limit(store):
    for i in range(15):
        store.log_dogfood(f"o{i}", observed_at=float(i))
    queue = store.triage_queue(limit=5)
    assert len(queue) == 5


# -- triage_dogfood handler --


def _make_agent(pipeline):
    from developer.agent import DeveloperAgent
    agent = DeveloperAgent(
        agent_id="test-developer",
        bus_url="http://localhost:8787",
        config_path="unused",
    )
    agent._pipeline = pipeline
    return agent


def test_triage_dogfood_promote_to_bug_round_trip(pipeline):
    """observation → promoted → bug exists → dog.promoted_to has bug_id."""
    agent = _make_agent(pipeline)
    dog = pipeline.dogfood.log_dogfood(
        "captured friction", kind=DOGFOOD_KIND_FRICTION, target="developer",
        observed_at=9_999_999_999.0,
    )
    original_observation = dog.observation
    result = _run(agent.handle_triage_dogfood({
        "dog_id": dog.id,
        "action": "promote_to_bug",
    }))
    assert result["status"] == DOGFOOD_STATUS_PROMOTED
    assert result["bug_id"].startswith("bug_developer_")
    bug = pipeline.bugs.get_bug(result["bug_id"])
    assert bug is not None
    assert "captured friction" in bug.description
    # Observation must survive verbatim in the source dogfood row.
    dog_after = pipeline.dogfood.get_dogfood(dog.id)
    assert dog_after.observation == original_observation
    assert result["bug_id"] in dog_after.promoted_to


def test_triage_dogfood_promote_to_fr_round_trip(pipeline):
    agent = _make_agent(pipeline)
    dog = pipeline.dogfood.log_dogfood(
        "friction worth an FR", kind=DOGFOOD_KIND_UX, target="developer",
        observed_at=9_999_999_998.0,
    )
    result = _run(agent.handle_triage_dogfood({
        "dog_id": dog.id,
        "action": "promote_to_fr",
    }))
    assert result["status"] == DOGFOOD_STATUS_PROMOTED
    assert result["fr_id"].startswith("fr_developer_")
    fr = pipeline.frs.get(result["fr_id"])
    assert fr is not None
    # FR description references the source dog id (provenance).
    assert dog.id in fr.description
    dog_after = pipeline.dogfood.get_dogfood(dog.id)
    assert result["fr_id"] in dog_after.promoted_to
    # Observation survives triage verbatim.
    assert dog_after.observation == "friction worth an FR"


def test_triage_dogfood_promote_to_bug_idempotent_reuse(pipeline):
    """Re-promoting a dog to a bug reuses the existing bug_id.

    Without the handler-level idempotency check, the retry would call
    file_bug again, which raises on the deterministic id collision
    BEFORE record_promotion can no-op. Verify the second call returns
    the same bug_id with promotion_reused=True and does not raise or
    create a second bug.
    """
    agent = _make_agent(pipeline)
    dog = pipeline.dogfood.log_dogfood(
        "retry friction bug", kind=DOGFOOD_KIND_FRICTION, target="developer",
        observed_at=9_999_999_997.0,
    )
    first = _run(agent.handle_triage_dogfood({
        "dog_id": dog.id,
        "action": "promote_to_bug",
    }))
    assert first["bug_id"].startswith("bug_developer_")
    assert first["promotion_reused"] is False
    bugs_before = len(pipeline.bugs.list_bugs(status="all"))

    second = _run(agent.handle_triage_dogfood({
        "dog_id": dog.id,
        "action": "promote_to_bug",
    }))
    assert "error" not in second
    assert second["bug_id"] == first["bug_id"]
    assert second["promotion_reused"] is True
    assert second["status"] == DOGFOOD_STATUS_PROMOTED
    # No duplicate bug row was created.
    assert len(pipeline.bugs.list_bugs(status="all")) == bugs_before


def test_triage_dogfood_promote_to_fr_idempotent_reuse(pipeline):
    """Re-promoting a dog to an FR reuses the existing fr_id.

    Mirror of the promote_to_bug idempotency test — FR promote() raises
    on the deterministic id collision, so the handler must short-circuit
    on already-promoted rows and return the existing fr_id.
    """
    agent = _make_agent(pipeline)
    dog = pipeline.dogfood.log_dogfood(
        "retry friction fr", kind=DOGFOOD_KIND_UX, target="developer",
        observed_at=9_999_999_996.0,
    )
    first = _run(agent.handle_triage_dogfood({
        "dog_id": dog.id,
        "action": "promote_to_fr",
    }))
    assert first["fr_id"].startswith("fr_developer_")
    assert first["promotion_reused"] is False
    frs_before = len(pipeline.frs.list(include_all=True))

    second = _run(agent.handle_triage_dogfood({
        "dog_id": dog.id,
        "action": "promote_to_fr",
    }))
    assert "error" not in second
    assert second["fr_id"] == first["fr_id"]
    assert second["promotion_reused"] is True
    assert second["status"] == DOGFOOD_STATUS_PROMOTED
    # No duplicate FR row was created.
    assert len(pipeline.frs.list(include_all=True)) == frs_before


def test_triage_dogfood_dismiss(pipeline):
    agent = _make_agent(pipeline)
    dog = pipeline.dogfood.log_dogfood("meh", observed_at=1.0)
    result = _run(agent.handle_triage_dogfood({
        "dog_id": dog.id,
        "action": "dismiss",
        "notes": "out of scope",
    }))
    assert result["status"] == DOGFOOD_STATUS_DISMISSED


def test_triage_dogfood_mark_duplicate(pipeline):
    agent = _make_agent(pipeline)
    original = pipeline.dogfood.log_dogfood("o1", observed_at=1.0)
    dup = pipeline.dogfood.log_dogfood("o2", observed_at=2.0)
    result = _run(agent.handle_triage_dogfood({
        "dog_id": dup.id,
        "action": "mark_duplicate",
        "target_id": original.id,
    }))
    assert result["status"] == DOGFOOD_STATUS_DUPLICATE
    assert result["duplicate_of"] == original.id


def test_triage_dogfood_mark_duplicate_requires_target(pipeline):
    agent = _make_agent(pipeline)
    dog = pipeline.dogfood.log_dogfood("o", observed_at=1.0)
    result = _run(agent.handle_triage_dogfood({
        "dog_id": dog.id,
        "action": "mark_duplicate",
    }))
    assert "error" in result


def test_triage_dogfood_unknown_action(pipeline):
    agent = _make_agent(pipeline)
    dog = pipeline.dogfood.log_dogfood("o", observed_at=1.0)
    result = _run(agent.handle_triage_dogfood({
        "dog_id": dog.id,
        "action": "explode",
    }))
    assert "error" in result


def test_triage_dogfood_unknown_id(pipeline):
    agent = _make_agent(pipeline)
    result = _run(agent.handle_triage_dogfood({
        "dog_id": "dog_nope12",
        "action": "dismiss",
    }))
    assert "error" in result


def test_triage_dogfood_rejects_promote_to_bug_on_terminal_dogfood(pipeline):
    """promote_to_bug on dismissed / duplicate dogfood is refused.

    Without the up-front guard, file_bug would create the bug first and
    record_promotion would then refuse the terminal status — stranding
    an orphan bug. Verify the rejection keeps bugs table unchanged for
    both dismissed and duplicate states. The ``promoted`` status is
    NOT a full-terminal refuse here — cross-kind double-promotion is
    allowed (see test_triage_dogfood_cross_kind_promotion_allowed) and
    same-kind re-promotion is idempotent-reuse
    (test_triage_dogfood_promote_to_bug_idempotent_reuse). The only
    promoted-state refusal is the genuine-corruption case (status
    promoted + empty promoted_to), covered by
    test_triage_dogfood_promote_refuses_only_on_genuine_corruption.
    """
    agent = _make_agent(pipeline)

    # Case 1: dismissed dogfood rejects promote_to_bug.
    dismissed = pipeline.dogfood.log_dogfood(
        "dismissed friction", kind=DOGFOOD_KIND_FRICTION, observed_at=1.0,
    )
    pipeline.dogfood.mark_dismissed(dismissed.id)
    bugs_before = len(pipeline.bugs.list_bugs(status="all"))
    result = _run(agent.handle_triage_dogfood({
        "dog_id": dismissed.id,
        "action": "promote_to_bug",
    }))
    assert "error" in result
    assert "terminal" in result["error"].lower()
    assert len(pipeline.bugs.list_bugs(status="all")) == bugs_before

    # Case 2: duplicate dogfood rejects promote_to_bug.
    original = pipeline.dogfood.log_dogfood(
        "orig", kind=DOGFOOD_KIND_FRICTION, observed_at=2.0,
    )
    dup = pipeline.dogfood.log_dogfood(
        "dup", kind=DOGFOOD_KIND_FRICTION, observed_at=3.0,
    )
    pipeline.dogfood.mark_duplicate(dup.id, original.id)
    bugs_before = len(pipeline.bugs.list_bugs(status="all"))
    result = _run(agent.handle_triage_dogfood({
        "dog_id": dup.id,
        "action": "promote_to_bug",
    }))
    assert "error" in result
    assert "terminal" in result["error"].lower()
    assert len(pipeline.bugs.list_bugs(status="all")) == bugs_before


def test_triage_dogfood_rejects_promote_to_fr_on_terminal_dogfood(pipeline):
    """Symmetric fix — promote_to_fr on terminal dogfood refused up-front.

    Without the guard, promote() would create the FR and then
    record_promotion would refuse the terminal status, stranding an
    orphan FR. Verify the rejection keeps FR count unchanged.
    """
    agent = _make_agent(pipeline)

    # Case 1: dismissed dogfood rejects promote_to_fr.
    dismissed = pipeline.dogfood.log_dogfood(
        "dismissed UX", kind=DOGFOOD_KIND_UX, observed_at=1.0,
    )
    pipeline.dogfood.mark_dismissed(dismissed.id)
    frs_before = len(pipeline.frs.list(include_all=True))
    result = _run(agent.handle_triage_dogfood({
        "dog_id": dismissed.id,
        "action": "promote_to_fr",
    }))
    assert "error" in result
    assert "terminal" in result["error"].lower()
    assert len(pipeline.frs.list(include_all=True)) == frs_before

    # Case 2: duplicate dogfood rejects promote_to_fr.
    original = pipeline.dogfood.log_dogfood(
        "o", kind=DOGFOOD_KIND_UX, observed_at=2.0,
    )
    dup = pipeline.dogfood.log_dogfood(
        "d", kind=DOGFOOD_KIND_UX, observed_at=3.0,
    )
    pipeline.dogfood.mark_duplicate(dup.id, original.id)
    frs_before = len(pipeline.frs.list(include_all=True))
    result = _run(agent.handle_triage_dogfood({
        "dog_id": dup.id,
        "action": "promote_to_fr",
    }))
    assert "error" in result
    assert "terminal" in result["error"].lower()
    assert len(pipeline.frs.list(include_all=True)) == frs_before


def test_triage_dogfood_cross_kind_promotion_allowed(pipeline):
    """Cross-kind double promotion is valid — dog can go to FR then bug.

    The R2 promoted-state guard refused whenever no TARGET-kind entry
    was in promoted_to; that falsely blocked cross-kind flows (promoted
    to fr_*, now asked to promote to bug_*). R3 narrows the guard to
    fire only on genuine corruption (status=promoted + empty list).
    Here: promote_to_fr first, then promote_to_bug on the same dog.
    Both must succeed; promoted_to must end with both kinds.
    """
    agent = _make_agent(pipeline)
    dog = pipeline.dogfood.log_dogfood(
        "cross-kind friction", kind=DOGFOOD_KIND_FRICTION, target="developer",
        observed_at=9_999_999_995.0,
    )
    # First: promote to FR.
    first = _run(agent.handle_triage_dogfood({
        "dog_id": dog.id,
        "action": "promote_to_fr",
    }))
    assert "error" not in first, first
    assert first["fr_id"].startswith("fr_developer_")
    assert first["promotion_reused"] is False
    assert first["status"] == DOGFOOD_STATUS_PROMOTED

    # Second: promote same dog to bug — cross-kind, must be allowed.
    second = _run(agent.handle_triage_dogfood({
        "dog_id": dog.id,
        "action": "promote_to_bug",
    }))
    assert "error" not in second, second
    assert second["bug_id"].startswith("bug_developer_")
    assert second["promotion_reused"] is False
    assert second["status"] == DOGFOOD_STATUS_PROMOTED

    # promoted_to has BOTH the fr_* and bug_* entries.
    dog_after = pipeline.dogfood.get_dogfood(dog.id)
    assert first["fr_id"] in dog_after.promoted_to
    assert second["bug_id"] in dog_after.promoted_to
    assert any(t.startswith("fr_") for t in dog_after.promoted_to)
    assert any(t.startswith("bug_") for t in dog_after.promoted_to)

    # Same-kind reuse still works after the cross-kind split: re-calling
    # promote_to_fr returns the original fr_id (idempotent-reuse).
    third = _run(agent.handle_triage_dogfood({
        "dog_id": dog.id,
        "action": "promote_to_fr",
    }))
    assert "error" not in third
    assert third["fr_id"] == first["fr_id"]
    assert third["promotion_reused"] is True


def test_triage_dogfood_same_kind_re_promotion_returns_existing(pipeline):
    """R1 behavior preserved: same-kind re-promotion reuses the existing id.

    Complementary to the cross-kind test — verifies that narrowing the
    R2 guard did not break the R1 idempotent-reuse path. First
    promote_to_bug creates a fresh bug_id; second promote_to_bug
    returns the same id with promotion_reused=True and no new bug row.
    """
    agent = _make_agent(pipeline)
    dog = pipeline.dogfood.log_dogfood(
        "same-kind replay bug", kind=DOGFOOD_KIND_FRICTION, target="developer",
        observed_at=9_999_999_994.0,
    )
    first = _run(agent.handle_triage_dogfood({
        "dog_id": dog.id,
        "action": "promote_to_bug",
    }))
    assert "error" not in first
    assert first["promotion_reused"] is False
    bugs_before = len(pipeline.bugs.list_bugs(status="all"))

    second = _run(agent.handle_triage_dogfood({
        "dog_id": dog.id,
        "action": "promote_to_bug",
    }))
    assert "error" not in second
    assert second["bug_id"] == first["bug_id"]
    assert second["promotion_reused"] is True
    # No new bug created.
    assert len(pipeline.bugs.list_bugs(status="all")) == bugs_before


def test_triage_dogfood_promote_refuses_only_on_genuine_corruption(pipeline):
    """status=promoted + empty promoted_to is the sole promoted-state refusal.

    Manually inject the corrupted state (status lies about reality:
    promoted flag set, promoted_to list empty). Both promote_to_bug
    and promote_to_fr must refuse with a corruption message and must
    NOT create a downstream record.
    """
    agent = _make_agent(pipeline)

    # Inject genuine corruption directly via the store's internal
    # mutation path. Using log_dogfood + private-state edit is the
    # cleanest way to simulate a corrupted DB row without exercising
    # record_promotion's protections.
    dog = pipeline.dogfood.log_dogfood(
        "corruption bait", kind=DOGFOOD_KIND_FRICTION, target="developer",
        observed_at=9_999_999_993.0,
    )
    # Flip status to promoted without adding an entry to promoted_to.
    dog.status = DOGFOOD_STATUS_PROMOTED
    dog.promoted_to = []
    pipeline.dogfood._store(dog)

    bugs_before = len(pipeline.bugs.list_bugs(status="all"))
    frs_before = len(pipeline.frs.list(include_all=True))

    bug_result = _run(agent.handle_triage_dogfood({
        "dog_id": dog.id,
        "action": "promote_to_bug",
    }))
    assert "error" in bug_result
    assert "corrupt" in bug_result["error"].lower()
    assert len(pipeline.bugs.list_bugs(status="all")) == bugs_before

    fr_result = _run(agent.handle_triage_dogfood({
        "dog_id": dog.id,
        "action": "promote_to_fr",
    }))
    assert "error" in fr_result
    assert "corrupt" in fr_result["error"].lower()
    assert len(pipeline.frs.list(include_all=True)) == frs_before


# -- dogfood_triage_queue handler --


def test_dogfood_triage_queue_handler_ordering(pipeline):
    agent = _make_agent(pipeline)
    # Seed a bug-kind and a docs-kind; bug outranks docs.
    bug_kind = pipeline.dogfood.log_dogfood(
        "latent bug", kind=DOGFOOD_KIND_BUG, observed_at=100.0,
    )
    pipeline.dogfood.log_dogfood(
        "docs nit", kind=DOGFOOD_KIND_DOCS, observed_at=1_000.0,
    )
    result = _run(agent.handle_dogfood_triage_queue({"limit": 5}))
    # First entry should be the bug-kind one.
    first = result["dogfood"][0]
    assert first["id"] == bug_kind.id


def test_dogfood_triage_queue_default_detail_is_compact(pipeline):
    agent = _make_agent(pipeline)
    pipeline.dogfood.log_dogfood(
        "x", kind=DOGFOOD_KIND_FRICTION, target="developer", observed_at=5.0,
    )
    result = _run(agent.handle_dogfood_triage_queue({}))
    # compact_dict includes 'kind' and 'target'.
    assert result["dogfood"]
    first = result["dogfood"][0]
    assert "kind" in first
    assert "target" in first


# ---------------------------------------------------------------------------
# Phase 3 of fr_developer_5d0a8711 — project dimension
# ---------------------------------------------------------------------------


def test_dogfood_project_defaults_to_khonliang(store):
    from developer.project_store import DEFAULT_PROJECT
    dog = store.log_dogfood("first friction")
    assert dog.project == DEFAULT_PROJECT
    assert store.knowledge.get(dog.id).metadata["project"] == DEFAULT_PROJECT


def test_dogfood_project_passes_through_log(store):
    dog = store.log_dogfood("alpha friction", project="alpha-app")
    assert dog.project == "alpha-app"
    assert store.knowledge.get(dog.id).metadata["project"] == "alpha-app"


def test_dogfood_list_filters_by_project(store):
    import time
    a = store.log_dogfood("alpha thing", project="alpha", observed_at=time.time() - 1)
    b = store.log_dogfood("beta thing", project="beta", observed_at=time.time())
    alpha_only = store.list_dogfood(project="alpha")
    ids = {d.id for d in alpha_only}
    assert a.id in ids
    assert b.id not in ids


def test_dogfood_migrate_records_to_project_is_idempotent(store):
    from developer.project_store import DEFAULT_PROJECT
    from khonliang.knowledge.store import EntryStatus, KnowledgeEntry, Tier
    import time
    now = time.time()
    store.knowledge.add(KnowledgeEntry(
        id="dog_legacy01",
        tier=Tier.DERIVED,
        title="legacy obs",
        content="legacy obs",
        source="developer.dogfood_store",
        scope="development",
        confidence=1.0,
        status=EntryStatus.DISTILLED,
        tags=["dogfood", "kind:friction"],
        metadata={
            "dogfood_status": "observed",
            "kind": "friction",
        },
        created_at=now, updated_at=now,
    ))
    assert store.migrate_records_to_project(DEFAULT_PROJECT) == 1
    assert store.migrate_records_to_project(DEFAULT_PROJECT) == 0
    raw = store.knowledge.get("dog_legacy01")
    assert raw.metadata["project"] == DEFAULT_PROJECT


def test_dogfood_migrate_preserves_unknown_metadata_keys(store):
    from developer.project_store import DEFAULT_PROJECT
    from khonliang.knowledge.store import EntryStatus, KnowledgeEntry, Tier
    import time
    now = time.time()
    store.knowledge.add(KnowledgeEntry(
        id="dog_extra_keys",
        tier=Tier.DERIVED,
        title="obs with extras",
        content="observation body",
        source="developer.dogfood_store",
        scope="development",
        confidence=1.0,
        status=EntryStatus.DISTILLED,
        tags=["dogfood", "kind:friction", "custom:legacy"],
        metadata={
            "dogfood_status": "observed",
            "kind": "friction",
            "legacy_extra": "preserve",
        },
        created_at=now, updated_at=now,
    ))
    assert store.migrate_records_to_project(DEFAULT_PROJECT) == 1
    raw = store.knowledge.get("dog_extra_keys")
    assert raw.metadata["project"] == DEFAULT_PROJECT
    assert raw.metadata["legacy_extra"] == "preserve"
    assert "custom:legacy" in raw.tags


def test_dogfood_legacy_record_reads_as_default_project(store):
    from developer.project_store import DEFAULT_PROJECT
    from khonliang.knowledge.store import EntryStatus, KnowledgeEntry, Tier
    import time
    now = time.time()
    store.knowledge.add(KnowledgeEntry(
        id="dog_legacyread",
        tier=Tier.DERIVED,
        title="legacy obs",
        content="legacy obs body",
        source="developer.dogfood_store",
        scope="development",
        confidence=1.0,
        status=EntryStatus.DISTILLED,
        tags=["dogfood", "kind:friction"],
        metadata={
            "dogfood_status": "observed",
            "kind": "friction",
        },
        created_at=now, updated_at=now,
    ))
    d = store.get_dogfood("dog_legacyread")
    assert d is not None
    assert d.project == DEFAULT_PROJECT
    assert any(x.id == "dog_legacyread" and x.project == DEFAULT_PROJECT
               for x in store.list_dogfood())


def test_list_dogfood_empty_string_project_filters_for_default(store):
    from developer.project_store import DEFAULT_PROJECT
    import time
    default_d = store.log_dogfood("default obs", observed_at=time.time() - 1)
    other = store.log_dogfood("alpha obs", project="alpha", observed_at=time.time())
    filtered = store.list_dogfood(project="")
    ids = {d.id for d in filtered}
    assert default_d.id in ids
    assert other.id not in ids


def test_dogfood_same_obs_across_projects_gets_distinct_ids(store):
    import time
    ts = time.time()
    alpha = store.log_dogfood("friction A", project="alpha", observed_at=ts)
    beta = store.log_dogfood("friction A", project="beta", observed_at=ts)
    assert alpha.id != beta.id


def test_dogfood_default_project_id_stable_across_phase3():
    from developer.project_store import DEFAULT_PROJECT
    from developer.dogfood_store import _derive_dog_id
    ts = 1234.5
    a = _derive_dog_id("obs", ts)
    b = _derive_dog_id("obs", ts, project=DEFAULT_PROJECT)
    c = _derive_dog_id("obs", ts, project="")
    assert a == b == c
