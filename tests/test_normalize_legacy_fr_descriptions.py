"""Tests for the Tier 1 legacy FR description normalization migration
(fr_developer_68b4db12)."""

from __future__ import annotations

import pytest

from khonliang.knowledge.store import KnowledgeStore

from developer.fr_store import FRStore
from developer.migrations.normalize_legacy_fr_descriptions import (
    normalize_legacy_fr_descriptions,
)


_LEGACY_BLOB = (
    '{"target": "autostock", "title": "T", '
    '"description": "Clean text.", "priority": "high"}'
)


@pytest.fixture
def store(tmp_path):
    knowledge = KnowledgeStore(str(tmp_path / "test.db"))
    return FRStore(knowledge=knowledge)


def test_dry_run_reports_matches_without_writing(store):
    fr = store.promote(target="autostock", title="T", description=_LEGACY_BLOB)
    report = normalize_legacy_fr_descriptions(store, apply=False)
    assert report.dry_run is True
    assert report.checked == 1
    assert report.matched == 1
    assert report.normalized == 0
    assert fr.id in report.ids

    unchanged = store.get(fr.id)
    assert unchanged.description == _LEGACY_BLOB
    assert unchanged.raw_description is None


def test_apply_normalizes_matching_frs(store):
    fr = store.promote(target="autostock", title="T", description=_LEGACY_BLOB)
    store.promote(target="developer", title="Clean one", description="already clean")

    report = normalize_legacy_fr_descriptions(store, apply=True)
    assert report.dry_run is False
    assert report.checked == 2
    assert report.matched == 1
    assert report.normalized == 1

    updated = store.get(fr.id)
    assert updated.description == "Clean text."
    assert updated.raw_description == _LEGACY_BLOB
    assert updated.priority == "high"


def test_apply_is_idempotent_across_runs(store):
    store.promote(target="autostock", title="T", description=_LEGACY_BLOB)

    first = normalize_legacy_fr_descriptions(store, apply=True)
    assert first.normalized == 1

    second = normalize_legacy_fr_descriptions(store, apply=True)
    assert second.matched == 0
    assert second.normalized == 0
    assert second.already_normalized == 1


def test_covers_all_projects_and_terminal_statuses(store):
    """Uses project=None + include_all=True so the sweep doesn't miss
    cross-project FRs or ones that already reached a terminal state —
    exactly the shape of the real production backlog."""
    fr = store.promote(
        target="genealogy", title="T", description=_LEGACY_BLOB, project="genealogy",
    )
    store.update_status(fr.id, "in_progress")
    store.update_status(fr.id, "completed")

    report = normalize_legacy_fr_descriptions(store, apply=True)
    assert report.normalized == 1
    updated = store.get(fr.id)
    assert updated.status == "completed"
    assert updated.description == "Clean text."
