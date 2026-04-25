"""Tests for the GitHub client wrapper.

Focused on the module's own logic (auth discovery, repo parsing, error
normalization). Doesn't exercise the actual GitHub API — that's
integration-test territory and would need a token + network.
"""

from __future__ import annotations

import asyncio

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
    # pull_request_review_id defaults to None — stays backward-compatible
    # with callers that construct the dataclass without the new field.
    assert c.pull_request_review_id is None


def test_review_comment_dataclass_carries_review_id():
    c = GithubReviewComment(
        id=1, pr_number=42, repo="o/n", reviewer="bot",
        path="a.py", line=5, body="nit", created_at="2026-04-13",
        pull_request_review_id=777,
    )
    assert c.pull_request_review_id == 777


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
                 head_sha="b348b3f1234567890", mergeable_state="blocked",
                 merged=False, merged_at=None):
        self.number = number
        self.title = title
        self.state = state
        self.draft = draft
        self.mergeable = mergeable
        self.mergeable_state = mergeable_state
        self.merged = merged
        self.merged_at = merged_at
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

        async def async_list_review_comments(self, **kwargs):
            _maybe_raise("pulls.async_list_review_comments")
            data = review_comments or []
            # Same convention as issues.async_list_comments: if the
            # fixture is a list of pages (list-of-lists), return the
            # requested page; otherwise return the flat list as a
            # single-page response.
            if data and isinstance(data[0], list):
                page = int(kwargs.get("page", 1))
                return _FakeResponse2(data[page - 1] if page <= len(data) else [])
            return _FakeResponse2(data)

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
        async def async_list_comments(self, **kwargs):
            _maybe_raise("issues.async_list_comments")
            data = issue_comments or []
            if data and isinstance(data[0], list):
                page = int(kwargs.get("page", 1))
                return _FakeResponse2(data[page - 1] if page <= len(data) else [])
            return _FakeResponse2(data)

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
    # pull_request_review_id absent on the fake → normalized to None,
    # matches the "standalone reply-to-thread" case.
    assert all(rc.pull_request_review_id is None for rc in out)


@pytest.mark.asyncio
async def test_list_pr_review_comments_carries_review_id_and_original_line():
    """When the inline comment attaches to a formal review, githubkit
    exposes ``pull_request_review_id``; the normalized dataclass carries
    it through so :class:`developer.pr_watcher.PRFleetWatcher` can set
    ``review_id`` on ``pr.inline_finding`` payloads.

    Also checks the ``line → original_line`` fallback: when a comment's
    current anchor line is None (the file shifted under it) we fall
    back to the position at the time of posting so the subscriber
    always has a line to report.
    """
    class _Enriched:
        def __init__(self, id, path, line, original_line, review_id, body,
                     user=None, created_at="2026-04-22T00:00:00Z"):
            self.id = id
            self.path = path
            self.line = line
            self.original_line = original_line
            self.pull_request_review_id = review_id
            self.body = body
            self.user = user or _FakeUser()
            self.created_at = created_at

    c = GithubClient(token="t")
    _install_fake_gh(c, review_comments=[
        _Enriched(20, "a.py", 7, 7, 555, "inside-review finding"),
        _Enriched(21, "b.py", None, 42, None, "outdated / standalone"),
    ])
    out = await c.list_pr_review_comments("o/n", 3)
    assert out[0].pull_request_review_id == 555
    assert out[0].line == 7
    # line=None → fall back to original_line so the subscriber still
    # has a line to report for outdated / shifted comments.
    assert out[1].line == 42
    assert out[1].pull_request_review_id is None


@pytest.mark.asyncio
async def test_list_pr_review_comments_paginates_until_short_page():
    """Copilot R2 concern: a full first page would previously drop
    anything past per_page=30 because the wrapper issued a single
    unbounded call. Now the loop walks pages until a short page,
    matching :meth:`list_pr_issue_comments`' shape.
    """
    c = GithubClient(token="t")
    first_page = [
        _FakeReviewComment(i, f"f{i}.py", i, f"nit {i}") for i in range(100)
    ]
    second_page = [_FakeReviewComment(200, "late.py", 1, "last inline")]
    _install_fake_gh(c, review_comments=[first_page, second_page])
    out = await c.list_pr_review_comments("o/n", 3)
    assert len(out) == 101
    # Order is preserved across pages, and the final element is the
    # single entry from the second (short) page.
    assert out[-1].body == "last inline"
    assert out[-1].id == 200


