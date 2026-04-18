"""Cache-aware session checkpoint and resume helpers.

External LLM sessions should be disposable: checkpoint the durable state,
exit before idle-cache cost cliffs, then resume from a compact briefing.
This module owns the serializable checkpoint shape; bus artifacts can store
the returned dict without pulling raw logs into the prompt.
"""

from __future__ import annotations

import time
from dataclasses import asdict
from typing import Any

from developer.fr_store import FR
from developer.git_client import RepoStatus
from developer.tests_runner import RunResult, format_response as format_test_response


SCHEMA_VERSION = "session-checkpoint/v1"


def build_session_checkpoint(
    *,
    fr: FR | None,
    work_unit: dict[str, Any] | None,
    repo_path: str,
    git_status: RepoStatus,
    head_sha: str,
    pr_ready: dict[str, Any] | None = None,
    test_result: RunResult | None = None,
    tests: dict[str, Any] | None = None,
    evidence: list[dict[str, Any]] | None = None,
    agent_state: dict[str, Any] | None = None,
    next_actions: list[str] | None = None,
    context_tokens: int = 0,
    context_limit: int = 0,
    idle_minutes: float = 0.0,
    now: float | None = None,
) -> dict[str, Any]:
    """Return a compact, artifact-ready checkpoint for a live work session."""
    created_at = float(time.time() if now is None else now)
    changed_files = _changed_files(git_status)
    pr_summary = _pr_summary(pr_ready)
    test_summary = dict(tests or {}) if tests is not None else _test_summary(test_result)
    hygiene = token_hygiene_findings(
        context_tokens=context_tokens,
        context_limit=context_limit,
        idle_minutes=idle_minutes,
    )

    checkpoint = {
        "schema": SCHEMA_VERSION,
        "checkpoint_id": _checkpoint_id(fr, created_at),
        "created_at": created_at,
        "fr": _fr_summary(fr),
        "work_unit": _work_unit_summary(work_unit),
        "repo": {
            "path": repo_path,
            "branch": git_status.branch,
            "head_sha": head_sha,
            "dirty": git_status.is_dirty,
            "ahead": git_status.ahead,
            "behind": git_status.behind,
            "detached": git_status.detached,
            "changed_files": changed_files,
            "untracked": list(git_status.untracked),
            "modified": list(git_status.modified),
            "staged": list(git_status.staged),
            "deleted": list(git_status.deleted),
        },
        "pull_request": pr_summary,
        "tests": test_summary,
        "evidence": list(evidence or []),
        "agent_state": dict(agent_state or {}),
        "token_hygiene": hygiene,
        "next_actions": _next_actions(
            explicit=next_actions or [],
            git_status=git_status,
            pr_summary=pr_summary,
            test_summary=test_summary,
            hygiene=hygiene,
        ),
        "resume_basis": {
            "branch": git_status.branch,
            "head_sha": head_sha,
            "changed_files": changed_files,
            "pr_head_sha": pr_summary.get("head_sha", "") if pr_summary else "",
        },
    }
    return checkpoint


