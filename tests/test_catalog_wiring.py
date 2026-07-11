"""Tests for SelfCatalog wiring into FR/bug/milestone/dogfood stores.

fr_developer_cadd38f3: every mutator's private ``_store`` choke point
builds + upserts an ``IndexRecord`` into a wired ``SelfCatalog`` sidecar.
Covers: per-kind record shape (kind/facets/project/text/ref), the link
mapping table, the milestone hard-delete -> catalog.delete() path,
backward compatibility with ``catalog=None`` (unchanged default), catalog
upsert failures never blocking the underlying store write, and the four
CatalogSkills bus skills being registered and callable end-to-end.
"""

from __future__ import annotations

import asyncio

import pytest
from khonliang.knowledge.store import KnowledgeStore
from khonliang_bus.testing import AgentTestHarness
from librarian_lib import SelfCatalog

from developer.agent import DeveloperAgent
from developer.bug_store import BugStore
from developer.dogfood_store import DogfoodStore
from developer.fr_store import FRStore
from developer.milestone_store import MilestoneStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def catalog(tmp_path):
    return SelfCatalog(
        db_path=tmp_path / "catalog.db",
        source="developer",
        owner_agent="developer-test",
    )


@pytest.fixture
def knowledge(tmp_path):
    return KnowledgeStore(str(tmp_path / "knowledge.db"))


@pytest.fixture
def fr_store(knowledge, catalog):
    return FRStore(knowledge=knowledge, catalog=catalog)


@pytest.fixture
def bug_store(knowledge, catalog):
    return BugStore(knowledge=knowledge, seed=False, catalog=catalog)


@pytest.fixture
def milestone_store(knowledge, catalog):
    return MilestoneStore(knowledge=knowledge, catalog=catalog)


@pytest.fixture
def dogfood_store(knowledge, catalog):
    return DogfoodStore(knowledge=knowledge, seed=False, catalog=catalog)


# ---------------------------------------------------------------------------
# (a) + (d): per-store mutator writes a matching catalog record; catalog=None
# is a fully backward-compatible no-op.
# ---------------------------------------------------------------------------


class TestFRCataloging:
    def test_promote_writes_catalog_record(self, fr_store, catalog):
        fr = fr_store.promote(
            target="developer", title="catalog fr", description="desc here",
            priority="high",
        )
        rec = catalog.get(fr.project, fr.id)
        assert rec is not None
        assert rec.kind == "fr"
        assert rec.project == fr.project
        assert rec.text == "catalog fr desc here"
        assert rec.facets == {"status": "open", "target": "developer", "priority": "high"}
        assert rec.ref == {"skill": "get_fr_local", "args": {"fr_id": fr.id}}

    def test_update_status_refreshes_catalog_record(self, fr_store, catalog):
        fr = fr_store.promote(target="developer", title="t", description="d")
        fr_store.update_status(fr.id, "planned")
        rec = catalog.get(fr.project, fr.id)
        assert rec.facets["status"] == "planned"

    def test_depends_on_maps_to_depends_on_link(self, fr_store, catalog):
        dep = fr_store.promote(target="developer", title="dep", description="d")
        fr = fr_store.promote(target="developer", title="main", description="d")
        fr_store.set_dependency(fr.id, [dep.id])
        rec = catalog.get(fr.project, fr.id)
        assert len(rec.links) == 1
        link = rec.links[0]
        assert link.rel == "depends_on"
        assert link.target_source == "developer"
        assert link.target_id == dep.id

    def test_merged_from_maps_to_supersedes_link_not_merged_into(self, fr_store, catalog):
        a = fr_store.promote(target="developer", title="a", description="d")
        b = fr_store.promote(target="developer", title="b", description="d")
        merged = fr_store.merge(source_ids=[a.id, b.id], title="merged", description="d")

        merged_rec = catalog.get(merged.project, merged.id)
        supersedes = [l for l in merged_rec.links if l.rel == "supersedes"]
        assert {l.target_id for l in supersedes} == {a.id, b.id}
        assert all(l.target_source == "developer" for l in supersedes)

        # The source (now-merged) FRs must NOT carry a "merged_into" link —
        # only the successor's merged_from -> supersedes side is emitted.
        a_rec = catalog.get(a.project, a.id)
        assert a_rec.links == []

    def test_backing_papers_maps_to_backed_by_link_source_researcher(self, fr_store, catalog):
        fr = fr_store.promote(
            target="developer", title="t", description="d",
            backing_papers=["paper_123"],
        )
        rec = catalog.get(fr.project, fr.id)
        assert len(rec.links) == 1
        link = rec.links[0]
        assert link.rel == "backed_by"
        assert link.target_source == "researcher"
        assert link.target_id == "paper_123"

    def test_catalog_record_updated_at_matches_synced_value_not_stale(
        self, fr_store, catalog, monkeypatch,
    ):
        """Codex R1 on PR #91: KnowledgeStore.add() unconditionally
        overwrites entry.updated_at with time.time() AT WRITE TIME, which
        is later than whatever the FR object held when the IndexRecord
        was first built — a record built before the write (or built once
        and never refreshed) would upsert with a stale updated_at,
        breaking list_since()/updated_after cursors on the catalog side.
        Control time.time() to make the pre-write and write-time values
        provably distinct, then assert the catalog record reflects the
        LATER (synced) value, not the earlier one.
        """
        import itertools
        import time as time_mod

        # Strictly increasing, so every time.time() call in the write path
        # returns a distinct, later value than the one before it — no need
        # to know or predict the exact call count.
        counter = itertools.count(1_000.0, 1.0)
        monkeypatch.setattr(time_mod, "time", lambda: next(counter))

        fr = fr_store.promote(target="developer", title="t", description="d")
        rec = catalog.get(fr.project, fr.id)

        # This is the regression check: before the fix, rec.updated_at held
        # whatever fr.updated_at was AT RECORD-CONSTRUCTION TIME (before
        # KnowledgeStore.add()'s own time.time() call), which — under a
        # strictly increasing clock — is provably earlier than fr.updated_at
        # AFTER the post-write sync. Equality here can only hold if the
        # record was built (or refreshed) using the post-sync value.
        assert rec.updated_at == fr.updated_at

    def test_catalog_none_is_backward_compatible(self, knowledge):
        store = FRStore(knowledge=knowledge)  # no catalog arg
        fr = store.promote(target="developer", title="t", description="d")
        assert store.catalog is None
        assert fr.id  # write succeeded with no catalog wired

    def test_catalog_upsert_failure_does_not_block_write(self, fr_store, catalog, monkeypatch):
        def _boom(record):
            raise RuntimeError("catalog sidecar unavailable")

        monkeypatch.setattr(catalog, "upsert", _boom)
        fr = fr_store.promote(target="developer", title="t", description="d")
        # The underlying FR write must have landed despite the catalog
        # failure — readable via the store's own get().
        assert fr_store.get(fr.id) is not None