@pytest.mark.asyncio
async def test_list_pr_review_comments_normalizes_datetime_to_iso():
    """Copilot R2 correctness: when githubkit returns ``created_at`` as a
    Python ``datetime`` instance, the wrapper must emit ISO-8601 (``T``
    separator) — not the space-separated ``str(datetime)`` form that
    downstream consumers (``pr.inline_finding.posted_at``) treat as
    broken.
    """
    from datetime import datetime, timezone

    dt = datetime(2026, 4, 23, 2, 25, 19, tzinfo=timezone.utc)
    c = GithubClient(token="t")
    _install_fake_gh(c, review_comments=[
        _FakeReviewComment(30, "a.py", 1, "hi", created_at=dt),
    ])
    out = await c.list_pr_review_comments("o/n", 3)
    assert out[0].created_at == "2026-04-23T02:25:19+00:00"
    # Guardrail: no space-separated form slipped through.
    assert " " not in out[0].created_at


def test_as_iso_helper_roundtrip():
    """``_as_iso`` returns ISO for datetimes, passes through existing
    strings, and coerces ``None`` to an empty string.
    """
    from datetime import datetime, timezone

    from developer.github_client import _as_iso

    assert _as_iso(None) == ""
    assert _as_iso("2026-04-23T02:25:19+00:00") == "2026-04-23T02:25:19+00:00"
    dt = datetime(2026, 4, 23, 2, 25, 19, tzinfo=timezone.utc)
    assert _as_iso(dt) == "2026-04-23T02:25:19+00:00"


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
async def test_list_pr_issue_comments_paginates_until_short_page():
    c = GithubClient(token="t")
    first_page = [
        _FakeIssueComment(i, f"comment {i}") for i in range(100)
    ]
    second_page = [_FakeIssueComment(200, "latest")]
    _install_fake_gh(c, issue_comments=[first_page, second_page])
    out = await c.list_pr_issue_comments("o/n", 5)
    assert len(out) == 101
    assert out[-1]["body"] == "latest"


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
    # Open PR: merged is False, merged_at is None.
    assert out["merged"] is False
    assert out["merged_at"] is None


@pytest.mark.asyncio
async def test_get_pr_surfaces_merged_fields_for_merged_pr():
    """FR fr_developer_207ff0fb: projection widening so watch_pr_fleet
    can observe the ``pr.merged`` transition without a second API call.
    """
    c = GithubClient(token="t")
    _install_fake_gh(c, pr=_FakePR(
        number=36, state="closed",
        merged=True, merged_at="2026-04-21T12:00:00Z",
    ))
    out = await c.get_pr("o/n", 36)
    assert out["state"] == "closed"
    assert out["merged"] is True
    assert out["merged_at"] == "2026-04-21T12:00:00Z"


@pytest.mark.asyncio
async def test_get_pr_normalizes_datetime_merged_at_to_iso8601():
    """githubkit can surface ``merged_at`` as a ``datetime`` instance.

    Python's default ``str(datetime)`` uses a space separator
    (``YYYY-MM-DD HH:MM:SS+00:00``), which is not ISO8601 and would
    destabilize downstream dedupe keys (pr_watcher's merged-event
    dedupe_id per fr_developer_6c8ec260). Assert we normalize to
    ISO8601 (``T`` separator) regardless of whether githubkit returned
    a string or a datetime.
    """
    from datetime import datetime, timezone
    c = GithubClient(token="t")
    dt = datetime(2026, 4, 21, 12, 0, 0, tzinfo=timezone.utc)
    _install_fake_gh(c, pr=_FakePR(
        number=36, state="closed", merged=True, merged_at=dt,
    ))
    out = await c.get_pr("o/n", 36)
    assert out["merged"] is True
    assert isinstance(out["merged_at"], str)
    # ISO8601 uses a ``T`` separator, not a space.
    assert "T" in out["merged_at"]
    assert " " not in out["merged_at"]
    # Round-trips through datetime.fromisoformat without error.
    assert datetime.fromisoformat(out["merged_at"]) == dt


@pytest.mark.asyncio
async def test_get_pr_closed_without_merge_has_none_merged_at():
    """Closed-but-never-merged PRs: merged=False, merged_at=None.

    Matches the REST contract; guards against a regression where we
    might stringify ``None`` into ``"None"``.
    """
    c = GithubClient(token="t")
    _install_fake_gh(c, pr=_FakePR(number=7, state="closed", merged=False, merged_at=None))
    out = await c.get_pr("o/n", 7)
    assert out["state"] == "closed"
    assert out["merged"] is False
    assert out["merged_at"] is None


