"""Tests for the FR data migration from researcher.db to developer.db.

All tests use a fresh temporary researcher-shaped SQLite file and a
fresh developer FRStore. We seed researcher-side records directly
(bypassing researcher's code, so this test doesn't depend on
khonliang-researcher being importable) and run the migration in
both dry-run and apply modes.
"""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

from khonliang.knowledge.store import KnowledgeStore, Tier, EntryStatus

from developer.fr_store import (
    FR_STATUS_ARCHIVED,
    FR_STATUS_COMPLETED,
    FR_STATUS_MERGED,
    FR_STATUS_OPEN,
    FRStore,
)
from developer.migrations.fr_data_from_researcher import (
    MigrationReport,
    _extract_fr_status_and_merge,
    migrate,
)


# ---------------------------------------------------------------------------
# Fixtures + seed helpers
# ---------------------------------------------------------------------------


_SCHEMA = """
CREATE TABLE knowledge (
    id          TEXT PRIMARY KEY,
    tier        INTEGER NOT NULL,
    title       TEXT NOT NULL,
    content     TEXT NOT NULL,
    scope       TEXT NOT NULL DEFAULT 'global',
    source      TEXT DEFAULT '',
    confidence  REAL DEFAULT 1.0,
    status      TEXT NOT NULL DEFAULT 'active',
    tags        TEXT DEFAULT '[]',
    metadata    TEXT DEFAULT '{}',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    access_count INTEGER DEFAULT 0
);
"""


@pytest.fixture
def researcher_db(tmp_path):
    """Build a minimal researcher.db-shaped SQLite file."""
    db_path = tmp_path / "researcher.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return str(db_path)


@pytest.fixture
def developer_store(tmp_path):
    knowledge = KnowledgeStore(str(tmp_path / "developer.db"))
    return FRStore(knowledge=knowledge)


def _seed_fr(db_path: str, *, fr_id: str, title: str, content: str,
             target: str, status_tags: list[str], metadata: dict,
             created_at: float = None):
    """Insert a researcher-style FR entry directly."""
    now = created_at if created_at is not None else time.time()
    tags = ["fr", f"target:{target}", metadata.get("classification", "app")] + status_tags
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO knowledge "
            "(id, tier, title, content, scope, source, confidence, status, tags, metadata, "
            " created_at, updated_at, access_count) "
            "VALUES (?, ?, ?, ?, 'research', 'synergize', 1.0, 'distilled', ?, ?, ?, ?, 0)",
            (fr_id, int(Tier.DERIVED), title, content,
             json.dumps(tags), json.dumps(metadata), now, now),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_capability(db_path: str, *, cap_id: str, target: str, cap_name: str,
                     status: str, fr_id: str):
    now = time.time()
    tags = ["capability", f"target:{target}"]
    metadata = {"target": target, "capability_status": status, "fr_id": fr_id}
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO knowledge "
            "(id, tier, title, content, scope, source, confidence, status, tags, metadata, "
            " created_at, updated_at, access_count) "
            "VALUES (?, ?, ?, ?, 'research', 'capability', 1.0, 'distilled', ?, ?, ?, ?, 0)",
            (cap_id, int(Tier.DERIVED), cap_name, "", json.dumps(tags),
             json.dumps(metadata), now, now),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tag → status translation
# ---------------------------------------------------------------------------


def test_extract_status_default_open():
    assert _extract_fr_status_and_merge(["fr", "target:developer", "app"]) == (FR_STATUS_OPEN, None)


def test_extract_status_archived():
    assert _extract_fr_status_and_merge(["fr", "fr:archived"]) == (FR_STATUS_ARCHIVED, None)


def test_extract_status_completed():
    assert _extract_fr_status_and_merge(["fr", "fr:completed"]) == (FR_STATUS_COMPLETED, None)


def test_extract_status_merged_with_redirect():
    status, target = _extract_fr_status_and_merge(["fr", "fr:merged_into:fr_developer_abc"])
    assert status == FR_STATUS_MERGED
    assert target == "fr_developer_abc"


def test_extract_status_ignores_irrelevant_tags():
    status, target = _extract_fr_status_and_merge([
        "fr", "target:developer", "app", "fr:archived", "other:tag",
    ])
    assert status == FR_STATUS_ARCHIVED
    assert target is None


# ---------------------------------------------------------------------------
# Migration happy path
# ---------------------------------------------------------------------------


