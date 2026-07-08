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

**Fork PRs are out of scope** (fr_developer_35fe69af Codex R1/R2 follow-up):
this repo's workflow is sole-maintainer, always-push-to-origin, no fork
PRs. ``merge_pr_and_sync`` only deletes the head branch when ``head_repo``
is *positively confirmed* to equal ``base_repo``; a genuine mismatch
skips with ``"note": "fork_pr_unsupported"`` and an empty/unknown
``head_repo`` (e.g. a since-deleted fork) skips with
``"note": "head_repo_unknown"`` — neither case is treated as "safe to
delete," since an unknown answer is not a confirmed same-repo match.
Full fork support (resolving ``delete_branch``/``create_pr`` against the
head repo) is left for a follow-up FR (fr_developer_00259318) if this
repo ever needs it.

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
    r"(?:git@github\.com:|https://(?:[^@/]+@)?github\.com/"
    r"|ssh://(?:[^@/]+@)?github\.com(?::\d+)?/)"
    r"(?P<owner>[^/]+)/(?P<name>[^/]+?)(?:\.git)?/?$"
)

_PR_URL_RE = re.compile(
    r"github\.com/(?P<owner>[^/]+)/(?P<name>[^/]+)/pull/(?P<number>\d+)"
)


def parse_owner_repo_from_origin(url: Optional[str]) -> str:
    """Resolve a GitHub remote URL (SSH or HTTPS) to ``"owner/name"``.

    Despite the name (kept for compatibility — it was written when
    ``maybe_update_pr`` only ever looked at ``origin``), this works for
    any remote's URL: the caller passes whichever remote's URL is
    actually relevant, e.g. ``git.remote_url(remote)`` for the remote
    that was actually pushed to (see ``maybe_update_pr`` below).

    Handles authenticated HTTPS remotes (Codex R2 on PR #88: token-based
    clones — CI, PAT-based remotes — commonly look like
    ``https://x-access-token:TOKEN@github.com/owner/repo.git`` or
    ``https://user@github.com/owner/repo.git``; the userinfo segment
    before ``@github.com`` is stripped before matching owner/repo,
    rather than causing the whole URL to fail to match) and the
    ``ssh://git@github.com/owner/repo.git`` (optionally with a
    ``:port``) SSH form alongside the ``git@github.com:owner/repo.git``
    shorthand (Codex R3 on PR #88 — this repo's own origin uses the
    shorthand form, but both are standard GitHub SSH remote shapes).

    Raises :class:`PrReviewLoopError` for an empty/unrecognized URL —
    ``maybe_update_pr`` needs a real ``owner/name`` before it can talk
    to the GitHub API and a garbage split would surface as a confusing
    downstream 404 instead of a clear parse failure.
    """
    if not url or not url.strip():
        raise PrReviewLoopError("remote url is empty; cannot resolve owner/repo")
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

    1. Stage + commit any uncommitted changes — modified, untracked, or
       already-staged paths all count (staging only happens for the
       modified/untracked set; already-staged paths are committed as-is).
       Deleted paths aren't auto-staged because :meth:`GitClient.stage`
       is add-only; a working tree with only deletions pending is left
       for the caller to commit explicitly.
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
    # ``staged`` is included: a caller that already ran `git add` (or a
    # prior partial ``git_stage`` call) before invoking this helper still
    # needs those changes committed — this API is documented as
    # "stage+commit uncommitted changes," and staged-but-uncommitted is
    # squarely "uncommitted" (Codex R2 on PR #88: previously only
    # modified/untracked were checked, so pre-staged changes were
    # silently skipped — no commit, no push, no review request).
    # ``deleted`` covers a tracked file removed from disk but not yet
    # ``git rm``'d — status.modified/.untracked alone miss it, so a
    # local deletion silently never got committed/pushed (Codex R3 on
    # PR #88).
    unstaged_paths = sorted(
        set(status.modified) | set(status.untracked) | set(status.deleted)
    )
    already_staged = bool(status.staged)
    committed = False
    if unstaged_paths or already_staged:
        message = commit_message.strip() or f"chore: sync {branch}"
        if unstaged_paths:
            git.stage(unstaged_paths)
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

    # Resolve owner/repo from the URL of the remote we actually pushed
    # to — NOT always "origin" (Codex R1 on PR #88: a caller with a
    # second configured remote passing remote="upstream" (say) would
    # previously still get the PR opened/reviewed against origin's
    # repo, silently wrong).
    repo = parse_owner_repo_from_origin(git.remote_url(remote))

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

    **Fork PRs (and unknown head repos) are explicitly unsupported for
    branch deletion** (fr_developer_35fe69af Codex R1 + R2): this repo's
    workflow always pushes to ``origin`` and never opens fork-based PRs,
    so ``delete_branch(repo, head_ref)`` assumes the head branch lives in
    the same repo the PR was opened against. For a fork-based PR the
    head branch actually lives in the fork, and blindly deleting
    ``head_ref`` against the base repo would either no-op (branch of
    that name doesn't exist there) or, worse, delete an unrelated
    same-named branch that does. Rather than build full fork support,
    this only deletes when ``get_pr``'s ``head_repo`` is *positively
    confirmed* to match ``base_repo`` (case-insensitive). Two cases skip
    the delete instead:

    - ``head_repo != base_repo`` — a genuine fork PR
      (``"note": "fork_pr_unsupported"``).
    - ``head_repo`` is empty — GitHub doesn't know (or no longer knows,
      e.g. the source fork was deleted after merge) which repo the head
      branch lives in. Codex R2 flagged that the earlier version treated
      this as "same repo" and fell through to deleting — an empty
      answer is UNKNOWN, not a confirmed same-repo match, so it gets the
      same safe skip (``"note": "head_repo_unknown"``).

    Either way: fail loud/safe, not silent/wrong.
    """
    from developer.github_client import GithubClient, GithubClientError

    gh = github_client if github_client is not None else GithubClient()
    repo, pr_number = parse_pr_url(pr_url)

    pr = await gh.get_pr(repo, pr_number)
    title = str(pr.get("title") or "")
    head_ref = str(pr.get("head") or "")
    head_repo = str(pr.get("head_repo") or "")
    base_repo = str(pr.get("base_repo") or "") or repo
    # Only a POSITIVELY CONFIRMED same-repo match is safe to delete
    # against. An empty head_repo is "unknown", not "same repo".
    same_repo_confirmed = bool(head_repo) and head_repo.lower() == base_repo.lower()

    merge_result = await gh.merge_pr(repo, pr_number, method=merge_method)

    branch_deleted = False
    note = ""
    if delete_branch and head_ref:
        if not same_repo_confirmed:
            if head_repo:
                note = "fork_pr_unsupported"
                logger.warning(
                    "merge_pr_and_sync(%s#%d): head repo %r differs from "
                    "base repo %r (fork PR) — skipping branch delete, "
                    "this workflow doesn't support fork PRs yet",
                    repo, pr_number, head_repo, base_repo,
                )
            else:
                note = "head_repo_unknown"
                logger.warning(
                    "merge_pr_and_sync(%s#%d): head repo unknown (empty) "
                    "— skipping branch delete rather than assuming it's "
                    "safe to delete against the base repo",
                    repo, pr_number,
                )
        else:
            try:
                branch_deleted = await gh.delete_branch(repo, head_ref)
            except GithubClientError as e:
                logger.warning(
                    "merge_pr_and_sync(%s#%d): branch delete failed "
                    "(best-effort): %s",
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
        "note": note,
    }