@pytest.mark.asyncio
async def test_pr_readiness_classifies_merged_pr_as_terminal():
    """A merged PR is terminal — must NOT be classified as needs_fixes
    just because a CHANGES_REQUESTED review predates the merge.

    Regression guard for bug_developer_b317e4ea: the readiness ladder
    used to fall straight through to the review-state check, so any
    merged PR that had ever received a CHANGES_REQUESTED review came
    back as `needs_fixes` despite being closed and merged.

    `recommended_action` is the empty string for terminal states so
    downstream consumers (e.g. session_checkpoint._next_actions, which
    appends any non-empty action != 'merge') don't surface a confusing
    "no_action" task for a merged PR.
    """
    c = GithubClient(token="t")
    _install_fake_gh(
        c,
        pr=_FakePR(
            state="closed",
            merged=True,
            merged_at="2026-04-25T06:00:00Z",
            mergeable_state="unknown",
        ),
        reviews=[
            _FakeReview(
                1, "CHANGES_REQUESTED", "old block", "2026-04-13T00:00:00Z",
                user=_FakeUser(login="someone"),
            ),
        ],
    )
    out = await c.pr_readiness("o/n", 42)
    assert out.state == "merged"
    assert out.recommended_action == ""


@pytest.mark.asyncio
async def test_pr_readiness_classifies_closed_unmerged_pr_as_terminal():
    """A closed-but-not-merged PR is terminal — must surface an explicit
    closed_unmerged state rather than running through the review/merge
    ladder. Empty `recommended_action` for the same reason.
    """
    c = GithubClient(token="t")
    _install_fake_gh(
        c,
        pr=_FakePR(
            state="closed",
            merged=False,
            merged_at=None,
            mergeable_state="unknown",
        ),
    )
    out = await c.pr_readiness("o/n", 42)
    assert out.state == "closed_unmerged"
    assert out.recommended_action == ""


