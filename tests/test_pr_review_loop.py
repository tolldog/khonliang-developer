"""Tests for developer.pr_review_loop (fr_developer_35fe69af).

Uses a real GitClient against a temp git repo (matching test_agent.py's
git_repo fixture pattern) with git.cmd.Git.push monkeypatched (no real
remote), and fake GithubClient-shaped objects injected via the
``github_client=`` param so no network / real GitHub call ever happens.
"""

from __future__ import annotations

import subprocess as _sub

import pytest

from developer.pr_review_loop import (
    PrReviewLoopError,
    maybe_update_pr,
    merge_pr_and_sync,
    parse_owner_repo_from_origin,
    parse_pr_url,
)


# -- parse helpers --


def test_parse_owner_repo_from_origin_ssh():
    assert parse_owner_repo_from_origin(
        "git@github.com:tolldog/khonliang-developer.git"
    ) == "tolldog/khonliang-developer"


def test_parse_owner_repo_from_origin_https():
    assert parse_owner_repo_from_origin(
        "https://github.com/tolldog/khonliang-developer.git"
    ) == "tolldog/khonliang-developer"


def test_parse_owner_repo_from_origin_https_no_dotgit():
    assert parse_owner_repo_from_origin(
        "https://github.com/tolldog/khonliang-developer"
    ) == "tolldog/khonliang-developer"


def test_parse_owner_repo_from_origin_empty_raises():
    with pytest.raises(PrReviewLoopError, match="empty"):
        parse_owner_repo_from_origin("")
    with pytest.raises(PrReviewLoopError, match="empty"):
        parse_owner_repo_from_origin(None)


def test_parse_owner_repo_from_origin_unrecognized_raises():
    with pytest.raises(PrReviewLoopError, match="not a recognized"):
        parse_owner_repo_from_origin("https://gitlab.com/o/n.git")


def test_parse_pr_url_happy_path():
    repo, number = parse_pr_url("https://github.com/tolldog/khonliang-developer/pull/84")
    assert repo == "tolldog/khonliang-developer"
    assert number == 84


def test_parse_pr_url_rejects_garbage():
    with pytest.raises(PrReviewLoopError, match="not a recognized GitHub PR URL"):
        parse_pr_url("not a url")


# -- maybe_update_pr --


@pytest.fixture
def git_repo(tmp_path):
    repo_dir = tmp_path / "gitrepo"
    repo_dir.mkdir()

    def _g(*args):
        _sub.run(["git", *args], cwd=str(repo_dir), check=True,
                 stdout=_sub.DEVNULL, stderr=_sub.DEVNULL)

    _g("init", "-b", "feat/x")
    _g("config", "user.email", "t@example.com")
    _g("config", "user.name", "T")
    _g("remote", "add", "origin", "git@github.com:tolldog/khonliang-developer.git")
    (repo_dir / "a.txt").write_text("first\n")
    _g("add", "a.txt")
    _g("commit", "-m", "initial")
    return repo_dir


@pytest.fixture
def no_op_push(monkeypatch):
    from git.cmd import Git as _GitCmd

    calls: list[tuple] = []

    def fake_push(self_git, *args, **kwargs):
        calls.append(args)
        return ""

    monkeypatch.setattr(_GitCmd, "push", fake_push, raising=False)
    return calls


class _FakeGithubClient:
    def __init__(self, *, existing_pr=None, requested=True, already_requested=False):
        self.existing_pr = existing_pr
        self._requested = requested
        self._already_requested = already_requested
        self.find_calls: list[tuple] = []
        self.create_calls: list[dict] = []
        self.request_calls: list[tuple] = []

    async def find_open_pr_for_branch(self, repo, branch):
        self.find_calls.append((repo, branch))
        return self.existing_pr

    async def create_pr(self, repo, *, title, body, head, base):
        self.create_calls.append(
            {"repo": repo, "title": title, "body": body, "head": head, "base": base}
        )
        return {"number": 101, "html_url": f"https://github.com/{repo}/pull/101"}

    async def request_copilot_review(self, repo, pr_number):
        self.request_calls.append((repo, pr_number))
        return {
            "requested": self._requested,
            "already_requested": self._already_requested,
            "pr_node_id": "PR_kw123",
        }


