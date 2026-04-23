"""Tests for developer's DogfoodStore (Phase 1 — CRUD-only)."""

from __future__ import annotations

import asyncio

import pytest

from khonliang.knowledge.store import KnowledgeStore

from developer.dogfood_store import (
    DOGFOOD_KIND_BUG,
    DOGFOOD_KIND_FRICTION,
    DOGFOOD_KIND_UX,
    DOGFOOD_STATUS_DISMISSED,
    DOGFOOD_STATUS_DUPLICATE,
    DOGFOOD_STATUS_OBSERVED,
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