@pytest.mark.asyncio
async def test_pr_readiness_terminal_skips_review_fetches():
    """Terminal PRs don't trigger the review/comment list fetches —
    saves three REST calls per polling iteration on done PRs.
    """
    c = GithubClient(token="t")
    _install_fake_gh(
        c,
        pr=_FakePR(state="closed", merged=True, merged_at="2026-04-25T06:00:00Z"),
    )
    # Replace the list helpers with sentinels that fail the test if called.
    async def _boom(*args, **kwargs):  # pragma: no cover - sentinel
        raise AssertionError("review/comment fetch must not run for terminal PRs")
    c.list_pr_reviews = _boom  # type: ignore[method-assign]
    c.list_pr_review_comments = _boom  # type: ignore[method-assign]
    c.list_pr_issue_comments = _boom  # type: ignore[method-assign]

    out = await c.pr_readiness("o/n", 42)
    assert out.state == "merged"


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
    _install_fake_gh(c, pr=_FakePR(head_sha="b348b3f1234567890", mergeable_state="blocked"), reviews=[
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
async def test_pr_readiness_blocks_dirty_after_copilot_clear():
    c = GithubClient(token="t")
    _install_fake_gh(c, pr=_FakePR(mergeable_state="dirty"), issue_comments=[
        _FakeIssueComment(
            100,
            "Re-reviewed b348b3f and I don't see additional blocking issues in this scope.",
            user=_FakeUser(login="copilot-swe-agent"),
        )
    ])
    out = await c.pr_readiness("o/n", 42)
    assert out.state == "blocked_update_or_conflicts"
    assert out.recommended_action == "update_branch_or_resolve_conflicts"


@pytest.mark.asyncio
async def test_pr_readiness_blocks_behind_after_copilot_clear():
    c = GithubClient(token="t")
    _install_fake_gh(c, pr=_FakePR(mergeable_state="behind"), issue_comments=[
        _FakeIssueComment(
            100,
            "Re-reviewed b348b3f and I don't see additional blocking issues in this scope.",
            user=_FakeUser(login="copilot-swe-agent"),
        )
    ])
    out = await c.pr_readiness("o/n", 42)
    assert out.state == "blocked_update_or_conflicts"
    assert out.recommended_action == "update_branch_or_resolve_conflicts"


@pytest.mark.asyncio
async def test_pr_readiness_blocks_unknown_after_copilot_clear():
    c = GithubClient(token="t")
    _install_fake_gh(c, pr=_FakePR(mergeable_state="unknown"), issue_comments=[
        _FakeIssueComment(
            100,
            "Re-reviewed b348b3f and I don't see additional blocking issues in this scope.",
            user=_FakeUser(login="copilot-swe-agent"),
        )
    ])
    out = await c.pr_readiness("o/n", 42)
    assert out.state == "blocked_unknown_merge_state"
    assert out.recommended_action == "refresh_pr_merge_state"


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
async def test_pr_readiness_matches_head_sha_case_insensitively():
    c = GithubClient(token="t")
    _install_fake_gh(c, pr=_FakePR(head_sha="B348B3F123"), issue_comments=[
        _FakeIssueComment(
            100,
            "Re-reviewed b348b3f and I don't see additional blocking issues in this scope.",
            user=_FakeUser(login="copilot-swe-agent"),
        )
    ])
    out = await c.pr_readiness("o/n", 42)
    assert out.copilot_verdict == "clear"


@pytest.mark.asyncio
async def test_pr_readiness_matches_full_head_sha_case_insensitively():
    c = GithubClient(token="t")
    _install_fake_gh(c, pr=_FakePR(head_sha="b348b3f123"), issue_comments=[
        _FakeIssueComment(
            100,
            "Re-reviewed B348B3F123 and I don't see additional blocking issues in this scope.",
            user=_FakeUser(login="copilot-swe-agent"),
        )
    ])
    out = await c.pr_readiness("o/n", 42)
    assert out.copilot_verdict == "clear"


@pytest.mark.asyncio
async def test_pr_readiness_does_not_match_sha_inside_longer_hex_token():
    c = GithubClient(token="t")
    _install_fake_gh(c, pr=_FakePR(head_sha="b348b3f123"), issue_comments=[
        _FakeIssueComment(
            100,
            "Re-reviewed xb348b3f1234 and I don't see additional blocking issues in this scope.",
            user=_FakeUser(login="copilot-swe-agent"),
        )
    ])
    out = await c.pr_readiness("o/n", 42)
    assert out.copilot_verdict == "pending"


@pytest.mark.asyncio
async def test_pr_readiness_does_not_match_sha_with_hex_suffix():
    c = GithubClient(token="t")
    _install_fake_gh(c, pr=_FakePR(head_sha="b348b3f123"), issue_comments=[
        _FakeIssueComment(
            100,
            "Re-reviewed b348b3f1234a and I don't see additional blocking issues in this scope.",
            user=_FakeUser(login="copilot-swe-agent"),
        )
    ])
    out = await c.pr_readiness("o/n", 42)
    assert out.copilot_verdict == "pending"


@pytest.mark.asyncio
async def test_pr_readiness_keeps_human_changes_requested_even_with_copilot_clear():
    c = GithubClient(token="t")
    _install_fake_gh(c, pr=_FakePR(head_sha="b348b3f1234567890"), reviews=[
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
async def test_pr_readiness_keeps_actionable_comment_count_after_clear():
    c = GithubClient(token="t")
    _install_fake_gh(c, pr=_FakePR(mergeable_state="blocked"), review_comments=[
        _FakeReviewComment(10, "a.py", 5, "old inline comment"),
    ], issue_comments=[
        _FakeIssueComment(
            100,
            "Re-reviewed b348b3f and I don't see additional blocking issues in this scope.",
            user=_FakeUser(login="copilot-swe-agent"),
        )
    ])
    out = await c.pr_readiness("o/n", 42)
    assert out.state == "ready_admin_merge_policy_blocked"
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


@pytest.mark.asyncio
async def test_get_authenticated_user_login_returns_empty_without_token(monkeypatch):
    """Unauthenticated clients can't ask "who am I" — degrade to empty
    so callers (e.g. pr_watcher) get a disabled-filter fallback instead
    of an exception."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    c = GithubClient()
    assert await c.get_authenticated_user_login() == ""


@pytest.mark.asyncio
async def test_get_authenticated_user_login_reads_login_from_api():
    """With a token, the wrapper calls ``users.async_get_authenticated``
    and returns the login string. This is what pr_watcher uses to
    resolve ``self_login`` once per watcher lifetime."""
    c = GithubClient(token="t")

    class _Me:
        login = "tolldog"

    class _Users:
        async def async_get_authenticated(self, **_):
            return _FakeResponse2(_Me())

    class _Rest:
        users = _Users()

    class _Gh:
        rest = _Rest()

    c._gh = _Gh()
    assert await c.get_authenticated_user_login() == "tolldog"


@pytest.mark.asyncio
async def test_get_authenticated_user_login_swallows_errors():
    """A failed /user lookup must NOT take down the watcher — degrade
    to empty (filter disabled) and let the watcher keep running."""
    c = GithubClient(token="t")

    class _Users:
        async def async_get_authenticated(self, **_):
            raise RuntimeError("api down")

    class _Rest:
        users = _Users()

    class _Gh:
        rest = _Rest()

    c._gh = _Gh()
    assert await c.get_authenticated_user_login() == ""


# ---------------------------------------------------------------------------
# Cooperative cancellation propagation (PR #39 Copilot R4).
#
# ``asyncio.CancelledError`` is a subclass of ``Exception`` (Python 3.8+),
# which means a bare ``except Exception`` will swallow it and defeat
# cooperative cancellation. Every wrapper that catches ``Exception``
# around an ``await`` must therefore re-raise ``CancelledError`` before
# the generic fallback. These tests pin that invariant so a future edit
# that drops the ``except asyncio.CancelledError: raise`` clause fails
# loudly instead of silently converting cancellation into "success with
# degraded value" (``get_authenticated_user_login``) or a
# ``GithubClientError`` (the other wrappers).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_pr_review_comments_propagates_cancellation():
    """R4 site: the paging loop's ``except Exception`` must not convert
    ``CancelledError`` into ``GithubClientError``."""
    c = GithubClient(token="t")
    _install_fake_gh(
        c,
        raise_on={"pulls.async_list_review_comments": asyncio.CancelledError()},
    )
    with pytest.raises(asyncio.CancelledError):
        await c.list_pr_review_comments("o/n", 3)


@pytest.mark.asyncio
async def test_get_authenticated_user_login_propagates_cancellation():
    """R4 site: the degrade-to-empty fallback must not swallow
    ``CancelledError`` — otherwise watcher shutdown mid-resolve would
    silently complete instead of cancelling cooperatively."""
    c = GithubClient(token="t")

    class _Users:
        async def async_get_authenticated(self, **_):
            raise asyncio.CancelledError()

    class _Rest:
        users = _Users()

    class _Gh:
        rest = _Rest()

    c._gh = _Gh()
    with pytest.raises(asyncio.CancelledError):
        await c.get_authenticated_user_login()


@pytest.mark.asyncio
async def test_list_pr_reviews_propagates_cancellation():
    """Same shape as R4 applied to the sibling raise-as-GithubClientError
    wrappers — grep swept all eight sites in this module."""
    c = GithubClient(token="t")
    _install_fake_gh(
        c,
        raise_on={"pulls.async_list_reviews": asyncio.CancelledError()},
    )
    with pytest.raises(asyncio.CancelledError):
        await c.list_pr_reviews("o/n", 7)


@pytest.mark.asyncio
async def test_get_pr_propagates_cancellation():
    c = GithubClient(token="t")
    _install_fake_gh(c, raise_on={"pulls.async_get": asyncio.CancelledError()})
    with pytest.raises(asyncio.CancelledError):
        await c.get_pr("o/n", 3)


@pytest.mark.asyncio
async def test_merge_pr_propagates_cancellation():
    """merge_pr has the most elaborate post-catch logic (405/409 → typed
    errors). The CancelledError guard must fire BEFORE that mapping so a
    cancelled merge doesn't bubble up as a ``GithubMergeBlockedError`` or
    ``GithubMergeConflictError``."""
    c = GithubClient(token="t")
    _install_fake_gh(c, raise_on={"pulls.async_merge": asyncio.CancelledError()})
    with pytest.raises(asyncio.CancelledError):
        await c.merge_pr("o/n", 3)


@pytest.mark.asyncio
async def test_list_pr_issue_comments_propagates_cancellation():
    c = GithubClient(token="t")
    _install_fake_gh(
        c,
        raise_on={"issues.async_list_comments": asyncio.CancelledError()},
    )
    with pytest.raises(asyncio.CancelledError):
        await c.list_pr_issue_comments("o/n", 3)


@pytest.mark.asyncio
async def test_post_pr_comment_propagates_cancellation():
    c = GithubClient(token="t")
    _install_fake_gh(
        c,
        raise_on={"issues.async_create_comment": asyncio.CancelledError()},
    )
    with pytest.raises(asyncio.CancelledError):
        await c.post_pr_comment("o/n", 3, "hi")


@pytest.mark.asyncio
async def test_create_pr_propagates_cancellation():
    c = GithubClient(token="t")
    _install_fake_gh(c, raise_on={"pulls.async_create": asyncio.CancelledError()})
    with pytest.raises(asyncio.CancelledError):
        await c.create_pr("o/n", title="t", body="b", head="h")
