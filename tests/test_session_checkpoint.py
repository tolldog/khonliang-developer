"""Tests for cache-aware session checkpoint helpers."""

from __future__ import annotations

from developer.git_client import RepoStatus
from developer.session_checkpoint import (
    build_resume_briefing,
    build_session_checkpoint,
    stale_checkpoint_reasons,
    token_hygiene_findings,
)


def test_token_hygiene_flags_context_and_idle_risk():
    findings = token_hygiene_findings(
        context_tokens=900_000,
        context_limit=1_000_000,
        idle_minutes=61,
    )

    assert [f["kind"] for f in findings] == [
        "context_window_pressure",
        "idle_cache_risk",
    ]
    assert findings[0]["severity"] == "high"
    assert findings[1]["action"] == "exit external LLM session and relaunch from checkpoint"


def test_checkpoint_shape_includes_repo_actions_and_resume_basis():
    status = RepoStatus(
        branch="fr/test",
        is_dirty=True,
        modified=["developer/agent.py"],
        staged=["tests/test_agent.py"],
        ahead=1,
    )

    checkpoint = build_session_checkpoint(
        fr=None,
        work_unit={"name": "Unit", "rank": 1, "targets": ["developer"], "frs": [{"fr_id": "fr_developer_12345678"}]},
        repo_path="/repo",
        git_status=status,
        head_sha="abc123456789",
        context_tokens=750_000,
        context_limit=1_000_000,
        idle_minutes=20,
        now=1000,
    )

    assert checkpoint["schema"] == "session-checkpoint/v1"
    assert checkpoint["checkpoint_id"] == "ckpt_work_1000"
    assert checkpoint["repo"]["changed_files"] == ["developer/agent.py", "tests/test_agent.py"]
    assert checkpoint["work_unit"]["fr_ids"] == ["fr_developer_12345678"]
    assert checkpoint["resume_basis"]["head_sha"] == "abc123456789"
    assert "review, test, and commit working-tree changes" in checkpoint["next_actions"]
    assert "checkpoint durable state before idle or exit" in checkpoint["next_actions"]


def test_checkpoint_skips_terminal_pr_action_in_next_actions():
    """A merged or closed-unmerged PR's `recommended_action`
    ("no_action" / "reopen_or_drop") is informational only. Filtering
    on the state field rather than the action string prevents the
    terminal verdict from leaking into the queued next-actions list.
    """
    pr_ready_merged = {
        "state": "merged",
        "recommended_action": "no_action",
        "head_sha": "abc",
    }
    checkpoint = build_session_checkpoint(
        fr=None,
        work_unit=None,
        repo_path="/repo",
        git_status=RepoStatus(branch="fr/x", is_dirty=False),
        pr_ready=pr_ready_merged,
        head_sha="abc",
        now=1000,
    )
    assert "no_action" not in checkpoint["next_actions"]

    pr_ready_closed = {
        "state": "closed_unmerged",
        "recommended_action": "reopen_or_drop",
        "head_sha": "abc",
    }
    checkpoint2 = build_session_checkpoint(
        fr=None,
        work_unit=None,
        repo_path="/repo",
        git_status=RepoStatus(branch="fr/x", is_dirty=False),
        pr_ready=pr_ready_closed,
        head_sha="abc",
        now=1000,
    )
    assert "reopen_or_drop" not in checkpoint2["next_actions"]


def test_resume_briefing_marks_stale_checkpoint():
    checkpoint = build_session_checkpoint(
        fr=None,
        work_unit=None,
        repo_path="/repo",
        git_status=RepoStatus(branch="fr/old", is_dirty=False),
        head_sha="oldsha",
        now=1000,
    )
    current = RepoStatus(branch="fr/new", is_dirty=True, modified=["x.py"])

    resume = build_resume_briefing(
        checkpoint,
        current_git_status=current,
        current_head_sha="newsha",
        now=2000,
    )

    assert resume["stale"] is True
    assert "branch changed from fr/old to fr/new" in resume["stale_reasons"]
    assert "HEAD changed since checkpoint" in resume["stale_reasons"]
    assert "changed file set differs from checkpoint" in resume["stale_reasons"]
    assert resume["next_actions"][0] == "refresh checkpoint before relying on stale state"
    assert "stale:" in resume["briefing"]


def test_resume_briefing_dedupes_stale_next_action():
    checkpoint = build_session_checkpoint(
        fr=None,
        work_unit=None,
        repo_path="/repo",
        git_status=RepoStatus(branch="old", is_dirty=False),
        head_sha="oldsha",
        next_actions=["refresh checkpoint before relying on stale state"],
        now=1000,
    )

    resume = build_resume_briefing(
        checkpoint,
        current_git_status=RepoStatus(branch="new", is_dirty=False),
        current_head_sha="oldsha",
        now=2000,
    )

    assert resume["next_actions"].count(
        "refresh checkpoint before relying on stale state"
    ) == 1


def test_stale_checkpoint_allows_matching_state():
    checkpoint = {
        "resume_basis": {
            "branch": "main",
            "head_sha": "same",
            "changed_files": ["a.py"],
        }
    }
    status = RepoStatus(branch="main", is_dirty=True, modified=["a.py"])

    assert stale_checkpoint_reasons(
        checkpoint,
        current_git_status=status,
        current_head_sha="same",
    ) == []
