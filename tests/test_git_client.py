"""Tests for developer's git client.

Mix of:
1. **Unit tests** over constructor / typed errors / lazy loading — don't
   touch a real repo
2. **Real-repo tests** using a ``git.Repo.init()`` temp dir — exercise
   the full happy path + error branches against a real working tree.
   No network (no fetch/pull/push to real remotes); those paths are
   covered via monkey-patched stubs.

Keeping it offline: we never push to real remotes in tests. Network
behavior is tested by stubbing the underlying ``git.Repo`` methods.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from developer.git_client import (
    GitBranch,
    GitClient,
    GitClientError,
    GitCommit,
    GitConflictError,
    GitDestructiveError,
    GitGuardError,
    GitNotFoundError,
    GitUncommittedError,
    GitUpstreamError,
    RepoStatus,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _run(*args: str, cwd: Path) -> None:
    """Run a git command via subprocess for test setup (not the code we test)."""
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@pytest.fixture
def repo_path(tmp_path):
    """Initialize a fresh git repo with one commit + test identity."""
    _run("init", "-b", "main", cwd=tmp_path)
    _run("config", "user.email", "test@example.com", cwd=tmp_path)
    _run("config", "user.name", "Test", cwd=tmp_path)
    (tmp_path / "a.txt").write_text("first\n")
    _run("add", "a.txt", cwd=tmp_path)
    _run("commit", "-m", "initial", cwd=tmp_path)
    return tmp_path


@pytest.fixture
def client(repo_path):
    return GitClient(repo_path)


# ---------------------------------------------------------------------------
# Lazy loading + error mapping
# ---------------------------------------------------------------------------


def test_constructor_does_not_load_repo(tmp_path):
    c = GitClient(tmp_path)
    assert c._repo is None


def test_nonexistent_path_raises_not_found(tmp_path):
    c = GitClient(tmp_path / "does-not-exist")
    with pytest.raises(GitNotFoundError, match="does not exist"):
        c.status()


def test_non_git_directory_raises_not_found(tmp_path):
    c = GitClient(tmp_path)  # tmp_path is a directory but not a git repo
    with pytest.raises(GitNotFoundError, match="not a git repository"):
        c.status()


# ---------------------------------------------------------------------------
# status + current_branch
# ---------------------------------------------------------------------------


def test_status_clean_repo(client, repo_path):
    s = client.status()
    assert s.branch == "main"
    assert not s.is_dirty
    assert s.untracked == []
    assert s.modified == []
    assert s.staged == []
    assert s.ahead == 0
    assert s.behind == 0
    assert not s.detached


def test_status_reports_untracked(client, repo_path):
    (repo_path / "new.txt").write_text("hi\n")
    s = client.status()
    assert s.is_dirty
    assert "new.txt" in s.untracked


def test_status_reports_modified(client, repo_path):
    (repo_path / "a.txt").write_text("changed\n")
    s = client.status()
    assert s.is_dirty
    assert "a.txt" in s.modified


def test_status_reports_staged(client, repo_path):
    (repo_path / "b.txt").write_text("new\n")
    _run("add", "b.txt", cwd=repo_path)
    s = client.status()
    assert s.is_dirty
    assert "b.txt" in s.staged


def test_status_reports_deleted(client, repo_path):
    _run("rm", "a.txt", cwd=repo_path)
    s = client.status()
    assert s.is_dirty
    assert "a.txt" in s.deleted


def test_current_branch_reports_branch_name(client):
    assert client.current_branch() == "main"


def test_current_branch_detached(client, repo_path):
    sha = client.rev_parse("HEAD")
    _run("checkout", sha, cwd=repo_path)
    assert client.current_branch() == "HEAD"
    assert client.status().detached


def test_origin_url_returns_configured_origin(client, repo_path):
    _run("remote", "add", "origin", "git@github.com:owner/repo.git", cwd=repo_path)

    assert client.origin_url() == "git@github.com:owner/repo.git"


def test_origin_url_returns_none_without_origin(client):
    assert client.origin_url() is None


# ---------------------------------------------------------------------------
# list_branches + log + show + rev_parse + diff
# ---------------------------------------------------------------------------


def test_list_branches_local_only(client, repo_path):
    _run("branch", "feat/x", cwd=repo_path)
    branches = client.list_branches(local=True, remote=False)
    names = {b.name for b in branches}
    assert "main" in names
    assert "feat/x" in names
    assert all(not b.is_remote for b in branches)


def test_list_branches_marks_current(client, repo_path):
    _run("branch", "feat/y", cwd=repo_path)
    branches = client.list_branches(local=True)
    current = [b for b in branches if b.is_current]
    assert len(current) == 1
    assert current[0].name == "main"


def test_log_returns_recent_commits(client, repo_path):
    (repo_path / "c.txt").write_text("c\n")
    _run("add", "c.txt", cwd=repo_path)
    _run("commit", "-m", "second", cwd=repo_path)
    commits = client.log(limit=5)
    assert len(commits) == 2
    assert commits[0].message == "second"
    assert commits[1].message == "initial"
    assert commits[0].short_sha != ""
    assert "<test@example.com>" in commits[0].author


def test_show_returns_the_commit(client):
    c = client.show("HEAD")
    assert c.message == "initial"
    assert len(c.sha) == 40


def test_show_unknown_ref_raises_not_found(client):
    with pytest.raises(GitNotFoundError):
        client.show("totally-not-a-ref")


def test_rev_parse_returns_full_sha(client):
    sha = client.rev_parse("HEAD")
    assert len(sha) == 40


def test_rev_parse_unknown_ref_raises_not_found(client):
    with pytest.raises(GitNotFoundError):
        client.rev_parse("totally-not-a-ref")


def test_diff_working_tree_vs_head(client, repo_path):
    (repo_path / "a.txt").write_text("changed\n")
    diff = client.diff("HEAD")
    assert "a.txt" in diff
    assert "-first" in diff
    assert "+changed" in diff


# ---------------------------------------------------------------------------
# checkout + create_branch + delete_branch
# ---------------------------------------------------------------------------


def test_create_branch_does_not_switch(client, repo_path):
    client.create_branch("feat/new")
    assert client.current_branch() == "main"
    branches = {b.name for b in client.list_branches()}
    assert "feat/new" in branches


def test_create_branch_duplicate_raises(client, repo_path):
    client.create_branch("feat/dup")
    with pytest.raises(GitClientError, match="already exists"):
        client.create_branch("feat/dup")


def test_checkout_switches_branch(client, repo_path):
    client.create_branch("feat/switch")
    client.checkout("feat/switch")
    assert client.current_branch() == "feat/switch"


def test_checkout_new_branch_creates_and_switches(client, repo_path):
    client.checkout("feat/shortcut", new_branch=True)
    assert client.current_branch() == "feat/shortcut"


def test_checkout_dirty_tree_raises_uncommitted(client, repo_path):
    (repo_path / "a.txt").write_text("dirty\n")
    client.create_branch("feat/x")
    with pytest.raises(GitUncommittedError):
        client.checkout("feat/x")


def test_checkout_untracked_files_raises_uncommitted(client, repo_path):
    """Matches git's own safety behavior — untracked files could be
    clobbered on switch, so refuse."""
    (repo_path / "new.txt").write_text("untracked\n")
    client.create_branch("feat/y")
    with pytest.raises(GitUncommittedError, match="untracked"):
        client.checkout("feat/y")


def test_checkout_unknown_ref_raises_not_found(client):
    with pytest.raises(GitNotFoundError):
        client.checkout("totally-not-a-ref")


def test_delete_branch_happy_path(client, repo_path):
    client.create_branch("feat/gone")
    client.delete_branch("feat/gone")
    assert "feat/gone" not in {b.name for b in client.list_branches()}


def test_delete_branch_unknown_raises_not_found(client):
    with pytest.raises(GitNotFoundError):
        client.delete_branch("feat/never-existed")


def test_delete_branch_current_raises(client):
    with pytest.raises(GitClientError, match="current branch"):
        client.delete_branch("main")


def test_delete_branch_unmerged_refuses_without_force(client, repo_path):
    client.checkout("feat/unmerged", new_branch=True)
    (repo_path / "new.txt").write_text("unmerged content\n")
    _run("add", "new.txt", cwd=repo_path)
    _run("commit", "-m", "unmerged", cwd=repo_path)
    client.checkout("main")
    with pytest.raises(GitClientError, match="not fully merged"):
        client.delete_branch("feat/unmerged")


def test_delete_branch_force_removes_unmerged(client, repo_path):
    client.checkout("feat/unmerged2", new_branch=True)
    (repo_path / "n.txt").write_text("x\n")
    _run("add", "n.txt", cwd=repo_path)
    _run("commit", "-m", "x", cwd=repo_path)
    client.checkout("main")
    client.delete_branch("feat/unmerged2", force=True)
    assert "feat/unmerged2" not in {b.name for b in client.list_branches()}


# ---------------------------------------------------------------------------
# stage + unstage + commit
# ---------------------------------------------------------------------------


def test_stage_and_commit(client, repo_path):
    (repo_path / "staged.txt").write_text("x\n")
    client.stage(["staged.txt"])
    s = client.status()
    assert "staged.txt" in s.staged

    commit = client.commit("feat: add staged.txt")
    assert commit.message == "feat: add staged.txt"
    assert client.status().staged == []


def test_unstage(client, repo_path):
    (repo_path / "s.txt").write_text("y\n")
    client.stage(["s.txt"])
    client.unstage(["s.txt"])
    s = client.status()
    assert "s.txt" not in s.staged
    assert "s.txt" in s.untracked


def test_commit_empty_message_raises(client, repo_path):
    (repo_path / "x.txt").write_text("x\n")
    client.stage(["x.txt"])
    with pytest.raises(GitClientError, match="non-empty message"):
        client.commit("")
    with pytest.raises(GitClientError, match="non-empty message"):
        client.commit("   ")


def test_commit_nothing_staged_raises(client):
    with pytest.raises(GitClientError, match="no staged changes"):
        client.commit("nothing")


def test_commit_with_co_authors_appends_trailers(client, repo_path):
    (repo_path / "co.txt").write_text("x\n")
    client.stage(["co.txt"])
    commit = client.commit(
        "feat: co-authored",
        co_authors=["Claude <claude@example.com>"],
    )
    assert "Co-Authored-By: Claude <claude@example.com>" in commit.full_message


# ---------------------------------------------------------------------------
# Stubbed network ops (push / pull / fetch)
#
# GitPython's `git.Git` uses __getattr__ with slots-like semantics, so we
# can't setattr on an instance. Patch at the class level via unittest.mock.
# ---------------------------------------------------------------------------


from unittest.mock import patch

from git.cmd import Git as _GitCmd


def test_push_without_force_returns_structured_shape(client, repo_path):
    calls: list[tuple[tuple, dict]] = []
    def _fake_push(self_git, *args, **kwargs):
        calls.append((args, kwargs))
        return ""
    with patch.object(_GitCmd, "push", _fake_push, create=True):
        # repo_path's default branch is main; the protected-branch guard
        # would refuse this push without allow_main=True, but this test's
        # concern is the structured-shape return, not the guard.
        result = client.push(remote="origin", branch="main", allow_main=True)
    assert result["remote"] == "origin"
    assert result["branch"] == "main"
    assert result["force"] is False


def test_push_force_is_logged_at_info(client, repo_path, caplog):
    import logging as _l
    def _fake_push(self_git, *args, **kwargs):
        return ""
    with patch.object(_GitCmd, "push", _fake_push, create=True):
        with caplog.at_level(_l.INFO, logger="developer.git_client"):
            result = client.push(force=True, allow_main=True)
    assert result["force"] is True
    # Force=True must hit the audit log
    assert any("force=True" in rec.message for rec in caplog.records)


def test_push_rejected_raises_upstream_error(client, repo_path):
    from git.exc import GitCommandError

    def _fake_push(self_git, *args, **kwargs):
        raise GitCommandError(
            ["git", "push"], 1, stderr=b"error: failed to push some refs to 'origin'\n"
            b"hint: Updates were rejected because the tip of your current branch is behind\n"
            b"fatal: non-fast-forward\n",
        )
    with patch.object(_GitCmd, "push", _fake_push, create=True):
        with pytest.raises(GitUpstreamError, match="non-fast-forward"):
            client.push(allow_main=True)


def test_pull_ff_only_is_default(client, repo_path):
    calls: list[tuple] = []
    def _fake_pull(self_git, *args, **kwargs):
        calls.append(args)
        return ""
    with patch.object(_GitCmd, "pull", _fake_pull, create=True):
        client.pull(remote="origin")
    assert "--ff-only" in calls[0]


def test_pull_remote_only_uses_current_branch(client, repo_path):
    """With remote but no branch, pull my current branch from remote —
    NOT git pull <remote> alone (which would merge remote's HEAD)."""
    calls: list[tuple] = []
    def _fake_pull(self_git, *args, **kwargs):
        calls.append(args)
        return ""
    with patch.object(_GitCmd, "pull", _fake_pull, create=True):
        client.pull(remote="origin")
    # Args should include both 'origin' and 'main' (the current branch)
    assert "origin" in calls[0]
    assert "main" in calls[0]


def test_pull_no_args_follows_upstream(client, repo_path):
    """With neither remote nor branch, rely on git's configured upstream."""
    calls: list[tuple] = []
    def _fake_pull(self_git, *args, **kwargs):
        calls.append(args)
        return ""
    with patch.object(_GitCmd, "pull", _fake_pull, create=True):
        client.pull()
    # Only --ff-only should be in args; no remote/branch
    assert calls[0] == ("--ff-only",)


def test_pull_detached_head_requires_branch(client, repo_path):
    sha = client.rev_parse("HEAD")
    _run("checkout", sha, cwd=repo_path)
    with pytest.raises(GitClientError, match="detached HEAD"):
        client.pull(remote="origin")


def test_pull_conflict_raises_conflict_error(client, repo_path):
    from git.exc import GitCommandError

    def _fake_pull(self_git, *args, **kwargs):
        raise GitCommandError(
            ["git", "pull"], 1, stderr=b"CONFLICT (content): Merge conflict in a.txt\n",
        )
    with patch.object(_GitCmd, "pull", _fake_pull, create=True):
        with pytest.raises(GitConflictError):
            client.pull(ff_only=False)


def test_fetch_unknown_remote_raises_not_found(client):
    with pytest.raises(GitNotFoundError, match="not configured"):
        client.fetch(remote="never-configured")


# ---------------------------------------------------------------------------
# Guards (fr_developer_44fc7dde)
# ---------------------------------------------------------------------------


def test_stage_refuses_empty_paths(client):
    with pytest.raises(GitGuardError, match="explicit paths"):
        client.stage([])


def test_stage_refuses_dot_wildcard(client):
    with pytest.raises(GitGuardError, match="wildcard"):
        client.stage(["."])


def test_stage_refuses_dash_a_unconditionally(client):
    """``-A`` / ``--all`` / ``-u`` / ``--update`` are CLI flags, not
    pathspecs — passing them to ``repo.index.add`` would silently
    misbehave. Refuse even with ``allow_all=True``; the recovery
    path is ``stage(["."], allow_all=True)``.
    """
    with pytest.raises(GitGuardError, match="CLI flag tokens"):
        client.stage(["-A"])
    with pytest.raises(GitGuardError, match="CLI flag tokens"):
        client.stage(["-A"], allow_all=True)
    with pytest.raises(GitGuardError, match="CLI flag tokens"):
        client.stage(["--all"], allow_all=True)
    with pytest.raises(GitGuardError, match="CLI flag tokens"):
        client.stage(["-u"], allow_all=True)


def test_stage_allow_all_accepts_dot_pathspec(client, repo_path):
    """``allow_all=True`` is the explicit opt-in for callers who really
    do want to capture every change in the working tree. Only ``.``
    is the supported bulk-add pathspec.
    """
    (repo_path / "b.txt").write_text("hi\n")
    staged = client.stage(["."], allow_all=True)
    assert staged == ["."]
    # Bulk add captured the new file (status returns paths with ./ prefix).
    assert any("b.txt" in p for p in client.status().staged)


def test_commit_refuses_branch_hint_mismatch(client, repo_path):
    """branch_hint is the strict guard against wrong-cwd commits — if
    the message names branch X but the cwd is on branch Y, refuse so
    the misroute fails fast instead of landing on Y.
    """
    (repo_path / "b.txt").write_text("hi\n")
    client.stage(["b.txt"])
    with pytest.raises(GitGuardError, match="branch_hint mismatch"):
        client.commit("subject", branch_hint="some-other-branch")


def test_commit_branch_hint_match_succeeds(client, repo_path):
    (repo_path / "b.txt").write_text("hi\n")
    client.stage(["b.txt"])
    commit = client.commit("subject", branch_hint="main")
    assert commit.message == "subject"


def test_commit_no_branch_hint_skips_check(client, repo_path):
    """Empty branch_hint preserves the legacy behavior — callers that
    haven't migrated still work.
    """
    (repo_path / "b.txt").write_text("hi\n")
    client.stage(["b.txt"])
    commit = client.commit("subject")
    assert commit.message == "subject"


def test_push_refuses_main_without_allow_main(client, repo_path):
    """The headline guard: ``main`` is the protected branch, refuse-by-
    default. The error message must point at the recovery path
    (feature branch + PR).
    """
    with pytest.raises(GitGuardError, match="protected branch"):
        client.push(branch="main")


def test_push_refuses_master_without_allow_main(client, repo_path):
    with pytest.raises(GitGuardError, match="protected branch"):
        client.push(branch="master")


def test_push_refuses_current_branch_when_protected(client, repo_path):
    """Without an explicit branch arg, ``push`` resolves to the cwd's
    current branch — which is ``main`` in repo_path. Same guard fires.
    """
    with pytest.raises(GitGuardError, match="protected branch"):
        client.push()


def test_push_feature_branch_does_not_trigger_guard(client, repo_path):
    """Sanity check: pushing a non-protected branch goes through the
    normal path (which here hits the not-configured-remote, not the
    guard).
    """
    _run("checkout", "-b", "feat/x", cwd=repo_path)
    # No remote configured in the test repo → push fails with a non-
    # guard error. The point is the guard didn't catch it.
    with pytest.raises(Exception) as exc_info:
        client.push(branch="feat/x")
    assert not isinstance(exc_info.value, GitGuardError)


# -- pr_commit_push composite ---------------------------------------------


def test_pr_commit_push_refuses_protected_branch(client, repo_path):
    with pytest.raises(GitGuardError, match="protected"):
        client.pr_commit_push("main", "subject", ["b.txt"])


def test_pr_commit_push_refuses_branch_mismatch(client, repo_path):
    """cwd is on main; caller asks for feat/x → fail fast before any
    side effect.
    """
    (repo_path / "b.txt").write_text("hi\n")
    with pytest.raises(GitGuardError, match="branch mismatch"):
        client.pr_commit_push("feat/x", "subject", ["b.txt"])
    # No side effect — file still untracked, nothing staged.
    assert "b.txt" in client.status().untracked
    assert "b.txt" not in client.status().staged


def test_pr_commit_push_happy_path(client, repo_path):
    """Full happy path against a feature branch: stage → commit →
    push (mocked). Returns ``{commit, push}`` shape.
    """
    _run("checkout", "-b", "feat/x", cwd=repo_path)
    (repo_path / "b.txt").write_text("hi\n")
    with patch.object(_GitCmd, "push", lambda *_a, **_kw: "", create=True):
        result = client.pr_commit_push(
            "feat/x", "add b.txt", ["b.txt"],
        )
    assert result["commit"]["message"] == "add b.txt"
    assert result["push"]["branch"] == "feat/x"
    # Commit landed on feat/x, not main.
    assert client.current_branch() == "feat/x"


def test_pr_commit_push_refuses_wildcard_paths(client, repo_path):
    _run("checkout", "-b", "feat/x", cwd=repo_path)
    with pytest.raises(GitGuardError, match="wildcard"):
        client.pr_commit_push("feat/x", "subject", ["."])
