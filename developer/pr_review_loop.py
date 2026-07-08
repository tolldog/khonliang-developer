"""Composed PR-iteration primitives (fr_developer_35fe69af).

The canonical PR loop — push, request review, watch, react, merge — has
three pieces that are self-contained and used on every cycle:

- ``request_copilot_review`` lives on :class:`developer.github_client.GithubClient`
  (GraphQL ``requestReviews`` + ``botIds``, idempotent).
- :func:`maybe_update_pr` — single-call "sync my branch to a PR" entry
  point: stage+commit any uncommitted changes, push, create the PR if
  none exists yet, request Copilot review when the code materially
  changed.
- :func:`merge_pr_and_sync` — squash-merge + delete branch + fire the
  existing FR auto-completion hook (:meth:`DeveloperAgent._sync_fr_status_on_merge`)
  so a manually-triggered merge gets the same "record in store"
  treatment as one observed by the PR watcher.

**Scoping note (fr_developer_35fe69af):** ``schedule_pr_review_loop``
(subscribe to ``pr.*`` bus events with stale-timeout self-escalation)
and ``dispatch_fix_subagent`` (spawn a Claude subagent from inside
developer) are NOT implemented here. Verified by grepping
``scheduler|fireback|wait_for_event`` across ``developer/``: the only
"scheduler" hits are ``khonliang-scheduler`` (an infra service for LLM
inference scheduling, unrelated) and the fr-drafting keyword list; the
only "fireback" hits are this docstring's own mention. ``bus_wait_for_event``
does exist, but it's a bus-lib primitive an *external* caller uses to
block on one event — not something developer exposes internally as a
persistent, self-escalating background loop (the PR watcher's own
``PRFleetWatcher.run`` is the closest analog, and it's a bespoke asyncio
task, not a reusable subscribe-and-react primitive other skills can
compose). Similarly, there's no primitive anywhere in this codebase for
developer to spawn a Claude subagent — dispatch today happens by handing
a human/Claude session a briefing prompt, not by developer invoking one
itself. Building a half-working subscribe loop or a fake subagent-spawn
on top of infrastructure that isn't there would produce an unmaintainable
stub; both are left as a documented follow-up FR once event-subscription
/ dispatch primitives land. What's here — request + maybe_update + merge
— are the three pieces used on every existing PR cycle per this repo's
CLAUDE.md conventions, and are fully self-contained.

Composes :mod:`developer.git_client` and :mod:`developer.github_client`;
does not reimplement git plumbing (uses ``GitClient.stage`` / ``commit``
/ ``push``, all of which carry the existing branch-match and
protected-branch guards).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


class PrReviewLoopError(RuntimeError):
    """Raised for input/parse failures local to this module (bad URLs,
    unresolvable remotes) — distinct from :class:`GitClientError` /
    :class:`GithubClientError`, which callers may already special-case.
    """


_ORIGIN_URL_RE = re.compile(
    r"(?:git@github\.com:|https://github\.com/)"
    r"(?P<owner>[^/]+)/(?P<name>[^/]+?)(?:\.git)?/?$"
)

_PR_URL_RE = re.compile(
    r"github\.com/(?P<owner>[^/]+)/(?P<name>[^/]+)/pull/(?P<number>\d+)"
)


def parse_owner_repo_from_origin(url: Optional[str]) -> str:
    """Resolve a git remote URL (SSH or HTTPS) to ``"owner/name"``.

    Raises :class:`PrReviewLoopError` for an empty/unrecognized URL —
    ``maybe_update_pr`` needs a real ``owner/name`` before it can talk
    to the GitHub API and a garbage split would surface as a confusing
    downstream 404 instead of a clear parse failure.
    """
    if not url or not url.strip():
        raise PrReviewLoopError("origin url is empty; cannot resolve owner/repo")
    m = _ORIGIN_URL_RE.search(url.strip())
    if not m:
        raise PrReviewLoopError(
            f"origin url {url!r} is not a recognized github.com remote"
        )
    return f"{m.group('owner')}/{m.group('name')}"


def parse_pr_url(pr_url: str) -> tuple[str, int]:
    """Split a GitHub PR URL into ``("owner/name", number)``."""
    m = _PR_URL_RE.search(pr_url or "")
    if not m:
        raise PrReviewLoopError(f"pr_url {pr_url!r} is not a recognized GitHub PR URL")
    return f"{m.group('owner')}/{m.group('name')}", int(m.group("number"))


async def maybe_update_pr(
    cwd: str,
    branch: str,
    *,
    commit_message: str = "",
    auto_request_review: bool = True,
    remote: str = "origin",
    base: str = "main",
    pr_body: str = "",
    git_client: Optional[Any] = None,
    github_client: Optional[Any] = None,
) -> dict:
    """Single-call "sync my branch to a PR" entry point.

    1. Stage + commit any uncommitted changes (modified + untracked
       paths only — deleted paths aren't handled because
       :meth:`GitClient.stage` is add-only; a working tree with only
       deletions pending is left for the caller to commit explicitly).
    2. Push the branch (idempotent — a no-op push when nothing's new).
    3. Create a PR for the branch if none is open yet (conventional
       body), else reuse the existing one.
    4. When ``auto_request_review`` and the code *materially changed*
       (a new commit was made here, the branch already had unpushed
       commits walking in — e.g. from a prior ``git_pr_commit_push`` —
       or the PR was just created), request a Copilot review via the
       idempotent GraphQL wrapper. A clean, already-pushed branch with
       an existing PR is a no-op re-check: nothing new for Copilot to
       look at, so no review request fires.

    Returns a dict: ``pr_url``, ``pr_number``, ``repo``, ``created``,
    ``committed``, ``review_requested``, ``already_requested``.

    Raises :class:`GitClientError` (protected-branch / branch-mismatch
    guards), :class:`GithubClientError` (API failures), or
    :class:`PrReviewLoopError` (unparseable origin URL) — the caller
    (agent handler) is expected to catch and translate to a structured
    error response, matching every other git/github skill in this repo.
    """
    from developer.git_client import GitClient, GitGuardError
    from developer.github_client import GithubClient

    git = git_client if git_client is not None else GitClient(cwd)
    gh = github_client if github_client is not None else GithubClient()

    current = git.current_branch()
    if current != branch:
        raise GitGuardError(
            f"maybe_update_pr expects branch {branch!r}, cwd is on "
            f"{current!r}. Switch branches or correct the call."
        )

    status = git.status()
    changed_paths = sorted(set(status.modified) | set(status.untracked))
    committed = False
    if changed_paths:
        message = commit_message.strip() or f"chore: sync {branch}"
        git.stage(changed_paths)
        git.commit(message, branch_hint=branch)
        committed = True
    # Commits made locally before this call (e.g. via a prior
    # ``git_pr_commit_push``) but never pushed also count as "local work
    # not yet in the PR" — captured before the push below so it reflects
    # what was ahead walking in, not the (always zero) state after.
    had_unpushed_commits = status.ahead > 0

    # Push is idempotent — "Everything up-to-date" is a normal outcome,
    # not an error, when this call finds nothing new to send.
    git.push(remote=remote, branch=branch, set_upstream=True)

    repo = parse_owner_repo_from_origin(git.origin_url())

    existing = await gh.find_open_pr_for_branch(repo, branch)
    created = False
    if existing:
        pr_number = existing["number"]
        pr_url = existing["html_url"]
    else:
        title = commit_message.strip() or branch
        body = pr_body or f"Automated PR for `{branch}` via maybe_update_pr."
        opened = await gh.create_pr(repo, title=title, body=body, head=branch, base=base)
        pr_number = opened["number"]
        pr_url = opened["html_url"]
        created = True

    materially_changed = committed or created or had_unpushed_commits
    review_requested = False
    already_requested = False
    if auto_request_review and materially_changed:
        review = await gh.request_copilot_review(repo, pr_number)
        review_requested = review["requested"]
        already_requested = review["already_requested"]

    return {
        "pr_url": pr_url,
        "pr_number": pr_number,
        "repo": repo,
        "created": created,
        "committed": committed,
        "review_requested": review_requested,
        "already_requested": already_requested,
    }


OnMergedFn = Callable[[str, int, str], Awaitable[None]]


async def merge_pr_and_sync(
    pr_url: str,
    *,
    delete_branch: bool = True,
    merge_method: str = "squash",
    github_client: Optional[Any] = None,
    on_merged: Optional[OnMergedFn] = None,
) -> dict:
    """Squash-merge a PR, optionally delete its branch, then "record in
    store" via ``on_merged``.

    ``on_merged`` is the same ``(repo, pr_number, title) -> None`` shape
    the PR watcher already uses for FR auto-completion
    (:meth:`DeveloperAgent._sync_fr_status_on_merge`) — reusing that
    hook here means a manually-triggered merge gets the identical
    "FR named in the title advances to completed" side effect a
    watcher-observed merge gets, without a second, parallel bookkeeping
    mechanism. Best-effort: an ``on_merged`` failure is logged, not
    raised — the merge itself already succeeded and shouldn't be
    reported as failed because a downstream sync hiccuped.

    Branch deletion is also best-effort: a failure there (e.g. the
    branch was already deleted, or protection rules block it) doesn't
    unwind the merge — the merge is the state that matters.
    """
    from developer.github_client import GithubClient, GithubClientError

    gh = github_client if github_client is not None else GithubClient()
    repo, pr_number = parse_pr_url(pr_url)

    pr = await gh.get_pr(repo, pr_number)
    title = str(pr.get("title") or "")
    head_ref = str(pr.get("head") or "")

    merge_result = await gh.merge_pr(repo, pr_number, method=merge_method)

    branch_deleted = False
    if delete_branch and head_ref:
        try:
            branch_deleted = await gh.delete_branch(repo, head_ref)
        except GithubClientError as e:
            logger.warning(
                "merge_pr_and_sync(%s#%d): branch delete failed (best-effort): %s",
                repo, pr_number, e,
            )

    if on_merged is not None:
        try:
            await on_merged(repo, pr_number, title)
        except Exception as e:
            logger.warning(
                "merge_pr_and_sync(%s#%d): on_merged hook failed: %s",
                repo, pr_number, e,
            )

    return {
        "repo": repo,
        "pr_number": pr_number,
        "merged": bool(merge_result.get("merged", False)),
        "sha": str(merge_result.get("sha") or ""),
        "branch": head_ref,
        "branch_deleted": branch_deleted,
    }