class TestBugCataloging:
    def test_file_bug_writes_catalog_record(self, bug_store, catalog):
        bug = bug_store.file_bug(
            target="developer", title="bug title", description="bug desc",
            severity="high",
        )
        rec = catalog.get(bug.project, bug.id)
        assert rec is not None
        assert rec.kind == "bug"
        assert rec.text == "bug title bug desc"
        assert rec.facets == {"status": "open", "target": "developer", "priority": "high"}
        assert rec.ref == {"skill": "get_bug", "args": {"bug_id": bug.id}}

    def test_linked_frs_maps_to_fixes_link(self, bug_store, catalog):
        bug = bug_store.file_bug(target="developer", title="t", description="d")
        bug_store.escalate_to_fr(bug.id, "fr_developer_deadbeef")
        rec = catalog.get(bug.project, bug.id)
        assert len(rec.links) == 1
        link = rec.links[0]
        assert link.rel == "fixes"
        assert link.target_source == "developer"
        assert link.target_id == "fr_developer_deadbeef"

    def test_catalog_none_is_backward_compatible(self, knowledge):
        store = BugStore(knowledge=knowledge, seed=False)  # no catalog arg
        bug = store.file_bug(target="developer", title="t", description="d")
        assert store.catalog is None
        assert bug.id

    def test_catalog_upsert_failure_does_not_block_write(self, bug_store, catalog, monkeypatch):
        monkeypatch.setattr(catalog, "upsert", lambda record: (_ for _ in ()).throw(RuntimeError("boom")))
        bug = bug_store.file_bug(target="developer", title="t", description="d")
        assert bug_store.get_bug(bug.id) is not None


