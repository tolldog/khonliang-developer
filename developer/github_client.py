"""Thin async wrapper around `githubkit` for developer's GitHub interactions.

Per ``feedback_prefer_python_modules_over_execs``, developer reaches for
the Python SDK rather than shelling out to ``gh``. This module is the
single entry point for GitHub API calls from anywhere in developer, so
authentication + error normalization live in one place.

Consumers (near-future FRs that will land on top of this layer):
- ``fr_developer_d5642f3e`` (ingest PR reviews — read review comments,
  cluster findings)
- ``fr_developer_5559d499`` (PR watcher + auto-trigger Claude — watch PR
  state, post comments, re-request review)
- ``fr_developer_167965f4`` (stacked PR workflow — create / rebase /
  merge stacks via API instead of the ``gh`` CLI)

The surface is intentionally minimal in this PR. We add methods as
consumers land — avoids guessing which shapes they want.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


def _as_iso(value: Any) -> str:
    """Normalize a GitHub-provided timestamp into an ISO-8601 string.

    githubkit sometimes surfaces timestamp fields as Python ``datetime``
    instances and sometimes as already-ISO strings, depending on the
    response model / response-parsed path. Python's default
    ``str(datetime)`` uses a space separator (``"2026-04-23 02:25:19+00:00"``)
    rather than ISO-8601 (``"2026-04-23T02:25:19+00:00"``). Downstream
    consumers (notably :mod:`developer.pr_watcher` payload fields like
    ``posted_at``) expect the ``T``-separated shape, so normalize at the
    client boundary instead of patching every payload site.

    ``None`` is coerced to an empty string so callers can use the result
    as a plain ``str`` without a separate nullable branch.
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


@dataclass
class GithubReview:
    """Normalized shape of a PR review for downstream consumers.

    Keeps the schema simple (id, reviewer, state, body, submitted_at) so
    the FR for PR review ingest doesn't have to deal with githubkit's
    response models directly.
    """

    id: int
    pr_number: int
    repo: str                       # "owner/name"
    reviewer: str                   # login, e.g. "copilot-pull-request-reviewer[bot]"
    state: str                      # APPROVED | CHANGES_REQUESTED | COMMENTED | PENDING
    body: str
    submitted_at: str | None        # ISO8601 string; None if still draft


@dataclass
class GithubReviewComment:
    """A single inline review comment on a file:line.

    ``pull_request_review_id`` links the comment back to the containing
    review (from :meth:`GithubClient.list_pr_reviews`) when the comment
    is part of a formal review. Standalone review-thread replies post
    outside a review and carry ``pull_request_review_id=None``.
    """

    id: int
    pr_number: int
    repo: str
    reviewer: str
    path: str                        # file path within the repo
    line: int | None                 # target line; None for file-level comments
    body: str
    created_at: str
    pull_request_review_id: int | None = None


@dataclass
class GithubPRReadiness:
    """Bounded PR merge-readiness summary for agent workflows."""

    state: str
    recommended_action: str
    copilot_verdict: str
    latest_copilot_comment: str
    actionable_comments: int
    review_decision: str
    merge_state: str
    head_ref: str
    head_sha: str
    url: str


class GithubClientError(RuntimeError):
    """Base class for client errors so callers can distinguish from
    domain errors (404 from GitHub is a transport concern, not a bug)."""


class GithubMergeBlockedError(GithubClientError):
    """Merge rejected by branch protection.

    Raised when GitHub's merge endpoint returns 405 Method Not Allowed —
    typically because branch protection requires reviews, required checks,
    or similar gates that haven't been satisfied. Callers with bypass
    permission (the solo-maintainer case this project runs as) should
    pass ``admin_bypass=True`` to :meth:`GithubClient.merge_pr` so the
    bypass is explicit in logs and audit trails, instead of a silently
    successful REST call.
    """


class GithubMergeConflictError(GithubClientError):
    """Merge rejected because the PR has conflicts or the base has moved.

    Raised when GitHub's merge endpoint returns 409 Conflict. Callers
    typically need to rebase / update the branch before retrying.
    """


