"""Tests for developer.pipeline — store isolation guarantee.

Acceptance #9 from specs/MS-01/spec.md (the spec ↔ milestone review's
required fix): no file handles shared between developer and researcher.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from developer.config import Config
from developer.pipeline import Pipeline, PipelineIsolationError


# ---------------------------------------------------------------------------
# Acceptance #9 — store isolation
# ---------------------------------------------------------------------------


def test_stores_are_isolated(pipeline, loaded_config):
    """All three stores must point at the resolved developer.db path."""
    expected = str(loaded_config.db_path.resolve())
    assert str(Path(pipeline.knowledge.db_path).resolve()) == expected
    assert str(Path(pipeline.triples.db_path).resolve()) == expected
    assert str(Path(pipeline.digest.db_path).resolve()) == expected


def test_stores_never_point_at_researcher_db(pipeline):
    """Spec rev 2 architectural guarantee: developer.db only, never researcher.db."""
    for store in (pipeline.knowledge, pipeline.triples, pipeline.digest):
        assert "researcher.db" not in store.db_path
        assert "khonliang-researcher/data" not in store.db_path


def test_pipeline_refuses_to_start_when_store_path_diverges(loaded_config, monkeypatch):
    """Direct PipelineIsolationError test: simulate a buggy store init."""
    from developer import pipeline as pipeline_mod

    real_kstore = pipeline_mod.KnowledgeStore

    class DivergingKnowledgeStore(real_kstore):
        def __init__(self, db_path):
            super().__init__(db_path)
            # Simulate a downstream bug that swaps the path under us.
            self.db_path = "/tmp/some-other.db"

    monkeypatch.setattr(pipeline_mod, "KnowledgeStore", DivergingKnowledgeStore)
    with pytest.raises(PipelineIsolationError, match="knowledge"):
        Pipeline.from_config(loaded_config)


def test_pipeline_loads_developer_guide(pipeline):
    assert "Developer Pipeline Guide" in pipeline.developer_guide_text
    assert len(pipeline.developer_guide_text) > 500
