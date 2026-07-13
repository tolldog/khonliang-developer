"""Tests for developer.specs — using this very milestone's spec as the fixture.

Acceptance #5, #6, #7 from specs/MS-01/spec.md.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from developer.specs import FR_ID_PATTERN, PathNotAllowedError
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
    # Don't pin the count — the developer repo accumulates specs over
    # time. Just verify the MS-01 entry is discoverable, and that every
    # returned path is a spec.md.
    ms01 = next(
        (s for s in summaries if Path(s.path).parent.name == "MS-01"),
        None,
    )
    assert ms01 is not None, (
        f"MS-01 spec not found among {[Path(s.path).parent.name for s in summaries]}"
    )
    assert Path(ms01.path).name == "spec.md"
    assert ms01.fr == "fr_developer_28a11ce2"
    assert all(Path(s.path).name == "spec.md" for s in summaries)


def test_list_specs_unknown_project_returns_empty(pipeline):
    assert pipeline.specs.list_specs("nonexistent") == []


# ---------------------------------------------------------------------------
# Reverse links (fr_developer_cfe3001c) — list_specs mirrors linked_specs
# ---------------------------------------------------------------------------


def _spec_reader_over(tmp_path, fr_store):
    from developer.config import ProjectConfig
    from developer.specs import SpecReader
    from khonliang_researcher.doc_reader import LocalDocReader
    from developer.specs import FR_ID_PATTERN

    specs_dir = tmp_path / "specs" / "MS-01"
    specs_dir.mkdir(parents=True)
    return SpecReader(
        reader=LocalDocReader(reference_pattern=FR_ID_PATTERN),
        projects={"proj": ProjectConfig(name="proj", repo=tmp_path, specs_dir="specs")},
        fr_store=fr_store,
    ), specs_dir


def test_list_specs_mirrors_linked_spec_onto_fr(pipeline, tmp_path):
    fr = pipeline.frs.promote(target="developer", title="Spec mirror test", description="d")
    reader, specs_dir = _spec_reader_over(tmp_path, pipeline.frs)
    (specs_dir / "spec.md").write_text(
        f"# MS-01\n\n**FR:** `{fr.id}`\n**Status:** approved\n"
    )

    reader.list_specs("proj")

    linked = pipeline.frs.get(fr.id).linked_specs
    assert linked == [{"project": "proj", "path": str(specs_dir / "spec.md"), "section": "MS-01"}]


def test_list_specs_mirror_is_idempotent(pipeline, tmp_path):
    fr = pipeline.frs.promote(target="developer", title="Spec mirror idempotent", description="d")
    reader, specs_dir = _spec_reader_over(tmp_path, pipeline.frs)
    (specs_dir / "spec.md").write_text(f"# MS-01\n\n**FR:** `{fr.id}`\n")

    reader.list_specs("proj")
    reader.list_specs("proj")

    assert len(pipeline.frs.get(fr.id).linked_specs) == 1


def test_list_specs_mirror_is_best_effort_on_unresolvable_fr(tmp_path, pipeline):
    """A spec referencing an FR id that doesn't exist must not raise —
    list_specs already has real results to return."""
    reader, specs_dir = _spec_reader_over(tmp_path, pipeline.frs)
    (specs_dir / "spec.md").write_text("# MS-01\n\n**FR:** `fr_developer_deadbeef`\n")

    summaries = reader.list_specs("proj")
    assert len(summaries) == 1


def test_list_specs_prunes_stale_linked_spec_when_fr_reference_retargeted(pipeline, tmp_path):
    """Codex R5 on PR #93: add_linked_spec is append-only, so a spec
    retargeted from FR A to FR B must have its rescan prune the now-
    stale entry off FR A — otherwise the same spec looks linked from
    two FRs forever even though only one **FR:** line exists."""
    fr_a = pipeline.frs.promote(target="developer", title="Stale A", description="d")
    fr_b = pipeline.frs.promote(target="developer", title="Stale B", description="d")
    reader, specs_dir = _spec_reader_over(tmp_path, pipeline.frs)
    spec_path = specs_dir / "spec.md"
    spec_path.write_text(f"# MS-01\n\n**FR:** `{fr_a.id}`\n")

    reader.list_specs("proj")
    assert pipeline.frs.get(fr_a.id).linked_specs == [
        {"project": "proj", "path": str(spec_path), "section": "MS-01"},
    ]

    spec_path.write_text(f"# MS-01\n\n**FR:** `{fr_b.id}`\n")
    reader.list_specs("proj")

    assert pipeline.frs.get(fr_a.id).linked_specs == []
    assert pipeline.frs.get(fr_b.id).linked_specs == [
        {"project": "proj", "path": str(spec_path), "section": "MS-01"},
    ]


def test_list_specs_does_not_flag_redirected_spec_as_stale(pipeline, tmp_path):
    """Codex R6 on PR #93: add_linked_spec writes through
    follow_redirect=True, so a spec naming a since-merged-away FR id
    lands on the TERMINAL fr. Reconciliation must compare against that
    same terminal id (not the raw scanned id) or it would immediately
    prune the link it just wrote, on every single rescan."""
    source = pipeline.frs.promote(target="developer", title="Redirect source", description="d")
    other = pipeline.frs.promote(target="developer", title="Redirect other", description="d")
    terminal = pipeline.frs.merge(
        source_ids=[source.id, other.id], title="Redirect target", description="d",
    )
    reader, specs_dir = _spec_reader_over(tmp_path, pipeline.frs)
    spec_path = specs_dir / "spec.md"
    spec_path.write_text(f"# MS-01\n\n**FR:** `{source.id}`\n")

    reader.list_specs("proj")
    reader.list_specs("proj")  # second rescan must not undo the first

    assert pipeline.frs.get(terminal.id).linked_specs == [
        {
            "project": "proj", "path": str(spec_path), "section": "MS-01",
            "redirected_from": source.id,
        },
    ]


def test_list_specs_prunes_stale_entry_on_section_rename_same_path(pipeline, tmp_path):
    """Codex R6 on PR #93: the same file, same FR, but a changed
    heading/title must replace the old {path, section} entry, not
    accumulate a second one alongside it."""
    fr = pipeline.frs.promote(target="developer", title="Section rename test", description="d")
    reader, specs_dir = _spec_reader_over(tmp_path, pipeline.frs)
    spec_path = specs_dir / "spec.md"
    spec_path.write_text(f"# Old Title\n\n**FR:** `{fr.id}`\n")

    reader.list_specs("proj")
    assert pipeline.frs.get(fr.id).linked_specs == [
        {"project": "proj", "path": str(spec_path), "section": "Old Title"},
    ]

    spec_path.write_text(f"# New Title\n\n**FR:** `{fr.id}`\n")
    reader.list_specs("proj")

    assert pipeline.frs.get(fr.id).linked_specs == [
        {"project": "proj", "path": str(spec_path), "section": "New Title"},
    ]


def test_list_specs_prunes_stale_linked_spec_when_file_deleted(pipeline, tmp_path):
    fr = pipeline.frs.promote(target="developer", title="Deleted spec test", description="d")
    reader, specs_dir = _spec_reader_over(tmp_path, pipeline.frs)
    spec_path = specs_dir / "spec.md"
    spec_path.write_text(f"# MS-01\n\n**FR:** `{fr.id}`\n")
    reader.list_specs("proj")
    assert pipeline.frs.get(fr.id).linked_specs != []

    spec_path.unlink()
    reader.list_specs("proj")

    assert pipeline.frs.get(fr.id).linked_specs == []


def test_list_specs_prunes_stale_linked_spec_when_specs_root_deleted(pipeline, tmp_path):
    """Codex R11 on PR #93: the whole specs_root directory being
    renamed/deleted (not just one file) must still trigger
    reconciliation — the old early ``return []`` when ``root.exists()``
    is False bypassed the stale-entry cleanup entirely, leaving every
    previously-recorded linked_specs entry for the project stranded
    forever."""
    import shutil

    fr = pipeline.frs.promote(target="developer", title="Whole root deleted test", description="d")
    reader, specs_dir = _spec_reader_over(tmp_path, pipeline.frs)
    spec_path = specs_dir / "spec.md"
    spec_path.write_text(f"# MS-01\n\n**FR:** `{fr.id}`\n")
    reader.list_specs("proj")
    assert pipeline.frs.get(fr.id).linked_specs != []

    shutil.rmtree(tmp_path / "specs")
    summaries = reader.list_specs("proj")

    assert summaries == []
    assert pipeline.frs.get(fr.id).linked_specs == []


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
async def test_traverse_milestone_resolves_frs_from_developer_store(pipeline):
    """FR references resolve from developer's authoritative FR store."""
    from khonliang.knowledge.store import EntryStatus, KnowledgeEntry, Tier

    pipeline.knowledge.add(
        KnowledgeEntry(
            id="fr_developer_28a11ce2",
            tier=Tier.DERIVED,
            title="MS-01 FR",
            content="developer-owned FR",
            source="developer.fr_store",
            scope="development",
            confidence=1.0,
            status=EntryStatus.DISTILLED,
            tags=["fr", "target:developer", "app"],
            metadata={
                "fr_status": "open",
                "priority": "high",
                "concept": "specs",
                "classification": "app",
                "target": "developer",
            },
        )
    )
    chain = await pipeline.specs.traverse_milestone(str(MILESTONE_PATH))
    assert len(chain.frs) >= 1
    fr_ids = [f.fr_id for f in chain.frs]
    assert "fr_developer_28a11ce2" in fr_ids
    target = next(f for f in chain.frs if f.fr_id == "fr_developer_28a11ce2")
    assert target.resolved is True
    assert target.record is not None
    assert target.record.fr_id == "fr_developer_28a11ce2"


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