def build_resume_briefing(
    checkpoint: dict[str, Any],
    *,
    current_git_status: RepoStatus,
    current_head_sha: str,
    current_pr_ready: dict[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Reconstruct a concise launch briefing and stale-state warnings."""
    checked_at = float(time.time() if now is None else now)
    stale_reasons = stale_checkpoint_reasons(
        checkpoint,
        current_git_status=current_git_status,
        current_head_sha=current_head_sha,
        current_pr_ready=current_pr_ready,
    )
    fr = checkpoint.get("fr") or {}
    repo = checkpoint.get("repo") or {}
    pr = checkpoint.get("pull_request") or {}
    tests = checkpoint.get("tests") or {}
    actions = list(checkpoint.get("next_actions") or [])
    if stale_reasons:
        actions.insert(0, "refresh checkpoint before relying on stale state")

    lines = [
        f"resume checkpoint: {checkpoint.get('checkpoint_id', '?')}",
        f"fr: {fr.get('id') or '?'} - {fr.get('title') or '?'}",
        f"branch: {current_git_status.branch} @ {current_head_sha[:12]}",
        f"dirty: {current_git_status.is_dirty}",
    ]
    if pr:
        lines.append(
            "pr: "
            f"{pr.get('number') or '?'} {pr.get('state') or '?'} "
            f"action={pr.get('recommended_action') or '?'}"
        )
    if tests:
        lines.append(
            "tests: "
            f"passed={tests.get('passed', 0)} failed={tests.get('failed', 0)} "
            f"errors={tests.get('errors', 0)}"
        )
    if stale_reasons:
        lines.append("stale: " + "; ".join(stale_reasons))
    if actions:
        lines.append("next: " + actions[0])

    return {
        "schema": "session-resume/v1",
        "checkpoint_id": checkpoint.get("checkpoint_id", ""),
        "checked_at": checked_at,
        "stale": bool(stale_reasons),
        "stale_reasons": stale_reasons,
        "fr": fr,
        "repo": {
            "path": repo.get("path", ""),
            "checkpoint_branch": repo.get("branch", ""),
            "current_branch": current_git_status.branch,
            "checkpoint_head_sha": repo.get("head_sha", ""),
            "current_head_sha": current_head_sha,
            "current_changed_files": _changed_files(current_git_status),
        },
        "pull_request": _pr_summary(current_pr_ready) or pr,
        "tests": tests,
        "next_actions": actions,
        "briefing": "\n".join(lines),
    }


def stale_checkpoint_reasons(
    checkpoint: dict[str, Any],
    *,
    current_git_status: RepoStatus,
    current_head_sha: str,
    current_pr_ready: dict[str, Any] | None = None,
) -> list[str]:
    """Return reasons the checkpoint no longer matches repo/PR state."""
    basis = checkpoint.get("resume_basis") or {}
    reasons: list[str] = []
    if basis.get("branch") and basis.get("branch") != current_git_status.branch:
        reasons.append(
            f"branch changed from {basis.get('branch')} to {current_git_status.branch}"
        )
    if basis.get("head_sha") and basis.get("head_sha") != current_head_sha:
        reasons.append("HEAD changed since checkpoint")
    old_files = set(basis.get("changed_files") or [])
    new_files = set(_changed_files(current_git_status))
    if old_files != new_files:
        reasons.append("changed file set differs from checkpoint")
    current_pr = _pr_summary(current_pr_ready)
    if (
        current_pr
        and basis.get("pr_head_sha")
        and basis.get("pr_head_sha") != current_pr.get("head_sha")
    ):
        reasons.append("PR head changed since checkpoint")
    return reasons


def token_hygiene_findings(
    *,
    context_tokens: int = 0,
    context_limit: int = 0,
    idle_minutes: float = 0.0,
) -> list[dict[str, Any]]:
    """Emit cache/context cost hygiene findings from caller-provided metrics."""
    findings: list[dict[str, Any]] = []
    if context_tokens > 0 and context_limit > 0:
        ratio = context_tokens / context_limit
        if ratio >= 0.85:
            severity = "high"
        elif ratio >= 0.70:
            severity = "medium"
        else:
            severity = ""
        if severity:
            findings.append({
                "kind": "context_window_pressure",
                "severity": severity,
                "context_tokens": context_tokens,
                "context_limit": context_limit,
                "ratio": round(ratio, 4),
                "action": "checkpoint and resume from compact artifacts",
            })
    if idle_minutes >= 60:
        severity = "high"
    elif idle_minutes >= 15:
        severity = "medium"
    else:
        severity = ""
    if severity:
        findings.append({
            "kind": "idle_cache_risk",
            "severity": severity,
            "idle_minutes": idle_minutes,
            "action": "exit external LLM session and relaunch from checkpoint",
        })
    return findings


def _checkpoint_id(fr: FR | None, created_at: float) -> str:
    fr_id = fr.id if fr is not None else "work"
    return f"ckpt_{fr_id}_{int(created_at)}"


def _fr_summary(fr: FR | None) -> dict[str, Any]:
    if fr is None:
        return {}
    return {
        "id": fr.id,
        "title": fr.title,
        "target": fr.target,
        "status": fr.status,
        "priority": fr.priority,
        "concept": fr.concept,
        "branch": fr.branch,
        "depends_on": list(fr.depends_on),
        "backing_papers": list(fr.backing_papers),
    }


def _work_unit_summary(work_unit: dict[str, Any] | None) -> dict[str, Any]:
    if not work_unit:
        return {}
    return {
        "name": work_unit.get("name", ""),
        "rank": work_unit.get("rank", 0),
        "targets": list(work_unit.get("targets") or []),
        "concept": work_unit.get("concept", ""),
        "fr_ids": [
            item.get("fr_id", "")
            for item in work_unit.get("frs", [])
            if isinstance(item, dict) and item.get("fr_id")
        ],
    }


def _changed_files(status: RepoStatus) -> list[str]:
    return sorted(
        set(status.untracked + status.modified + status.staged + status.deleted)
    )


def _pr_summary(pr_ready: dict[str, Any] | None) -> dict[str, Any]:
    if not pr_ready or pr_ready.get("error"):
        return {}
    return {
        "state": pr_ready.get("state", ""),
        "recommended_action": pr_ready.get("recommended_action", ""),
        "copilot_verdict": pr_ready.get("copilot_verdict", ""),
        "actionable_comments": pr_ready.get("actionable_comments", 0),
        "merge_state": pr_ready.get("merge_state", ""),
        "head_ref": pr_ready.get("head_ref", ""),
        "head_sha": pr_ready.get("head_sha", ""),
        "url": pr_ready.get("url", ""),
        "latest_copilot_comment": pr_ready.get("latest_copilot_comment", ""),
        "number": pr_ready.get("number", ""),
    }


def _test_summary(result: RunResult | None) -> dict[str, Any]:
    if result is None:
        return {}
    return {
        "returncode": result.returncode,
        "elapsed_s": round(result.elapsed_s, 3),
        "passed": result.passed,
        "failed": result.failed,
        "errors": result.errors,
        "skipped": result.skipped,
        "timed_out": result.timed_out,
        "parsed": result.parsed,
        "digest": format_test_response(result, detail="brief"),
        "command": list(result.command),
        "cwd": result.cwd,
        "failures": [asdict(f) for f in result.failures[:5]],
    }


def _next_actions(
    *,
    explicit: list[str],
    git_status: RepoStatus,
    pr_summary: dict[str, Any],
    test_summary: dict[str, Any],
    hygiene: list[dict[str, Any]],
) -> list[str]:
    actions = list(explicit)
    if git_status.is_dirty:
        actions.append("review, test, and commit working-tree changes")
    if git_status.behind:
        actions.append("pull or rebase before continuing")
    if test_summary:
        if (
            test_summary.get("failed")
            or test_summary.get("errors")
            or test_summary.get("timed_out")
        ):
            actions.append("fix failing tests before requesting review")
    else:
        actions.append("run tests and store a distilled digest")
    if pr_summary:
        action = pr_summary.get("recommended_action")
        if action and action != "merge":
            actions.append(str(action))
    else:
        actions.append("open or update PR when branch is ready")
    if hygiene:
        actions.append("checkpoint durable state before idle or exit")
    return _dedupe(actions)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        clean = str(item).strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        out.append(clean)
    return out
