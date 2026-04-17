"""Tests for the GitHub client wrapper.

Focused on the module's own logic (auth discovery, repo parsing, error
normalization). Doesn't exercise the actual GitHub API — that's
integration-test territory and would need a token + network.
"""

from __future__ import annotations

import pytest

from developer.github_client import (
    GithubClient,
    GithubClientError,
    GithubReview,
    GithubReviewComment,
)


def test_import_does_not_construct_github_object():
    """Constructor is cheap; githubkit import is deferred to first API call."""
    c = GithubClient(token="x")
    assert c._gh is None


def test_token_from_kwarg_wins():
    c = GithubClient(token="from-kwarg")
    assert c._token == "from-kwarg"


def test_token_from_github_token_env(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "from-env")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    c = GithubClient()
    assert c._token == "from-env"


def test_token_falls_back_to_gh_token_env(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GH_TOKEN", "gh-cli-style")
    c = GithubClient()
    assert c._token == "gh-cli-style"


def test_token_is_none_when_no_env(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    c = GithubClient()
    assert c._token is None


def test_split_repo_valid():
    assert GithubClient._split_repo("tolldog/khonliang-developer") == ("tolldog", "khonliang-developer")


def test_split_repo_rejects_bare_name():
    with pytest.raises(GithubClientError, match="owner/name"):
        GithubClient._split_repo("khonliang-developer")


def test_split_repo_with_extra_slashes_takes_first():
    # "owner/name/extra" — partition("/") gives ("owner", "/", "name/extra")
    owner, name = GithubClient._split_repo("owner/name/extra")
    assert owner == "owner"
    assert name == "name/extra"


def test_split_repo_rejects_empty_halves():
    with pytest.raises(GithubClientError, match="non-empty"):
        GithubClient._split_repo("owner/")
    with pytest.raises(GithubClientError, match="non-empty"):
        GithubClient._split_repo("/name")
    with pytest.raises(GithubClientError, match="non-empty"):
        GithubClient._split_repo("  /  ")


def test_split_repo_strips_whitespace():
    owner, name = GithubClient._split_repo("  owner/name  ")
    assert owner == "owner"
    assert name == "name"


def test_review_dataclass_roundtrip():
    r = GithubReview(
        id=1, pr_number=42, repo="o/n", reviewer="bot",
        state="COMMENTED", body="looks good", submitted_at="2026-04-13T00:00:00Z",
    )
    assert r.pr_number == 42
    assert r.state == "COMMENTED"


def test_review_comment_dataclass_roundtrip():
    c = GithubReviewComment(
        id=1, pr_number=42, repo="o/n", reviewer="bot",
        path="developer/foo.py", line=10, body="nit", created_at="2026-04-13",
    )
    assert c.path == "developer/foo.py"
    assert c.line == 10


def test_pr_readiness_dataclass_roundtrip():
    from developer.github_client import GithubPRReadiness

    r = GithubPRReadiness(
        state="ready_admin_merge_policy_blocked",
        recommended_action="admin_merge_if_operator_approves",
        copilot_verdict="clear",
        latest_copilot_comment="no additional blocking issues in b348b3f",
        actionable_comments=0,
        review_decision="unknown",
        merge_state="blocked",
        head_ref="feat/x",
        head_sha="b348b3f",
        url="https://github.com/o/n/pull/1",
    )
    assert r.state == "ready_admin_merge_policy_blocked"


# -- typed merge errors --

class _FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


class _FakeHttpError(Exception):
    def __init__(self, status_code: int):
        super().__init__(f"HTTP {status_code}")
        self.response = _FakeResponse(status_code)


def _install_merge_stub(client, error_to_raise):
    """Replace the underlying githubkit merge call with one that raises."""
    class _FakePulls:
        async def async_merge(self, **kwargs):
            raise error_to_raise

    class _FakeRest:
        pulls = _FakePulls()

    class _FakeGh:
        rest = _FakeRest()

    client._gh = _FakeGh()


@pytest.mark.asyncio
async def test_merge_pr_raises_merge_blocked_on_405():
    from developer.github_client import GithubMergeBlockedError

    c = GithubClient(token="t")
    _install_merge_stub(c, _FakeHttpError(405))
    with pytest.raises(GithubMergeBlockedError, match="branch protection"):
        await c.merge_pr("o/n", 1)


@pytest.mark.asyncio
async def test_merge_pr_raises_merge_conflict_on_409():
    from developer.github_client import GithubMergeConflictError

    c = GithubClient(token="t")
    _install_merge_stub(c, _FakeHttpError(409))
    with pytest.raises(GithubMergeConflictError, match="conflicts or base has moved"):
        await c.merge_pr("o/n", 1)


@pytest.mark.asyncio
async def test_merge_pr_wraps_other_errors_as_client_error():
    c = GithubClient(token="t")
    _install_merge_stub(c, _FakeHttpError(500))
    with pytest.raises(GithubClientError):
        await c.merge_pr("o/n", 1)


# -- API normalization tests ------------------------------------------------
# Replace the lazy `_gh` object with a minimal fake so we exercise each
# wrapper's normalization logic without a network round-trip or githubkit.


class _FakeUser:
    def __init__(self, login="copilot-pull-request-reviewer[bot]"):
        self.login = login


class _FakeResponse2:
    def __init__(self, parsed_data):
        self.parsed_data = parsed_data


class _FakeReview:
    def __init__(self, id, state, body, submitted_at, user=None):
        self.id = id
        self.state = state
        self.body = body
        self.submitted_at = submitted_at
        self.user = user or _FakeUser()


class _FakeReviewComment:
    def __init__(self, id, path, line, body, user=None, created_at="2026-04-13T00:00:00Z"):
        self.id = id
        self.path = path
        self.line = line
        self.body = body
        self.user = user or _FakeUser()
        self.created_at = created_at


class _FakeIssueComment:
    def __init__(self, id, body, user=None, created_at="2026-04-13T00:00:00Z"):
        self.id = id
        self.body = body
        self.user = user or _FakeUser(login="tolldog")
        self.created_at = created_at


class _FakePR:
    def __init__(self, number=42, title="t", state="open", draft=False,
                 mergeable=True, author="tolldog", head="feat/x", base="main",
                 head_sha="b348b3f1234567890", mergeable_state="blocked"):
        self.number = number
        self.title = title
        self.state = state
        self.draft = draft
        self.mergeable = mergeable
        self.mergeable_state = mergeable_state
        self.user = _FakeUser(login=author)

        class _Ref:
            def __init__(self, ref, sha=""):
                self.ref = ref
                self.sha = sha

        self.head = _Ref(head, head_sha)
        self.base = _Ref(base)
        self.html_url = f"https://github.com/o/n/pull/{number}"


def _install_fake_gh(client, *, reviews=None, review_comments=None,
                     issue_comments=None, pr=None, merge=None, create_pr=None,
                     create_comment_id=None, raise_on=None):
    """Install a minimal fake ``_gh`` on ``client`` with the given fixtures.

    ``raise_on`` is a mapping of method-path → exception, e.g.
    ``{"pulls.async_list_reviews": _FakeHttpError(404)}`` to simulate failures.
    """
    raise_on = raise_on or {}

    def _maybe_raise(key):
        if key in raise_on:
            raise raise_on[key]

    class _Pulls:
        async def async_list_reviews(self, **_):
            _maybe_raise("pulls.async_list_reviews")
            return _FakeResponse2(reviews or [])

        async def async_list_review_comments(self, **_):
            _maybe_raise("pulls.async_list_review_comments")
            return _FakeResponse2(review_comments or [])

        async def async_get(self, **_):
            _maybe_raise("pulls.async_get")
            return _FakeResponse2(pr or _FakePR())

        async def async_create(self, **_):
            _maybe_raise("pulls.async_create")
            return _FakeResponse2(create_pr or _FakePR(number=999))

        async def async_merge(self, **_):
            _maybe_raise("pulls.async_merge")
            class _Result:
                merged = True
                sha = "abc123"
                message = "Pull Request successfully merged"
            return _FakeResponse2(merge or _Result())

    class _Issues:
        async def async_list_comments(self, **_):
            _maybe_raise("issues.async_list_comments")
            return _FakeResponse2(issue_comments or [])

        async def async_create_comment(self, **_):
            _maybe_raise("issues.async_create_comment")
            class _Result:
                id = create_comment_id if create_comment_id is not None else 9876
            return _FakeResponse2(_Result())

    class _Rest:
        pulls = _Pulls()
        issues = _Issues()

    class _Gh:
        rest = _Rest()

    client._gh = _Gh()


@pytest.mark.asyncio
async def test_list_pr_reviews_normalizes_and_drops_pending():
    c = GithubClient(token="t")
    _install_fake_gh(c, reviews=[
        _FakeReview(1, "APPROVED", "lgtm", "2026-04-13T00:00:00Z"),
        _FakeReview(2, "PENDING", "wip", None),  # draft, should drop
        _FakeReview(3, "COMMENTED", "nit", "2026-04-13T01:00:00Z"),
    ])
    out = await c.list_pr_reviews("o/n", 7)
    assert [r.id for r in out] == [1, 3]
    assert out[0].pr_number == 7
    assert out[0].repo == "o/n"
    assert out[0].reviewer == "copilot-pull-request-reviewer[bot]"


@pytest.mark.asyncio
async def test_list_pr_reviews_wraps_errors():
    c = GithubClient(token="t")
    _install_fake_gh(c, raise_on={"pulls.async_list_reviews": _FakeHttpError(404)})
    with pytest.raises(GithubClientError, match="list_pr_reviews"):
        await c.list_pr_reviews("o/n", 7)


@pytest.mark.asyncio
async def test_list_pr_review_comments_normalizes():
    c = GithubClient(token="t")
    _install_fake_gh(c, review_comments=[
        _FakeReviewComment(10, "a.py", 5, "nit"),
        _FakeReviewComment(11, "b.py", None, "file-level comment"),
    ])
    out = await c.list_pr_review_comments("o/n", 3)
    assert len(out) == 2
    assert out[0].path == "a.py" and out[0].line == 5
    assert out[1].line is None


@pytest.mark.asyncio
async def test_list_pr_issue_comments_uses_plain_dicts():
    c = GithubClient(token="t")
    _install_fake_gh(c, issue_comments=[
        _FakeIssueComment(100, "hi"),
        _FakeIssueComment(101, "@copilot review please"),
    ])
    out = await c.list_pr_issue_comments("o/n", 5)
    assert len(out) == 2
    assert out[0]["id"] == 100
    assert out[1]["body"] == "@copilot review please"
    assert out[0]["user"] == "tolldog"


@pytest.mark.asyncio
async def test_get_pr_returns_normalized_metadata():
    c = GithubClient(token="t")
    _install_fake_gh(c, pr=_FakePR(number=42, title="t", head="feat/x", base="main"))
    out = await c.get_pr("o/n", 42)
    assert out["number"] == 42
    assert out["head"] == "feat/x"
    assert out["head_sha"].startswith("b348b3f")
    assert out["base"] == "main"
    assert out["html_url"].endswith("/pull/42")


@pytest.mark.asyncio
async def test_pr_readiness_classifies_policy_blocked_after_copilot_clear():
    c = GithubClient(token="t")
    _install_fake_gh(c, pr=_FakePR(mergeable_state="blocked"), issue_comments=[
        _FakeIssueComment(
            100,
            "Re-reviewed b348b3f and I don't see additional blocking issues in this scope.",
            user=_FakeUser(login="copilot-swe-agent"),
        )
    ])
    out = await c.pr_readiness("o/n", 42)
    assert out.state == "ready_admin_merge_policy_blocked"
    assert out.recommended_action == "admin_merge_if_operator_approves"
    assert out.copilot_verdict == "clear"


@pytest.mark.asyncio
async def test_pr_readiness_ignores_stale_copilot_changes_requested_after_clear():
    c = GithubClient(token="t")
    _install_fake_gh(c, pr=_FakePR(mergeable_state="blocked"), reviews=[
        _FakeReview(
            1,
            "CHANGES_REQUESTED",
            "please address this",
            "2026-04-13T00:00:00Z",
            user=_FakeUser(login="copilot-pull-request-reviewer[bot]"),
        ),
    ], issue_comments=[
        _FakeIssueComment(
            100,
            "Re-reviewed B348B3F and I don't see additional blocking issues in this scope.",
            user=_FakeUser(login="copilot-swe-agent"),
        )
    ])
    out = await c.pr_readiness("o/n", 42)
    assert out.state == "ready_admin_merge_policy_blocked"
    assert out.recommended_action == "admin_merge_if_operator_approves"
    assert out.copilot_verdict == "clear"


@pytest.mark.asyncio
async def test_pr_readiness_requires_copilot_rereview_when_clear_is_stale():
    c = GithubClient(token="t")
    _install_fake_gh(c, pr=_FakePR(head_sha="fffffff123"), issue_comments=[
        _FakeIssueComment(
            100,
            "Re-reviewed b348b3f and I don't see additional blocking issues in this scope.",
            user=_FakeUser(login="copilot-swe-agent"),
        )
    ])
    out = await c.pr_readiness("o/n", 42)
    assert out.state == "needs_copilot_rereview"
    assert out.copilot_verdict == "pending"


@pytest.mark.asyncio
async def test_pr_readiness_keeps_human_changes_requested_even_with_copilot_clear():
    c = GithubClient(token="t")
    _install_fake_gh(c, reviews=[
        _FakeReview(
            1,
            "CHANGES_REQUESTED",
            "human review block",
            "2026-04-13T00:00:00Z",
            user=_FakeUser(login="maintainer"),
        ),
    ], issue_comments=[
        _FakeIssueComment(
            100,
            "Re-reviewed b348b3f and I don't see additional blocking issues in this scope.",
            user=_FakeUser(login="copilot-swe-agent"),
        )
    ])
    out = await c.pr_readiness("o/n", 42)
    assert out.state == "needs_fixes"
    assert out.recommended_action == "address_changes_requested"


@pytest.mark.asyncio
async def test_pr_readiness_reports_review_comments_before_clear():
    c = GithubClient(token="t")
    _install_fake_gh(c, review_comments=[
        _FakeReviewComment(10, "a.py", 5, "please fix"),
    ])
    out = await c.pr_readiness("o/n", 42)
    assert out.state == "needs_fixes"
    assert out.recommended_action == "address_review_comments"
    assert out.actionable_comments == 1


@pytest.mark.asyncio
async def test_create_pr_returns_number_and_url():
    c = GithubClient(token="t")
    _install_fake_gh(c, create_pr=_FakePR(number=99))
    out = await c.create_pr("o/n", title="t", body="b", head="feat/x")
    assert out["number"] == 99
    assert "/pull/99" in out["html_url"]


@pytest.mark.asyncio
async def test_post_pr_comment_returns_id():
    c = GithubClient(token="t")
    _install_fake_gh(c, create_comment_id=555)
    comment_id = await c.post_pr_comment("o/n", 3, "thanks for the review")
    assert comment_id == 555


@pytest.mark.asyncio
async def test_merge_pr_success_reports_admin_bypass_flag():
    c = GithubClient(token="t")
    _install_fake_gh(c)  # default merge stub returns merged=True
    out = await c.merge_pr("o/n", 1, admin_bypass=True)
    assert out["merged"] is True
    assert out["admin_bypass"] is True
    assert out["sha"] == "abc123"


@pytest.mark.asyncio
async def test_missing_user_falls_back_to_unknown():
    """When GitHub omits the user (ghost / deleted account), normalizer uses 'unknown'."""
    c = GithubClient(token="t")
    review_no_user = _FakeReview(1, "COMMENTED", "", "2026-04-13T00:00:00Z")
    review_no_user.user = None
    _install_fake_gh(c, reviews=[review_no_user])
    out = await c.list_pr_reviews("o/n", 1)
    assert out[0].reviewer == "unknown"