# ---------------------------------------------------------------------------
# Path-boundary hardening (PR #1 review feedback)
# ---------------------------------------------------------------------------


def test_read_rejects_absolute_path_outside_projects(pipeline):
    """Absolute paths outside any configured project repo must be rejected."""
    with pytest.raises(PathNotAllowedError, match="not under any configured project"):
        pipeline.specs.read("/etc/passwd")


def test_read_rejects_traversal_escape(pipeline, tmp_path):
    """``..``-based escapes from a project repo must be rejected after resolve()."""
    # A path that looks like it's inside a project but resolves outside.
    crafted = REPO_ROOT / "specs" / ".." / ".." / ".." / "etc" / "passwd"
    with pytest.raises(PathNotAllowedError):
        pipeline.specs.read(str(crafted))


def test_read_rejects_non_markdown_extension(pipeline, tmp_path):
    """SpecReader.read should refuse non-.md files even when they're in a project."""
    # Drop a .txt file inside the developer repo (under specs/MS-01/) so it
    # passes the project-boundary check but fails the .md check.
    txt_path = REPO_ROOT / "specs" / "MS-01" / "_test_only.txt"
    try:
        txt_path.write_text("not a markdown file")
        with pytest.raises(PathNotAllowedError, match="\\.md extension"):
            pipeline.specs.read(str(txt_path))
    finally:
        txt_path.unlink(missing_ok=True)


