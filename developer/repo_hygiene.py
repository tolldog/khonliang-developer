"""Repository hygiene and documentation audit helpers.

The workflow is intentionally audit-first. It returns compact, artifact-ready
sections that can be stored by the bus without loading raw file reads into the
LLM context. Apply mode is conservative: it writes the generated audit document
only, leaving code deletion or broad docs rewrites to explicit follow-up work.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from developer.git_client import GitClient, GitClientError, RepoStatus


# Additive fields are allowed within v1; removals or renames require a bump.
SCHEMA_VERSION = "repo-hygiene/v1"
DEFAULT_AUDIT_PATH = "docs/repo-hygiene-audit.md"
TEXT_SUFFIXES = {
    ".cfg",
    ".ini",
    ".json",
    ".md",
    ".py",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
SKIP_DIRS = {".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv", "__pycache__", "build", "dist"}
STALE_TERMS = (
    "MS-01",
    "MS-02",
    "MS-03",
    "MS-04",
    "MS-05",
    "MS-06",
    "stubbed",
    "not built",
    "from_mcp",
    "Go)",
    "direct sibling MCP",
)


@dataclass
class HygieneFinding:
    kind: str
    severity: str
    path: str
    message: str
    action: str
    evidence: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "path": self.path,
            "message": self.message,
            "action": self.action,
            "evidence": self.evidence,
        }


@dataclass
class CleanupAction:
    id: str
    title: str
    mode: str
    risk: str
    paths: list[str]
    rationale: str
    applied: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "mode": self.mode,
            "risk": self.risk,
            "paths": list(self.paths),
            "rationale": self.rationale,
            "applied": self.applied,
        }


@dataclass
class RepoHygieneAudit:
    repo_path: str
    generated_at: float
    repo_id: str
    git_status: dict[str, Any]
    repo_inventory: dict[str, Any]
    deprecated_paths: list[dict[str, Any]] = field(default_factory=list)
    docs_drift: list[dict[str, Any]] = field(default_factory=list)
    cleanup_plan: list[dict[str, Any]] = field(default_factory=list)
    test_plan: list[str] = field(default_factory=list)
    applied_changes: list[dict[str, Any]] = field(default_factory=list)
    artifact_hints: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA_VERSION,
            "repo_path": self.repo_path,
            "repo_id": self.repo_id,
            "generated_at": self.generated_at,
            "git_status": dict(self.git_status),
            "repo_inventory": dict(self.repo_inventory),
            "deprecated_paths": list(self.deprecated_paths),
            "docs_drift": list(self.docs_drift),
            "cleanup_plan": list(self.cleanup_plan),
            "test_plan": list(self.test_plan),
            "applied_changes": list(self.applied_changes),
            "artifact_hints": dict(self.artifact_hints),
            "summary": _summary(
                deprecated_count=len(self.deprecated_paths),
                docs_drift_count=len(self.docs_drift),
                action_count=len(self.cleanup_plan),
                applied_count=len(self.applied_changes),
            ),
        }


def audit_repo_hygiene(
    repo_path: str | Path,
    *,
    include_text_scan: bool = True,
    max_text_files: int = 120,
    now: float | None = None,
) -> RepoHygieneAudit:
    """Inspect a repo and return compact hygiene/doc drift sections."""
    root = Path(repo_path).resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"repo path does not exist or is not a directory: {root}")
    status = _git_status(root)
    inventory = _inventory(root)
    deprecated = _deprecated_paths(root, include_text_scan=include_text_scan, max_text_files=max_text_files)
    docs_drift = _docs_drift(root, inventory)
    cleanup_plan = _cleanup_plan(deprecated=deprecated, docs_drift=docs_drift)
    return RepoHygieneAudit(
        repo_path=str(root),
        generated_at=float(time.time() if now is None else now),
        repo_id=_repo_identifier(root),
        git_status=status,
        repo_inventory=inventory,
        deprecated_paths=[f.to_dict() for f in deprecated],
        docs_drift=[f.to_dict() for f in docs_drift],
        cleanup_plan=[a.to_dict() for a in cleanup_plan],
        test_plan=_test_plan(root, inventory),
        artifact_hints={
            "repo_inventory": "repo_inventory",
            "deprecated_paths": "deprecated_paths",
            "docs_drift": "docs_drift",
            "cleanup_plan": "cleanup_plan",
            "test_plan": "test_plan",
            "applied_changes": "applied_changes",
        },
    )


def apply_repo_hygiene_plan(
    audit: RepoHygieneAudit | dict[str, Any],
    *,
    audit_path: str = DEFAULT_AUDIT_PATH,
    overwrite: bool = True,
) -> dict[str, Any]:
    """Write the generated audit document into the repo.

    This is deliberately conservative. It does not remove files or rewrite
    README/code; it records the plan as a durable docs artifact that a follow-up
    coding session can execute with normal review.
    """
    audit_dict = audit.to_dict() if isinstance(audit, RepoHygieneAudit) else dict(audit)
    root = Path(str(audit_dict["repo_path"])).resolve()
    if Path(audit_path).is_absolute():
        raise ValueError("audit_path must be relative to the repo")
    out_path = (root / audit_path).resolve()
    out_path.relative_to(root)
    if out_path.exists() and not overwrite:
        return {
            "applied_changes": [],
            "skipped": f"{audit_path} already exists",
        }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(format_hygiene_audit_markdown(audit_dict), encoding="utf-8")
    return {
        "applied_changes": [
            {
                "path": audit_path,
                "action": "write_repo_hygiene_audit",
                "description": "Wrote generated repo hygiene audit markdown.",
            }
        ],
        "skipped": "",
    }


def format_hygiene_audit_markdown(audit: dict[str, Any]) -> str:
    """Render an audit dict as compact markdown suitable for repo docs."""
    inventory = audit.get("repo_inventory", {})
    lines = [
        "# Repo Hygiene Audit",
        "",
        f"Generated: {_format_generated_at(audit.get('generated_at'))}",
        f"Repo: `{audit.get('repo_id') or _repo_label(audit.get('repo_path', ''))}`",
        "",
        "## Summary",
        "",
        f"- {audit.get('summary', '')}",
        f"- Python files: {inventory.get('python_files', 0)}",
        f"- Test files: {inventory.get('test_files', 0)}",
        f"- Docs files: {inventory.get('docs_files', 0)}",
        "",
        "## Cleanup Plan",
        "",
    ]
    actions = audit.get("cleanup_plan", [])
    if not actions:
        lines.append("- No cleanup actions proposed.")
    for action in actions:
        paths = ", ".join(f"`{p}`" for p in action.get("paths", [])) or "n/a"
        lines.append(
            f"- **{action.get('id')}** [{action.get('risk')}] "
            f"{action.get('title')} ({paths})"
        )
        lines.append(f"  - {action.get('rationale', '')}")
    lines.extend(["", "## Docs Drift", ""])
    _append_findings(lines, audit.get("docs_drift", []))
    lines.extend(["", "## Deprecated Or Stale Paths", ""])
    _append_findings(lines, audit.get("deprecated_paths", []))
    lines.extend(["", "## Test Plan", ""])
    test_plan = audit.get("test_plan", [])
    if not test_plan:
        lines.append("- No test commands inferred.")
    for command in test_plan:
        lines.append(f"- `{command}`")
    lines.append("")
    return "\n".join(lines)


def _inventory(root: Path) -> dict[str, Any]:
    files = _iter_files(root)
    root_file_names = {p.name for p in root.iterdir() if p.is_file()}
    rels = [p.relative_to(root).as_posix() for p in files]
    py_files = [p for p in rels if p.endswith(".py")]
    test_files = [p for p in rels if p.startswith("tests/") and p.endswith(".py")]
    docs_files = [p for p in rels if p.endswith(".md") or p.startswith("docs/")]
    packages = sorted({
        p.parent.relative_to(root).as_posix()
        for p in files
        if p.name == "__init__.py" and p.parent != root
    })
    return {
        "root_files": sorted(root_file_names),
        "has_readme": "README.md" in root_file_names,
        "has_claude": "CLAUDE.md" in root_file_names,
        "has_pyproject": "pyproject.toml" in root_file_names,
        "has_tests": (root / "tests").is_dir(),
        "has_docs": (root / "docs").is_dir(),
        "has_config_example": "config.example.yaml" in root_file_names or "config.example.yml" in root_file_names,
        "has_local_config": "config.yaml" in root_file_names or "config.yml" in root_file_names,
        "has_mcp_json": ".mcp.json" in root_file_names,
        "python_files": len(py_files),
        "test_files": len(test_files),
        "docs_files": len(docs_files),
        "packages": packages[:50],
        "sample_files": rels[:80],
    }


def _deprecated_paths(root: Path, *, include_text_scan: bool, max_text_files: int) -> list[HygieneFinding]:
    findings: list[HygieneFinding] = []
    for rel in ("go.mod", "go.sum", "bus/testing.py", "initial_code_review.md"):
        path = root / rel
        if path.exists():
            findings.append(HygieneFinding(
                kind="deprecated_path",
                severity="medium",
                path=rel,
                message=f"Potential retired/deprecated path exists: {rel}",
                action="confirm ownership and remove in a focused cleanup PR",
            ))
    if not include_text_scan:
        return findings
    scanned = 0
    for path in _iter_files(root):
        rel = path.relative_to(root).as_posix()
        if path.suffix not in TEXT_SUFFIXES:
            continue
        if path.stat().st_size > 200_000:
            continue
        scanned += 1
        if scanned > max_text_files:
            break
        text = _read_text(path)
        for term in STALE_TERMS:
            if term in text:
                findings.append(HygieneFinding(
                    kind="stale_reference",
                    severity="low",
                    path=rel,
                    message=f"Found stale marker {term!r}.",
                    action="review whether this is historical context or current guidance",
                    evidence=term,
                ))
                break
    return findings


def _docs_drift(root: Path, inventory: dict[str, Any]) -> list[HygieneFinding]:
    findings: list[HygieneFinding] = []
    readme = root / "README.md"
    claude = root / "CLAUDE.md"
    gitignore = root / ".gitignore"
    if not inventory["has_readme"]:
        findings.append(HygieneFinding("docs_missing", "high", "README.md", "README.md is missing.", "add current setup, workflow, and test guidance"))
    else:
        text = _read_text(readme).lower()
        for term, action in (
            ("bus", "document bus-native runtime or explain why it is not used"),
            ("test", "document the primary verification command"),
            ("config", "document local config/example boundaries"),
        ):
            if term not in text:
                findings.append(HygieneFinding("docs_drift", "medium", "README.md", f"README does not mention {term!r}.", action))
    if not inventory["has_claude"]:
        findings.append(HygieneFinding("docs_missing", "medium", "CLAUDE.md", "CLAUDE.md is missing.", "add repo-specific agent guidance"))
    elif "direct mcp" in _read_text(claude).lower() and "bus" not in _read_text(claude).lower():
        findings.append(HygieneFinding("docs_drift", "medium", "CLAUDE.md", "CLAUDE mentions direct MCP without bus context.", "refresh architecture guidance"))
    if inventory["has_local_config"] and not inventory["has_config_example"]:
        findings.append(HygieneFinding("config_hygiene", "high", "config.yaml", "Local config exists without config.example.yaml.", "add example config and gitignore local config"))
    if inventory["has_mcp_json"] and not _gitignore_mentions(gitignore, ".mcp.json"):
        findings.append(HygieneFinding("config_hygiene", "high", ".mcp.json", ".mcp.json exists but is not ignored.", "add .mcp.json to .gitignore"))
    return findings


def _cleanup_plan(
    *,
    deprecated: list[HygieneFinding],
    docs_drift: list[HygieneFinding],
) -> list[CleanupAction]:
    actions: list[CleanupAction] = []
    if docs_drift:
        actions.append(CleanupAction(
            id="docs-refresh",
            title="Refresh README/CLAUDE/config documentation",
            mode="docs",
            risk="low",
            paths=sorted({f.path for f in docs_drift}),
            rationale="Docs drift findings indicate setup or architecture guidance is stale or incomplete.",
        ))
    stale_paths = [f.path for f in deprecated if f.kind == "deprecated_path"]
    if stale_paths:
        actions.append(CleanupAction(
            id="remove-deprecated-paths",
            title="Remove confirmed deprecated paths",
            mode="code",
            risk="medium",
            paths=sorted(set(stale_paths)),
            rationale="Deprecated paths should be removed only after confirming no runtime references remain.",
        ))
    stale_refs = [f.path for f in deprecated if f.kind == "stale_reference"]
    if stale_refs:
        actions.append(CleanupAction(
            id="review-stale-references",
            title="Review stale wording in docs and source comments",
            mode="audit",
            risk="low",
            paths=sorted(set(stale_refs))[:40],
            rationale="Stale terms may be historical, but current guidance should not point at retired milestones or runtimes.",
        ))
    actions.append(CleanupAction(
        id="write-hygiene-artifact",
        title="Write compact repo hygiene artifact",
        mode="docs",
        risk="low",
        paths=[DEFAULT_AUDIT_PATH],
        rationale="Persist the audit so future sessions can resume without rereading raw files.",
    ))
    return actions


def _test_plan(root: Path, inventory: dict[str, Any]) -> list[str]:
    plan: list[str] = []
    if inventory.get("has_pyproject") and inventory.get("has_tests"):
        plan.append("python -m pytest -q")
    if inventory.get("python_files"):
        plan.append("python -m compileall .")
    if (root / "package.json").exists():
        plan.append("npm test")
    return plan


def _format_generated_at(value: Any) -> str:
    """Return stable UTC ISO-8601 text for audit markdown."""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    text = str(value or "").strip()
    return text


def _repo_label(repo_path: Any) -> str:
    """Return a stable fallback repo label without host-local parent paths."""
    if not repo_path:
        return ""
    return Path(str(repo_path)).name


def _repo_identifier(root: Path) -> str:
    """Infer owner/name from origin when available, otherwise repo basename."""
    try:
        remote = GitClient(root).origin_url()
    except GitClientError:
        return root.name
    if not remote:
        return root.name
    match = re.search(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?$", remote.strip())
    if not match:
        return root.name
    return f"{match.group(1)}/{match.group(2)}"


def _git_status(root: Path) -> dict[str, Any]:
    try:
        status = GitClient(root).status()
    except GitClientError as e:
        return {"error": str(e), "is_git_repo": False}
    return _status_dict(status)


def _status_dict(status: RepoStatus) -> dict[str, Any]:
    return {
        "is_git_repo": True,
        "branch": status.branch,
        "is_dirty": status.is_dirty,
        "untracked": list(status.untracked),
        "modified": list(status.modified),
        "staged": list(status.staged),
        "deleted": list(status.deleted),
        "ahead": status.ahead,
        "behind": status.behind,
        "detached": status.detached,
    }


def _iter_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if path.is_file():
            files.append(path)
    return sorted(files, key=lambda p: p.relative_to(root).as_posix())


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _gitignore_mentions(path: Path, needle: str) -> bool:
    if not path.exists():
        return False
    return any(line.strip() == needle for line in _read_text(path).splitlines())


def _append_findings(lines: list[str], findings: list[dict[str, Any]]) -> None:
    if not findings:
        lines.append("- None found.")
        return
    for finding in findings:
        lines.append(
            f"- [{finding.get('severity')}] `{finding.get('path')}`: "
            f"{finding.get('message')} Action: {finding.get('action')}"
        )


def _summary(
    *,
    deprecated_count: int,
    docs_drift_count: int,
    action_count: int,
    applied_count: int,
) -> str:
    action_label = "proposed action" if action_count == 1 else "proposed actions"
    return (
        f"{docs_drift_count} docs drift findings, {deprecated_count} stale/deprecated "
        f"findings, {action_count} {action_label}, {applied_count} applied changes"
    )