@pytest.mark.asyncio
async def test_maybe_update_pr_creates_pr_and_requests_review_for_dirty_repo(
    git_repo, no_op_push,
):
    (git_repo / "a.txt").write_text("second\n")
    fake_gh = _FakeGithubClient(existing_pr=None)

    result = await maybe_update_pr(
        str(git_repo), "feat/x", github_client=fake_gh,
    )

    assert result["created"] is True
    assert result["committed"] is True
    assert result["repo"] == "tolldog/khonliang-developer"
    assert result["pr_number"] == 101
    assert result["review_requested"] is True
    assert fake_gh.create_calls[0]["head"] == "feat/x"
    assert fake_gh.request_calls == [("tolldog/khonliang-developer", 101)]


@pytest.mark.asyncio
async def test_maybe_update_pr_reuses_existing_pr_no_commit_no_review(
    git_repo, no_op_push,
):
    # Clean working tree, PR already exists — nothing materially changed,
    # so no review request should fire.
    fake_gh = _FakeGithubClient(
        existing_pr={"number": 55, "html_url": "https://github.com/o/n/pull/55"},
    )

    result = await maybe_update_pr(
        str(git_repo), "feat/x", github_client=fake_gh,
    )

    assert result["created"] is False
    assert result["committed"] is False
    assert result["pr_number"] == 55
    assert result["review_requested"] is False
    assert fake_gh.request_calls == []


@pytest.mark.asyncio
async def test_maybe_update_pr_generates_default_commit_message(git_repo, no_op_push):
    (git_repo / "b.txt").write_text("new file\n")
    fake_gh = _FakeGithubClient(existing_pr=None)

    result = await maybe_update_pr(str(git_repo), "feat/x", github_client=fake_gh)

    assert result["committed"] is True
    # No explicit commit_message → PR title falls back to the branch name.
    assert fake_gh.create_calls[0]["title"] == "feat/x"