def test_dry_run_reports_counts_but_writes_nothing(researcher_db, developer_store):
    _seed_fr(researcher_db, fr_id="fr_developer_aaaaaaaa", title="A", content="a",
             target="developer", status_tags=[],
             metadata={"concept": "x", "classification": "app",
                       "priority": "medium", "target": "developer"})
    _seed_fr(researcher_db, fr_id="fr_developer_bbbbbbbb", title="B", content="b",
             target="developer", status_tags=["fr:archived"],
             metadata={"concept": "y", "classification": "app",
                       "priority": "low", "target": "developer"})

    report = migrate(researcher_db, developer_store, apply=False)

    assert report.dry_run is True
    assert report.frs_found == 2
    assert report.frs_migrated == 2  # would migrate
    assert report.frs_already_present == 0
    # Target store has none
    assert developer_store.knowledge.get("fr_developer_aaaaaaaa") is None
    assert developer_store.knowledge.get("fr_developer_bbbbbbbb") is None


def test_apply_writes_records(researcher_db, developer_store):
    _seed_fr(researcher_db, fr_id="fr_developer_aaaaaaaa", title="Feature A",
             content="description of A", target="developer", status_tags=[],
             metadata={"concept": "x", "classification": "library",
                       "priority": "high", "target": "developer",
                       "backing_papers": ["paper-a"]})
    report = migrate(researcher_db, developer_store, apply=True)
    assert report.frs_migrated == 1

    fr = developer_store.get("fr_developer_aaaaaaaa")
    assert fr is not None
    assert fr.title == "Feature A"
    assert fr.description == "description of A"
    assert fr.status == FR_STATUS_OPEN
    assert fr.priority == "high"
    assert fr.classification == "library"
    assert fr.backing_papers == ["paper-a"]


def test_apply_preserves_status_from_tags(researcher_db, developer_store):
    """Researcher's status-in-tags is translated to metadata.fr_status."""
    _seed_fr(researcher_db, fr_id="fr_developer_archived", title="Arch",
             content="d", target="developer", status_tags=["fr:archived"],
             metadata={"concept": "x", "classification": "app",
                       "priority": "low", "target": "developer"})
    _seed_fr(researcher_db, fr_id="fr_developer_completed", title="Comp",
             content="d", target="developer", status_tags=["fr:completed"],
             metadata={"concept": "y", "classification": "app",
                       "priority": "low", "target": "developer"})

    migrate(researcher_db, developer_store, apply=True)

    # get() follows redirects; use raw read
    arch = developer_store.get("fr_developer_archived", follow_redirect=False)
    assert arch.status == FR_STATUS_ARCHIVED
    comp = developer_store.get("fr_developer_completed", follow_redirect=False)
    assert comp.status == FR_STATUS_COMPLETED


def test_apply_preserves_merge_redirect(researcher_db, developer_store):
    """Merged FRs on researcher side get their redirect chain preserved.

    After migration, developer's FRStore.get() should walk the chain
    and return the terminal FR with a redirected_from hint.
    """
    # Seed target first
    _seed_fr(researcher_db, fr_id="fr_developer_target", title="Target",
             content="d", target="developer", status_tags=[],
             metadata={"concept": "tgt", "classification": "app",
                       "priority": "medium", "target": "developer"})
    # Seed source merged into target
    _seed_fr(researcher_db, fr_id="fr_developer_source",
             title="Source", content="s", target="developer",
             status_tags=["fr:merged_into:fr_developer_target"],
             metadata={"concept": "src", "classification": "app",
                       "priority": "medium", "target": "developer"})

    migrate(researcher_db, developer_store, apply=True)

    # Lookup with follow_redirect=True (default) should resolve to target
    resolved = developer_store.get("fr_developer_source")
    assert resolved.id == "fr_developer_target"
    assert resolved.redirected_from == "fr_developer_source"

    # And the source's raw status is 'merged'
    raw = developer_store.get("fr_developer_source", follow_redirect=False)
    assert raw.status == FR_STATUS_MERGED
    assert raw.merged_into == "fr_developer_target"


def test_idempotent_second_run_is_no_op(researcher_db, developer_store):
    _seed_fr(researcher_db, fr_id="fr_developer_iii", title="Idemp",
             content="d", target="developer", status_tags=[],
             metadata={"concept": "c", "classification": "app",
                       "priority": "medium", "target": "developer"})
    # First apply
    r1 = migrate(researcher_db, developer_store, apply=True)
    assert r1.frs_migrated == 1
    # Second apply: already present, no-op
    r2 = migrate(researcher_db, developer_store, apply=True)
    assert r2.frs_found == 1
    assert r2.frs_migrated == 0
    assert r2.frs_already_present == 1


