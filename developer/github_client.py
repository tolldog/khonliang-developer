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

import logging
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


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
    """A single inline review comment on a file:line."""

    id: int
    pr_number: int
    repo: str
    reviewer: str
    path: str                        # file path within the repo
    line: int | None                 # target line; None for file-level comments
    body: str
    created_at: str


class GithubClientError(RuntimeError):
    """Raised on auth/transport failures so callers can distinguish from
    domain errors (404 from GitHub is a transport concern, not a bug)."""


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
                    submitted_at=str(r.submitted_at),
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
        """
        owner, name = self._split_repo(repo)
        try:
            resp = await self._client().rest.pulls.async_list_review_comments(
                owner=owner, repo=name, pull_number=pr_number,
            )
        except Exception as e:
            raise GithubClientError(
                f"list_pr_review_comments({repo}#{pr_number}): {e}"
            ) from e

        return [
            GithubReviewComment(
                id=c.id,
                pr_number=pr_number,
                repo=repo,
                reviewer=c.user.login if c.user else "unknown",
                path=c.path,
                line=getattr(c, "line", None),
                body=c.body or "",
                created_at=str(c.created_at),
            )
            for c in resp.parsed_data
        ]

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
        try:
            resp = await self._client().rest.issues.async_list_comments(
                owner=owner, repo=name, issue_number=pr_number,
            )
        except Exception as e:
            raise GithubClientError(f"list_pr_issue_comments({repo}#{pr_number}): {e}") from e
        return [
            {
                "id": c.id,
                "user": c.user.login if c.user else "unknown",
                "body": c.body or "",
                "created_at": str(c.created_at),
            }
            for c in resp.parsed_data
        ]

    async def get_pr(self, repo: str, pr_number: int) -> dict:
        """Return normalized PR metadata: state, mergeable, title, author, head, base, draft."""
        owner, name = self._split_repo(repo)
        try:
            resp = await self._client().rest.pulls.async_get(
                owner=owner, repo=name, pull_number=pr_number,
            )
        except Exception as e:
            raise GithubClientError(f"get_pr({repo}#{pr_number}): {e}") from e
        pr = resp.parsed_data
        return {
            "number": pr.number,
            "title": pr.title,
            "state": pr.state,
            "draft": pr.draft,
            "mergeable": pr.mergeable,
            "mergeable_state": getattr(pr, "mergeable_state", None),
            "author": pr.user.login if pr.user else "unknown",
            "head": pr.head.ref,
            "base": pr.base.ref,
            "html_url": pr.html_url,
        }

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
        except Exception as e:
            raise GithubClientError(f"create_pr({repo}, {head}→{base}): {e}") from e
        pr = resp.parsed_data
        return {"number": pr.number, "html_url": pr.html_url}

    async def merge_pr(
        self, repo: str, pr_number: int, *,
        method: str = "squash", commit_title: str | None = None, commit_message: str | None = None,
    ) -> dict:
        """Merge a PR. ``method`` is ``merge|squash|rebase``.

        Note: does not --admin-bypass branch protection; callers that need
        that should use the REST ``/merge`` endpoint with a token that has
        bypass permission. Route-switchable if it becomes load-bearing.
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
        except Exception as e:
            raise GithubClientError(f"merge_pr({repo}#{pr_number}): {e}") from e
        return {
            "merged": resp.parsed_data.merged,
            "sha": resp.parsed_data.sha,
            "message": resp.parsed_data.message,
        }

    # -- helpers --

    @staticmethod
    def _split_repo(repo: str) -> tuple[str, str]:
        if "/" not in repo:
            raise GithubClientError(f"repo must be 'owner/name', got {repo!r}")
        owner, _, name = repo.partition("/")
        return owner, name
