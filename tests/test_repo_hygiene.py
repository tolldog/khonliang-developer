"""Tests for repo-directed hygiene audit helpers."""

from __future__ import annotations

from developer.repo_hygiene import (
    audit_repo_hygiene,
    apply_repo_hygiene_plan,
    format_hygiene_audit_markdown,
)


def test_audit_repo_hygiene_detects_docs_and_config_drift(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# Demo\n\nNo current setup here.\n")
    (repo / "config.yaml").write_text("secret: local\n")
    (repo / ".mcp.json").write_text("{}\n")
    (repo / ".gitignore").write_text("*.pyc\n")
    (repo / "legacy.md").write_text("MS-01 stubbed path\n")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_demo.py").write_text("def test_x():\n    assert True\n")
    (repo / "pyproject.toml").write_text("[project]\nname = 'demo'\n")

    audit = audit_repo_hygiene(repo, now=1000).to_dict()

    assert audit["schema"] == "repo-hygiene/v1"
    assert audit["repo_inventory"]["has_readme"] is True
    assert audit["repo_inventory"]["has_local_config"] is True
    assert audit["repo_inventory"]["packages"] == ["pkg"]
    assert any(f["path"] == "config.yaml" for f in audit["docs_drift"])
    assert any(f["path"] == ".mcp.json" for f in audit["docs_drift"])
    assert any(f["path"] == "legacy.md" for f in audit["deprecated_paths"])
    assert ".venv/bin/python -m pytest -q" in audit["test_plan"]
    assert any(a["id"] == "write-hygiene-artifact" for a in audit["cleanup_plan"])


def test_apply_repo_hygiene_plan_writes_markdown_artifact(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# Demo\n\nbus config test\n")
    audit = audit_repo_hygiene(repo, now=1000)

    result = apply_repo_hygiene_plan(audit, audit_path="docs/hygiene.md")

    assert result["applied_changes"][0]["path"] == "docs/hygiene.md"
    written = repo / "docs" / "hygiene.md"
    assert written.exists()
    assert "# Repo Hygiene Audit" in written.read_text()


def test_apply_repo_hygiene_plan_respects_overwrite_false(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# Demo\n\nbus config test\n")
    existing = repo / "docs" / "hygiene.md"
    existing.parent.mkdir()
    existing.write_text("keep\n")
    audit = audit_repo_hygiene(repo, now=1000)

    result = apply_repo_hygiene_plan(
        audit,
        audit_path="docs/hygiene.md",
        overwrite=False,
    )

    assert result["applied_changes"] == []
    assert result["skipped"] == "docs/hygiene.md already exists"
    assert existing.read_text() == "keep\n"


def test_format_hygiene_audit_markdown_is_compact(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    audit = audit_repo_hygiene(repo, now=1000).to_dict()

    text = format_hygiene_audit_markdown(audit)

    assert text.startswith("# Repo Hygiene Audit")
    assert "## Cleanup Plan" in text
    assert "## Test Plan" in text