def test_idempotent_flags_content_mismatch_without_overwriting(researcher_db, developer_store):
    _seed_fr(researcher_db, fr_id="fr_developer_mm", title="Original",
             content="original content", target="developer", status_tags=[],
             metadata={"concept": "c", "classification": "app",
                       "priority": "medium", "target": "developer"})
    # First apply
    migrate(researcher_db, developer_store, apply=True)

    # Modify target's copy so content diverges
    existing = developer_store.knowledge.get("fr_developer_mm")
    existing.content = "locally edited on developer"
    developer_store.knowledge.add(existing)

    # Re-run migration: must NOT overwrite, should add to skipped
    r = migrate(researcher_db, developer_store, apply=True)
    assert r.frs_migrated == 0
    assert r.frs_already_present == 0
    assert len(r.frs_skipped) == 1
    assert r.frs_skipped[0]["id"] == "fr_developer_mm"
    # Local edit preserved
    after = developer_store.get("fr_developer_mm")
    assert after.description == "locally edited on developer"


def test_migrates_capability_entries(researcher_db, developer_store):
    _seed_capability(researcher_db, cap_id="capability_developer_abc",
                     target="developer", cap_name="Test capability",
                     status="exists", fr_id="fr_developer_something")

    r = migrate(researcher_db, developer_store, apply=True)
    assert r.capabilities_found == 1
    assert r.capabilities_migrated == 1

    caps = developer_store.capabilities_for("developer")
    assert len(caps) == 1
    assert caps[0]["name"] == "Test capability"
    assert caps[0]["status"] == "exists"


def test_dry_run_does_not_write_capabilities(researcher_db, developer_store):
    _seed_capability(researcher_db, cap_id="capability_developer_xyz",
                     target="developer", cap_name="Cap X",
                     status="planned", fr_id="fr_developer_x")
    r = migrate(researcher_db, developer_store, apply=False)
    assert r.capabilities_found == 1
    assert r.capabilities_migrated == 1  # would migrate
    assert developer_store.knowledge.get("capability_developer_xyz") is None


def test_ignores_non_fr_derived_entries(researcher_db, developer_store):
    """Researcher's non-FR Tier.DERIVED entries (paper summaries, etc.) are skipped."""
    conn = sqlite3.connect(researcher_db)
    try:
        now = time.time()
        # A paper summary — DERIVED tier but no 'fr' tag
        conn.execute(
            "INSERT INTO knowledge "
            "(id, tier, title, content, scope, status, tags, metadata, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'research', 'distilled', ?, ?, ?, ?)",
            ("paper_summary_1", int(Tier.DERIVED), "A paper", "summary content",
             json.dumps(["summary"]), json.dumps({}), now, now),
        )
        conn.commit()
    finally:
        conn.close()

    r = migrate(researcher_db, developer_store, apply=True)
    assert r.frs_found == 0
    assert developer_store.knowledge.get("paper_summary_1") is None


def test_preserves_extra_metadata(researcher_db, developer_store):
    """Researcher-specific metadata fields (e.g. synergies) survive the migration."""
    _seed_fr(researcher_db, fr_id="fr_developer_syn", title="S",
             content="d", target="developer", status_tags=[],
             metadata={"concept": "c", "classification": "app",
                       "priority": "medium", "target": "developer",
                       "synergies": ["autostock benefits from X"]})
    migrate(researcher_db, developer_store, apply=True)

    entry = developer_store.knowledge.get("fr_developer_syn")
    assert entry is not None
    assert entry.metadata.get("synergies") == ["autostock benefits from X"]


def test_derives_target_from_id_when_metadata_missing(researcher_db, developer_store):
    """Some legacy researcher FRs don't have target in metadata — derive from id."""
    conn = sqlite3.connect(researcher_db)
    try:
        now = time.time()
        conn.execute(
            "INSERT INTO knowledge "
            "(id, tier, title, content, scope, status, tags, metadata, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'research', 'distilled', ?, ?, ?, ?)",
            ("fr_autostock_legacy", int(Tier.DERIVED), "Legacy", "content",
             json.dumps(["fr", "app"]), json.dumps({"concept": "x"}), now, now),
        )
        conn.commit()
    finally:
        conn.close()

    migrate(researcher_db, developer_store, apply=True)
    fr = developer_store.get("fr_autostock_legacy")
    assert fr is not None
    assert fr.target == "autostock"  # derived from id


def test_report_summary_has_counts(researcher_db, developer_store):
    _seed_fr(researcher_db, fr_id="fr_developer_aaa", title="A", content="a",
             target="developer", status_tags=[],
             metadata={"concept": "x", "classification": "app",
                       "priority": "medium", "target": "developer"})
    r = migrate(researcher_db, developer_store, apply=False)
    text = r.summary()
    assert "DRY-RUN" in text
    assert "found=1" in text
