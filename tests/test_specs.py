"""Tests for developer.specs — using this very milestone's spec as the fixture.

Acceptance #5, #6, #7 from specs/MS-01/spec.md.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from developer.specs import FR_ID_PATTERN
from tests.conftest import MILESTONE_PATH, REPO_ROOT, SPEC_PATH


# ---------------------------------------------------------------------------
# Acceptance #5 — read_spec parses MS-01's own spec file
# ---------------------------------------------------------------------------


def test_read_spec_returns_doc_content(pipeline):
    doc = pipeline.specs.read(str(SPEC_PATH))
    assert doc.path == str(SPEC_PATH)
    assert doc.text  # non-empty
    assert len(doc.sections) > 0


def test_summarize_extracts_bold_metadata(pipeline):
    summary = pipeline.specs.summarize(str(SPEC_PATH))
    assert summary.fr == "fr_developer_28a11ce2"
    assert summary.priority == "high"
    assert summary.class_ == "app"
    assert "approved" in summary.status
    assert "MS-01" in summary.title


def test_read_spec_finds_strict_fr_reference(pipeline):
    """Strict FR pattern must catch the real ID and ignore prose like ``fr_status``."""
    doc = pipeline.specs.read(str(SPEC_PATH))
    assert "fr_developer_28a11ce2" in doc.references
    # Loose pattern would have matched these python identifiers from prose:
    assert "fr_status" not in doc.references
    assert "fr_id" not in doc.references


# ---------------------------------------------------------------------------
# Acceptance #7 — list_specs uses projects[X].specs_dir, not hardcoded path
# ---------------------------------------------------------------------------


def test_list_specs_discovers_via_project_config(pipeline):
    summaries = pipeline.specs.list_specs("developer")
    assert len(summaries) == 1
    assert Path(summaries[0].path).name == "spec.md"
    assert summaries[0].fr == "fr_developer_28a11ce2"


def test_list_specs_unknown_project_returns_empty(pipeline):
    assert pipeline.specs.list_specs("nonexistent") == []


# ---------------------------------------------------------------------------
# Acceptance #6 — traverse_milestone walks milestone → specs → FRs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_traverse_milestone_walks_back_to_specs(pipeline):
    chain = await pipeline.specs.traverse_milestone(str(MILESTONE_PATH))
    assert chain.milestone_path == str(MILESTONE_PATH)
    assert chain.milestone_summary.fr == "fr_developer_28a11ce2"
    # Milestone links to specs/MS-01/spec.md
    assert any(Path(s.path).name == "spec.md" for s in chain.specs)


@pytest.mark.asyncio
async def test_traverse_milestone_resolves_frs_via_stub(pipeline):
    """ResearcherClient is stubbed in MS-01 — every FR comes back unresolved."""
    chain = await pipeline.specs.traverse_milestone(str(MILESTONE_PATH))
    assert len(chain.frs) >= 1
    fr_ids = [f.fr_id for f in chain.frs]
    assert "fr_developer_28a11ce2" in fr_ids
    # Stub guarantees: every FR is unresolved in MS-01.
    assert all(not f.resolved for f in chain.frs)
    assert all(f.record is None for f in chain.frs)


# ---------------------------------------------------------------------------
# FR pattern hygiene
# ---------------------------------------------------------------------------


def test_fr_id_pattern_matches_real_ids():
    import re

    assert re.search(FR_ID_PATTERN, "fr_developer_28a11ce2")
    assert re.search(FR_ID_PATTERN, "fr_researcher_c6b7dca8")
    assert re.search(FR_ID_PATTERN, "fr_researcher-lib_d75b118c")


def test_fr_id_pattern_rejects_prose_identifiers():
    import re

    assert not re.search(FR_ID_PATTERN, "fr_status")
    assert not re.search(FR_ID_PATTERN, "fr_id")
    assert not re.search(FR_ID_PATTERN, "fr_lifecycle")
    assert not re.search(FR_ID_PATTERN, "frontmatter")