def test_read_accepts_legit_path_inside_project(pipeline):
    """The happy path still works after the boundary check is in place."""
    doc = pipeline.specs.read(str(SPEC_PATH))
    assert doc.text  # actually read the file


@pytest.mark.asyncio
async def test_traverse_milestone_rejects_path_outside_projects(pipeline):
    with pytest.raises(PathNotAllowedError):
        await pipeline.specs.traverse_milestone("/etc/passwd")


@pytest.mark.asyncio
async def test_traverse_milestone_skips_links_outside_projects(pipeline, tmp_path):
    """A crafted milestone with an outside-the-workspace link must drop it.

    Stage a milestone file inside the developer repo whose only link is to
    a markdown file outside any configured project. The traversal should
    succeed but report the outside link as unresolved.
    """
    # Create an "outside" markdown file (in tmp_path, not under any project).
    outside_md = tmp_path / "outside.md"
    outside_md.write_text("# outside\n")

    # Create a crafted milestone inside the developer project.
    crafted_dir = REPO_ROOT / "milestones" / "_TEST_TRAVERSE"
    crafted_dir.mkdir(parents=True, exist_ok=True)
    crafted_path = crafted_dir / "milestone.md"
    try:
        crafted_path.write_text(
            "# Test milestone\n\n"
            f"**FR:** `fr_developer_28a11ce2`\n\n"
            f"See [outside]({outside_md.as_posix()}) for details.\n"
        )
        chain = await pipeline.specs.traverse_milestone(str(crafted_path))
        # The outside link must NOT appear in resolved specs.
        assert all(str(outside_md) not in s.path for s in chain.specs)
        # And it should be reported as unresolved instead.
        assert any("outside" in link or str(outside_md) in link for link in chain.unresolved_links)
    finally:
        crafted_path.unlink(missing_ok=True)
        crafted_dir.rmdir()