@pytest.mark.asyncio
async def test_maybe_update_pr_requests_review_for_unpushed_prior_commit(
    git_repo, no_op_push,
):
    """A commit already made (e.g. via a prior git_pr_commit_push) but
    never pushed still counts as "materially changed" even though this
    call itself makes no new commit — status.ahead > 0 gate.

    Simulates "committed but not yet pushed" with real refs: create a
    remote-tracking ref at the current HEAD, set it as upstream, then
    add one more local commit so the tracking branch reports ahead=1 —
    without needing an actual remote or a real push.
    """
    head_sha = _sub.run(
        ["git", "rev-parse", "HEAD"], cwd=str(git_repo), check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    _sub.run(["git", "update-ref", "refs/remotes/origin/feat/x", head_sha],
              cwd=str(git_repo), check=True, stdout=_sub.DEVNULL, stderr=_sub.DEVNULL)
    _sub.run(["git", "branch", "--set-upstream-to=origin/feat/x", "feat/x"],
              cwd=str(git_repo), check=True, stdout=_sub.DEVNULL, stderr=_sub.DEVNULL)
    _sub.run(["git", "commit", "--allow-empty", "-m", "pending"],
              cwd=str(git_repo), check=True, stdout=_sub.DEVNULL, stderr=_sub.DEVNULL)

    from developer.git_client import GitClient
    assert GitClient(str(git_repo)).status().ahead == 1  # sanity-check the fixture

    fake_gh = _FakeGithubClient(
        existing_pr={"number": 55, "html_url": "https://github.com/o/n/pull/55"},
    )

    result = await maybe_update_pr(str(git_repo), "feat/x", github_client=fake_gh)

    assert result["committed"] is False
    assert result["review_requested"] is True


@pytest.mark.asyncio
async def test_maybe_update_pr_refuses_branch_mismatch(git_repo, no_op_push):
    from developer.git_client import GitGuardError

    fake_gh = _FakeGithubClient(existing_pr=None)
    with pytest.raises(GitGuardError, match="branch"):
        await maybe_update_pr(str(git_repo), "some-other-branch", github_client=fake_gh)


@pytest.mark.asyncio
async def test_maybe_update_pr_skips_review_when_auto_request_review_false(
    git_repo, no_op_push,
):
    (git_repo / "a.txt").write_text("third\n")
    fake_gh = _FakeGithubClient(existing_pr=None)

    result = await maybe_update_pr(
        str(git_repo), "feat/x", github_client=fake_gh, auto_request_review=False,
    )

    assert result["committed"] is True
    assert result["review_requested"] is False
    assert fake_gh.request_calls == []


# -- merge_pr_and_sync --


class _FakeGithubClientForMerge:
    def __init__(self, *, title="fr_developer_1234abcd: thing", head="feat/x",
                 merged=True, sha="abc123"):
        self._pr = {"title": title, "head": head}
        self._merge_result = {"merged": merged, "sha": sha}
        self.merge_calls: list[tuple] = []
        self.delete_calls: list[tuple] = []

    async def get_pr(self, repo, pr_number):
        return dict(self._pr)

    async def merge_pr(self, repo, pr_number, *, method="squash"):
        self.merge_calls.append((repo, pr_number, method))
        return dict(self._merge_result)

    async def delete_branch(self, repo, branch):
        self.delete_calls.append((repo, branch))
        return True


@pytest.mark.asyncio
async def test_merge_pr_and_sync_happy_path_deletes_branch_and_fires_hook():
    fake_gh = _FakeGithubClientForMerge()
    on_merged_calls = []

    async def fake_on_merged(repo, pr_number, title):
        on_merged_calls.append((repo, pr_number, title))

    result = await merge_pr_and_sync(
        "https://github.com/tolldog/khonliang-developer/pull/84",
        github_client=fake_gh,
        on_merged=fake_on_merged,
    )

    assert result["merged"] is True
    assert result["branch"] == "feat/x"
    assert result["branch_deleted"] is True
    assert fake_gh.merge_calls == [("tolldog/khonliang-developer", 84, "squash")]
    assert fake_gh.delete_calls == [("tolldog/khonliang-developer", "feat/x")]
    assert on_merged_calls == [
        ("tolldog/khonliang-developer", 84, "fr_developer_1234abcd: thing")
    ]


@pytest.mark.asyncio
async def test_merge_pr_and_sync_skips_branch_delete_when_disabled():
    fake_gh = _FakeGithubClientForMerge()

    result = await merge_pr_and_sync(
        "https://github.com/tolldog/khonliang-developer/pull/84",
        github_client=fake_gh, delete_branch=False,
    )

    assert result["branch_deleted"] is False
    assert fake_gh.delete_calls == []


@pytest.mark.asyncio
async def test_merge_pr_and_sync_survives_on_merged_hook_failure():
    """A failing FR-sync hook must not un-report a merge that already
    succeeded — best-effort, logged, not raised.
    """
    fake_gh = _FakeGithubClientForMerge()

    async def raising_hook(repo, pr_number, title):
        raise RuntimeError("boom")

    result = await merge_pr_and_sync(
        "https://github.com/tolldog/khonliang-developer/pull/84",
        github_client=fake_gh, on_merged=raising_hook,
    )
    assert result["merged"] is True


@pytest.mark.asyncio
async def test_merge_pr_and_sync_survives_branch_delete_failure():
    from developer.github_client import GithubClientError

    class _FailingDelete(_FakeGithubClientForMerge):
        async def delete_branch(self, repo, branch):
            raise GithubClientError("already gone")

    fake_gh = _FailingDelete()
    result = await merge_pr_and_sync(
        "https://github.com/tolldog/khonliang-developer/pull/84",
        github_client=fake_gh,
    )
    assert result["merged"] is True
    assert result["branch_deleted"] is False