class GithubClient:
    """Async thin wrapper over githubkit.

    Lazy-constructs the underlying ``GitHub`` object on first use so the
    module imports cheaply and tests that don't touch GitHub can run
    without a token configured.

    Token discovery order:
      1. ``token`` kwarg to the constructor
      2. ``GITHUB_TOKEN`` env var
      3. ``GH_TOKEN`` env var (matches the ``gh`` CLI's convention)
      4. Unauthenticated (read-only public access — fine for many reads)
    """

    def __init__(self, token: str | None = None):
        self._token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        self._gh: Any = None  # lazy: githubkit.GitHub

    def _client(self) -> Any:
        if self._gh is None:
            try:
                from githubkit import GitHub
            except ImportError as e:
                raise GithubClientError(
                    "githubkit is not installed. Install it or remove the "
                    "GitHub dependency from developer's pyproject.toml."
                ) from e
            self._gh = GitHub(self._token) if self._token else GitHub()
        return self._gh

    # -- reviews --

    async def list_pr_reviews(self, repo: str, pr_number: int) -> list[GithubReview]:
        """Return all submitted reviews on a PR as normalized records.

        ``repo`` is ``"owner/name"``. Excludes pending (draft) reviews by
        default — those have no ``submitted_at``.
        """
        owner, name = self._split_repo(repo)
        try:
            resp = await self._client().rest.pulls.async_list_reviews(
                owner=owner, repo=name, pull_number=pr_number,
            )
        except asyncio.CancelledError:
            # CancelledError subclasses Exception (since 3.8); never
            # convert cooperative cancellation into GithubClientError.
            raise
        except Exception as e:
            raise GithubClientError(f"list_pr_reviews({repo}#{pr_number}): {e}") from e

        result: list[GithubReview] = []
        for r in resp.parsed_data:
            if r.submitted_at is None:
                continue  # still drafting, skip
            result.append(
                GithubReview(
                    id=r.id,
                    pr_number=pr_number,
                    repo=repo,
                    reviewer=r.user.login if r.user else "unknown",
                    state=r.state,
                    body=r.body or "",
                    submitted_at=_as_iso(r.submitted_at),
                )
            )
        return result

    async def list_pr_review_comments(
        self, repo: str, pr_number: int,
    ) -> list[GithubReviewComment]:
        """Inline review comments on a PR (file:line feedback).

        Separate from :meth:`list_pr_reviews` because GitHub's API splits
        top-level review metadata (state, summary body) from per-line
        comments. Consumers typically want both.

        Paginates until the API returns a short page so large PRs don't
        silently drop older inline findings past the default page size.
        Mirrors :meth:`list_pr_issue_comments`' loop shape so both
        comment-channel fetches have the same operational behavior.
        """
        owner, name = self._split_repo(repo)
        comments: list[GithubReviewComment] = []
        page = 1
        try:
            while True:
                resp = await self._client().rest.pulls.async_list_review_comments(
                    owner=owner, repo=name, pull_number=pr_number,
                    per_page=100, page=page,
                )
                batch = [
                    GithubReviewComment(
                        id=c.id,
                        pr_number=pr_number,
                        repo=repo,
                        reviewer=c.user.login if c.user else "unknown",
                        path=c.path,
                        # GitHub exposes both ``line`` (current position,
                        # None once the diff has shifted) and
                        # ``original_line`` (position at the time the
                        # comment was posted). Prefer ``line`` so
                        # subscribers see the current anchor; fall back to
                        # ``original_line`` when the comment is outdated.
                        line=getattr(c, "line", None) or getattr(c, "original_line", None),
                        body=c.body or "",
                        created_at=_as_iso(c.created_at),
                        pull_request_review_id=getattr(c, "pull_request_review_id", None),
                    )
                    for c in resp.parsed_data
                ]
                comments.extend(batch)
                if len(batch) < 100:
                    break
                page += 1
        except asyncio.CancelledError:
            # CancelledError is a subclass of Exception (3.8+); never
            # let the generic fallback convert cooperative cancellation
            # into a GithubClientError.
            raise
        except Exception as e:
            raise GithubClientError(
                f"list_pr_review_comments({repo}#{pr_number}): {e}"
            ) from e
        return comments

    async def post_pr_comment(self, repo: str, pr_number: int, body: str) -> int:
        """Post a top-level (issue-style) comment on a PR; returns the comment id.

        Used by the PR watcher (``fr_developer_5559d499``) to re-request
        review after addressing findings, and similar workflows.
        """
        owner, name = self._split_repo(repo)
        try:
            resp = await self._client().rest.issues.async_create_comment(
                owner=owner, repo=name, issue_number=pr_number, body=body,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            raise GithubClientError(f"post_pr_comment({repo}#{pr_number}): {e}") from e
        return resp.parsed_data.id

    async def list_pr_issue_comments(self, repo: str, pr_number: int) -> list[dict]:
        """Top-level (conversation) comments on a PR — separate from reviews.

        This is where ``@copilot review please`` threads live. Returned
        as plain dicts (``user``, ``body``, ``created_at``, ``id``) to
        avoid leaking githubkit types.
        """
        owner, name = self._split_repo(repo)
        comments: list[dict] = []
        page = 1
        try:
            while True:
                resp = await self._client().rest.issues.async_list_comments(
                    owner=owner, repo=name, issue_number=pr_number,
                    per_page=100, page=page,
                )
                batch = [
                    {
                        "id": c.id,
                        "user": c.user.login if c.user else "unknown",
                        "body": c.body or "",
                        "created_at": _as_iso(c.created_at),
                    }
                    for c in resp.parsed_data
                ]
                comments.extend(batch)
                if len(batch) < 100:
                    break
                page += 1
        except asyncio.CancelledError:
            raise
        except Exception as e:
            raise GithubClientError(f"list_pr_issue_comments({repo}#{pr_number}): {e}") from e
        return comments

    async def get_pr(self, repo: str, pr_number: int) -> dict:
        """Return normalized PR metadata.

        Surfaced fields: ``number``, ``title``, ``state``, ``draft``,
        ``mergeable``, ``mergeable_state``, ``author``, ``head``,
        ``head_sha``, ``base``, ``html_url``, ``merged``, ``merged_at``.

        The ``merged`` / ``merged_at`` pair lets downstream consumers
        (notably :func:`developer.pr_watcher._snapshot_from_github`)
        detect the merged-terminal transition without a second API call
        — GitHub's REST response already carries both fields, so this
        is projection widening rather than a new lookup. ``merged_at``
        is an ISO8601 string when the PR is merged and ``None`` when
        the PR is open or closed-unmerged.
        """
        owner, name = self._split_repo(repo)
        try:
            resp = await self._client().rest.pulls.async_get(
                owner=owner, repo=name, pull_number=pr_number,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            raise GithubClientError(f"get_pr({repo}#{pr_number}): {e}") from e
        pr = resp.parsed_data
        merged_at_raw = getattr(pr, "merged_at", None)
        # githubkit may return merged_at as a ``datetime`` instance; Python's
        # default ``str(datetime)`` uses a space separator ("YYYY-MM-DD HH:MM:SS+00:00")
        # rather than ISO8601 ("YYYY-MM-DDTHH:MM:SS+00:00"). Downstream consumers
        # (including the pr_watcher dedupe key per fr_developer_6c8ec260) rely on
        # a stable ISO8601 shape, so normalize through the shared helper.
        # Preserve the nullable contract: when GH reports ``merged_at=None``
        # (open / closed-unmerged PR), we return ``None`` rather than ``""``
        # so callers can distinguish "never merged" from "merged-at-epoch".
        if merged_at_raw is None:
            merged_at = None
        else:
            merged_at = _as_iso(merged_at_raw)
        return {
            "number": pr.number,
            "title": pr.title,
            "state": pr.state,
            "draft": pr.draft,
            "mergeable": pr.mergeable,
            "mergeable_state": getattr(pr, "mergeable_state", None),
            "author": pr.user.login if pr.user else "unknown",
            "head": pr.head.ref,
            "head_sha": getattr(pr.head, "sha", ""),
            "base": pr.base.ref,
            "html_url": pr.html_url,
            "merged": bool(getattr(pr, "merged", False)),
            "merged_at": merged_at,
        }

    async def pr_readiness(self, repo: str, pr_number: int) -> GithubPRReadiness:
        """Classify whether a PR is ready for normal merge, admin merge, or fixes.

        This intentionally uses the REST surface already present in this
        client. It cannot see GraphQL review-thread resolution state, but it
        does capture the failure mode we hit repeatedly: Copilot clears the
        latest commit in conversation comments while branch policy still
        reports a review block.
        """
        pr, reviews, review_comments, issue_comments = await asyncio.gather(
            self.get_pr(repo, pr_number),
            self.list_pr_reviews(repo, pr_number),
            self.list_pr_review_comments(repo, pr_number),
            self.list_pr_issue_comments(repo, pr_number),
        )
        copilot_comment = _latest_copilot_clear_comment(
            issue_comments, head_sha=pr.get("head_sha", ""),
        )
        copilot_verdict = "clear" if copilot_comment else "pending"
        blocking_reviews = [
            r for r in reviews
            if r.state.upper() == "CHANGES_REQUESTED"
        ]
        effective_blocking_reviews = [
            r for r in blocking_reviews
            if not (copilot_comment and is_copilot_login(r.reviewer))
        ]
        actionable_comments = len(review_comments)
        merge_state = str(pr.get("mergeable_state") or "unknown").lower()

        if pr.get("draft"):
            state = "blocked_draft"
            action = "mark_ready_for_review"
        elif effective_blocking_reviews:
            state = "needs_fixes"
            action = "address_changes_requested"
        elif actionable_comments and copilot_verdict != "clear":
            state = "needs_fixes"
            action = "address_review_comments"
        elif copilot_verdict != "clear":
            state = "needs_copilot_rereview"
            action = "comment_@copilot_for_review"
        elif merge_state in {"clean", "unstable", "has_hooks"}:
            state = "ready_normal_merge"
            action = "merge"
        elif merge_state == "blocked":
            state = "ready_admin_merge_policy_blocked"
            action = "admin_merge_if_operator_approves"
        elif merge_state in {"dirty", "behind"}:
            state = "blocked_update_or_conflicts"
            action = "update_branch_or_resolve_conflicts"
        elif merge_state == "unknown":
            state = "blocked_unknown_merge_state"
            action = "refresh_pr_merge_state"
        else:
            state = "blocked_merge_state"
            action = f"inspect_merge_state:{merge_state}"

        return GithubPRReadiness(
            state=state,
            recommended_action=action,
            copilot_verdict=copilot_verdict,
            latest_copilot_comment=copilot_comment,
            actionable_comments=actionable_comments,
            review_decision="unknown",
            merge_state=merge_state,
            head_ref=pr.get("head", ""),
            head_sha=pr.get("head_sha", ""),
            url=pr.get("html_url", ""),
        )

    async def create_pr(
        self, repo: str, *, title: str, body: str, head: str, base: str = "main",
        draft: bool = False,
    ) -> dict:
        """Open a PR and return its number + url."""
        owner, name = self._split_repo(repo)
        try:
            resp = await self._client().rest.pulls.async_create(
                owner=owner, repo=name, title=title, body=body,
                head=head, base=base, draft=draft,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            raise GithubClientError(f"create_pr({repo}, {head}→{base}): {e}") from e
        pr = resp.parsed_data
        return {"number": pr.number, "html_url": pr.html_url}

    async def merge_pr(
        self, repo: str, pr_number: int, *,
        method: str = "squash",
        commit_title: str | None = None,
        commit_message: str | None = None,
        admin_bypass: bool = False,
    ) -> dict:
        """Merge a PR. ``method`` is ``merge|squash|rebase``.

        **On branch protection:** the REST merge endpoint doesn't have a
        literal "admin bypass" flag — bypass happens server-side if the
        authenticated token has ``bypass_pull_request_allowances`` on the
        protection rule. ``admin_bypass=True`` is a **semantic marker**
        on the caller side: it declares "I know this merge bypasses
        protection, and that's intentional." When True, the merge is
        logged at INFO so the bypass is visible in audit trails. When
        False and protection blocks the merge, we raise
        :class:`GithubMergeBlockedError` with a clear cause rather than
        surfacing GitHub's 405 as a generic error.
        """
        owner, name = self._split_repo(repo)
        kwargs: dict = {"owner": owner, "repo": name, "pull_number": pr_number,
                        "merge_method": method}
        if commit_title:
            kwargs["commit_title"] = commit_title
        if commit_message:
            kwargs["commit_message"] = commit_message

        try:
            resp = await self._client().rest.pulls.async_merge(**kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status == 405:
                raise GithubMergeBlockedError(
                    f"merge_pr({repo}#{pr_number}): branch protection rejected the merge. "
                    f"Pass admin_bypass=True if this is intentional and the token has bypass permission."
                ) from e
            if status == 409:
                raise GithubMergeConflictError(
                    f"merge_pr({repo}#{pr_number}): PR has conflicts or base has moved. Rebase and retry."
                ) from e
            raise GithubClientError(f"merge_pr({repo}#{pr_number}): {e}") from e

        if admin_bypass:
            logger.info(
                "merge_pr(%s#%d): admin-bypass merge via %s, sha=%s",
                repo, pr_number, method, resp.parsed_data.sha,
            )

        return {
            "merged": resp.parsed_data.merged,
            "sha": resp.parsed_data.sha,
            "message": resp.parsed_data.message,
            "admin_bypass": admin_bypass,
        }

    async def get_authenticated_user_login(self) -> str:
        """Return the login for the authenticated token, or ``""`` when
        running unauthenticated.

        Callers (notably :mod:`developer.pr_watcher`) use the login to
        filter out self-authored comments from event emission — an
        empty token discovery (``token=None``) yields an empty string,
        which naturally disables the filter (no login ever matches).
        Errors are swallowed to the same fallback so a transient API
        hiccup can't take down the watcher on a filter-only path.
        """
        if not self._token:
            return ""
        try:
            resp = await self._client().rest.users.async_get_authenticated()
        except asyncio.CancelledError:
            # Degrade-to-empty is for transient API failures, not for
            # cooperative cancellation — let the task actually stop.
            raise
        except Exception as e:
            logger.warning("get_authenticated_user_login failed: %s", e)
            return ""
        login = getattr(resp.parsed_data, "login", None)
        return str(login) if login else ""

    # -- helpers --

    @staticmethod
    def _split_repo(repo: str) -> tuple[str, str]:
        repo = (repo or "").strip()
        if "/" not in repo:
            raise GithubClientError(f"repo must be 'owner/name', got {repo!r}")
        owner, _, name = repo.partition("/")
        owner = owner.strip()
        name = name.strip()
        if not owner or not name:
            raise GithubClientError(
                f"repo must be 'owner/name' with both halves non-empty, got {repo!r}"
            )
        return owner, name


def _latest_copilot_clear_comment(comments: list[dict], *, head_sha: str = "") -> str:
    """Return latest Copilot clear comment body for the current head, if any."""
    clear_markers = (
        "no additional blocking",
        "no blocking issue",
        "no further code changes needed",
        "no additional code changes",
        "don't see additional blocking",
        "do not see additional blocking",
    )
    head_sha_lower = head_sha.lower()
    for comment in reversed(comments):
        user = str(comment.get("user", "")).lower()
        body = str(comment.get("body", ""))
        body_lower = body.lower()
        if not is_copilot_login(user):
            continue
        if not any(marker in body_lower for marker in clear_markers):
            continue
        if head_sha and not _contains_head_sha_reference(body_lower, head_sha_lower):
            continue
        return body
    return ""


def is_copilot_login(login: str) -> bool:
    """Does ``login`` match any known Copilot auth variant?

    Public helper — the single source of truth for Copilot-identity
    matching across developer. Callers include
    :func:`_latest_copilot_clear_comment` (local), the readiness gate
    in :meth:`GithubClient.pr_readiness` (local), and
    :func:`developer.pr_watcher.comment_looks_like_bot_verdict`
    (cross-module).

    Keeping the set in one place means a new Copilot variant only needs
    to be added once. The historical private name (``_is_copilot_login``)
    was promoted per PR #39 Copilot R3 to remove the duplicated login
    set that had drifted between this module and pr_watcher.py.
    """
    return login.lower() in {
        "copilot",
        "copilot-swe-agent",
        "copilot-swe-agent[bot]",
        "copilot-pull-request-reviewer",
        "copilot-pull-request-reviewer[bot]",
    }


def _contains_head_sha_reference(body_lower: str, head_sha_lower: str) -> bool:
    """Return True when body contains short/full SHA as a standalone hex token.

    Callers pass lowercased inputs, so boundary checks use lowercase hex.
    """
    short_sha = head_sha_lower[:7]
    pattern = re.compile(
        rf"(?<![0-9a-f])(?:{re.escape(head_sha_lower)}|{re.escape(short_sha)})(?![0-9a-f])"
    )
    return bool(pattern.search(body_lower))