class TestMilestoneCataloging:
    def test_propose_writes_catalog_record(self, milestone_store, catalog):
        work_unit = {"name": "wu", "frs": ["fr_developer_aaaaaaaa"], "targets": ["developer"]}
        ms = milestone_store.propose_from_work_unit(work_unit, target="developer")
        rec = catalog.get(ms.project, ms.id)
        assert rec is not None
        assert rec.kind == "milestone"
        assert rec.text.startswith(ms.title)
        assert rec.facets == {"status": "proposed", "target": "developer"}
        assert rec.ref == {"skill": "get_milestone", "args": {"milestone_id": ms.id}}

    def test_fr_ids_maps_to_bundles_link(self, milestone_store, catalog):
        work_unit = {
            "name": "wu",
            "frs": ["fr_developer_aaaaaaaa", "fr_developer_bbbbbbbb"],
            "targets": ["developer"],
        }
        ms = milestone_store.propose_from_work_unit(work_unit, target="developer")
        rec = catalog.get(ms.project, ms.id)
        bundles = [l for l in rec.links if l.rel == "bundles"]
        assert {l.target_id for l in bundles} == {
            "fr_developer_aaaaaaaa", "fr_developer_bbbbbbbb",
        }
        assert all(l.target_source == "developer" for l in bundles)

    def test_delete_calls_catalog_delete(self, milestone_store, catalog, fr_store):
        fr = fr_store.promote(target="developer", title="t", description="d")
        work_unit = {"name": "wu", "frs": [fr.id], "targets": ["developer"]}
        ms = milestone_store.propose_from_work_unit(work_unit, target="developer")
        assert catalog.get(ms.project, ms.id) is not None

        result = milestone_store.delete(ms.id, fr_store=fr_store)
        assert result["removed"] is True
        assert catalog.get(ms.project, ms.id) is None

    def test_catalog_none_is_backward_compatible(self, knowledge):
        store = MilestoneStore(knowledge=knowledge)  # no catalog arg
        ms = store.propose_from_work_unit(
            {"name": "wu", "frs": ["fr_x"], "targets": ["developer"]}, target="developer",
        )
        assert store.catalog is None
        assert ms.id

    def test_catalog_upsert_failure_does_not_block_write(self, milestone_store, catalog, monkeypatch):
        monkeypatch.setattr(catalog, "upsert", lambda record: (_ for _ in ()).throw(RuntimeError("boom")))
        ms = milestone_store.propose_from_work_unit(
            {"name": "wu", "frs": ["fr_x"], "targets": ["developer"]}, target="developer",
        )
        assert milestone_store.get(ms.id) is not None

    def test_catalog_delete_failure_does_not_block_delete(self, milestone_store, catalog, fr_store, monkeypatch):
        fr = fr_store.promote(target="developer", title="t", description="d")
        ms = milestone_store.propose_from_work_unit(
            {"name": "wu", "frs": [fr.id], "targets": ["developer"]}, target="developer",
        )
        monkeypatch.setattr(catalog, "delete", lambda project, record_id: (_ for _ in ()).throw(RuntimeError("boom")))
        result = milestone_store.delete(ms.id, fr_store=fr_store)
        assert result["removed"] is True
        assert milestone_store.get(ms.id) is None


class TestDogfoodCataloging:
    def test_log_writes_catalog_record(self, dogfood_store, catalog):
        dog = dogfood_store.log_dogfood("something happened", target="developer")
        rec = catalog.get(dog.project, dog.id)
        assert rec is not None
        assert rec.kind == "dogfood"
        assert rec.text == "something happened"
        assert rec.facets == {"status": "observed", "target": "developer"}
        assert rec.ref == {"skill": "get_dogfood", "args": {"dog_id": dog.id}}
        assert rec.links == []

    def test_catalog_none_is_backward_compatible(self, knowledge):
        store = DogfoodStore(knowledge=knowledge, seed=False)  # no catalog arg
        dog = store.log_dogfood("obs")
        assert store.catalog is None
        assert dog.id

    def test_catalog_upsert_failure_does_not_block_write(self, dogfood_store, catalog, monkeypatch):
        monkeypatch.setattr(catalog, "upsert", lambda record: (_ for _ in ()).throw(RuntimeError("boom")))
        dog = dogfood_store.log_dogfood("obs")
        assert dogfood_store.get_dogfood(dog.id) is not None


# ---------------------------------------------------------------------------
# (f) CatalogSkills bus skills — registered and callable end-to-end.
# ---------------------------------------------------------------------------


@pytest.fixture
def harness(temp_config_file):
    return AgentTestHarness(DeveloperAgent, config_path=str(temp_config_file()))


def test_catalog_skills_registered(harness):
    for name in ("catalog_query", "catalog_search", "catalog_stats", "list_since"):
        harness.assert_skill_exists(name)


async def _promote_and_query(harness):
    res = await harness.call(
        "promote_fr",
        {"target": "developer", "title": "e2e", "description": "d", "detail": "full"},
    )
    project = res["project"]
    query = await harness.call("catalog_query", {"project": project, "filters": {"kind": "fr"}})
    return res, query


def test_catalog_query_end_to_end(harness):
    res, query = asyncio.run(_promote_and_query(harness))
    assert query["count"] >= 1
    assert any(row["record_id"] == res["id"] for row in query["rows"])


def test_catalog_search_end_to_end(harness):
    async def run():
        await harness.call(
            "promote_fr",
            {"target": "developer", "title": "searchable unique token", "description": "d"},
        )
        return await harness.call(
            "catalog_search", {"project": "khonliang", "query_text": "unique"},
        )

    result = asyncio.run(run())
    assert result["count"] >= 1


def test_catalog_stats_end_to_end(harness):
    async def run():
        await harness.call(
            "promote_fr", {"target": "developer", "title": "stats fr", "description": "d"},
        )
        return await harness.call("catalog_stats", {})

    stats = asyncio.run(run())
    assert stats["by_kind"].get("fr", 0) >= 1


def test_list_since_end_to_end(harness):
    async def run():
        res = await harness.call(
            "promote_fr",
            {"target": "developer", "title": "since fr", "description": "d", "detail": "full"},
        )
        return res, await harness.call(
            "list_since", {"project": res["project"], "since_ts": 0.0},
        )

    res, result = asyncio.run(run())
    assert result["count"] >= 1
    assert any(row["record_id"] == res["id"] for row in result["rows"])
