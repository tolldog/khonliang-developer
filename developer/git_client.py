"""Native git client for developer.

Parallel to ``github_client.py``: per ``feedback_prefer_python_modules_over_execs``,
developer reaches for the Python SDK (``GitPython``) rather than shelling
out to ``git``. Single entry point so authentication, error normalization,
and destructive-op guardrails live in one place.

**Scope of this module:** common git operations a dispatched-Claude,
PR watcher, or stacked-PR workflow would run. Everything here calls
into ``git.Repo`` under the hood; we normalize into small dataclasses
so callers don't touch GitPython types directly.

**Destructive-op pattern** (mirrors ``admin_bypass`` on ``github_client.merge_pr``):
- ``delete_branch(force=False)``: refuses unmerged branches without force
- ``push(force=False)``: refuses ``--force`` without explicit opt-in
- ``amend`` / hard reset / force push: all require explicit flags, logged at INFO

**Typed errors** (mirrors github_client's typed exceptions) so callers can
switch on the specific cause without parsing messages.
"""

from __future__ import annotations

import logging
import os.path
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _is_wildcard_pathspec(p: str) -> bool:
    """True for ``.`` and any equivalent (``./``, ``.//``, etc).

    The bare ``.`` check would be bypassed by trailing-separator
    forms like ``./`` — ``os.path.normpath`` collapses them all
    back to ``.`` so the guard catches every variant.
    """
    return os.path.normpath(p) == "."


def _resolve_push_dst_branch(branch: str) -> str:
    """Pull the destination branch out of a push refspec.

    Refspecs come in two shapes:

    * Bare branch name (``"feat/x"``) — src and dst are the same;
      return as-is.
    * Colon form (``"<src>:<dst>"``) — return only the dst. ``HEAD:main``
      and ``main:main`` both push to ``main``, so the protected-branch
      check has to look at the dst, not the literal arg.

    ``refs/heads/main`` is normalized to ``main`` so a ref-style dst
    triggers the same guard. ``refs/tags/...`` is left alone (tag pushes
    aren't branch pushes).
    """
    dst = branch.split(":", 1)[1] if ":" in branch else branch
    if dst.startswith("refs/heads/"):
        dst = dst[len("refs/heads/"):]
    return dst


# ---------------------------------------------------------------------------
# Dataclasses — normalized shapes so consumers don't touch GitPython types
# ---------------------------------------------------------------------------


@dataclass
class RepoStatus:
    """Working-tree status summary."""
    branch: str                        # current branch (detached → "HEAD")
    is_dirty: bool                     # any uncommitted changes
    untracked: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    staged: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    ahead: int = 0                     # commits ahead of upstream (if tracking)
    behind: int = 0                    # commits behind upstream
    detached: bool = False


@dataclass
class GitCommit:
    """A commit in the repository."""
    sha: str
    short_sha: str
    author: str                        # "Name <email>"
    committed_at: str                  # ISO8601
    message: str                       # first line (subject)
    full_message: str                  # subject + body


@dataclass
class GitBranch:
    """A branch (local or remote)."""
    name: str                          # e.g. "main" or "origin/feat/x"
    is_remote: bool
    head_sha: str
    is_current: bool = False


# ---------------------------------------------------------------------------
# Errors — typed so callers can handle specific cases
# ---------------------------------------------------------------------------


class GitClientError(RuntimeError):
    """Base class for git client errors."""


class GitNotFoundError(GitClientError):
    """Ref / branch / remote / repo doesn't exist."""


class GitUncommittedError(GitClientError):
    """Destructive op attempted against a dirty working tree."""


class GitConflictError(GitClientError):
    """Merge / rebase / cherry-pick produced a conflict."""


class GitUpstreamError(GitClientError):
    """Push rejected (non-fast-forward without --force, missing upstream, etc.)."""


class GitDestructiveError(GitClientError):
    """Destructive op attempted without the required explicit flag."""


class GitGuardError(GitClientError):
    """Operation refused by a primitive-level safety guard.

    Distinct from GitDestructiveError so callers can catch
    "I refused for a structural reason" (wrong branch,
    protected push, wildcard staging) separately from
    "you asked me to do something destructive". The error
    message names the recovery path.
    """


# Branches that ``push`` refuses to write to without explicit
# ``allow_main=True``. Single source of truth so callers and tests
# agree. Adding a project-level branch_protection config is a
# future extension; keeping the list constant avoids a config
# round-trip in the hot path.
PROTECTED_BRANCHES: tuple[str, ...] = ("main", "master")

# Wildcard pathspec — bulk-add the entire working tree. Refused
# unless ``allow_all=True`` so a wrong-cwd ``stage(["."])`` can't
# quietly capture unrelated untracked files. The recovery path is
# explicit relative paths.
_STAGE_WILDCARD_PATHSPEC: str = "."

# CLI flags that callers sometimes try to pass as pathspecs after
# composing a chained shell command in their head ("git add -A").
# These are not pathspecs at all — ``repo.index.add(["-A"])`` would
# silently fail to do what the caller meant — so refuse them
# unconditionally with a message that points at the right primitive.
_STAGE_CLI_FLAG_TOKENS: frozenset[str] = frozenset({
    "-A", "--all", "-u", "--update",
})


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class GitClient:
    """Thin wrapper over ``git.Repo``.

    Lazy-imports GitPython so the module stays cheap to import and tests
    that don't touch git can skip the dependency.

    One client per repository path. Each public method takes simple
    arguments and returns normalized dataclasses or plain dicts — callers
    don't need to know GitPython's object model.
    """

    def __init__(self, cwd: str | Path):
        self.cwd = Path(cwd).resolve()
        self._repo: Any = None  # lazy: git.Repo

    def _get_repo(self) -> Any:
        if self._repo is None:
            try:
                from git import Repo
                from git.exc import InvalidGitRepositoryError, NoSuchPathError
            except ImportError as e:
                raise GitClientError(
                    "GitPython is not installed; add it to pyproject.toml or "
                    "remove the git_client dependency."
                ) from e

            if not self.cwd.exists():
                raise GitNotFoundError(f"path does not exist: {self.cwd}")
            try:
                self._repo = Repo(str(self.cwd))
            except InvalidGitRepositoryError as e:
                raise GitNotFoundError(f"not a git repository: {self.cwd}") from e
            except NoSuchPathError as e:
                raise GitNotFoundError(f"path does not exist: {self.cwd}") from e
        return self._repo

    # ------------------------------------------------------------------
    # Introspection — safe, read-only
    # ------------------------------------------------------------------

    def status(self) -> RepoStatus:
        """Return a structured working-tree status snapshot.

        Uses ``git status --porcelain=v1`` under the hood for stable
        parseable output — cleaner than GitPython's index-diff semantics
        (which reverse direction conventions depending on how you call
        them). Porcelain v1 is a documented, stable format.
        """
        repo = self._get_repo()

        detached = repo.head.is_detached
        branch_name = "HEAD" if detached else repo.active_branch.name

        # Parse porcelain v1: two-char status + space + path (or two paths for rename).
        # XY format: X=index status, Y=working-tree status.
        # "??" = untracked. Space = unmodified.
        raw = repo.git.status("--porcelain=v1", "-z")
        untracked: list[str] = []
        modified: list[str] = []
        staged: list[str] = []
        deleted: list[str] = []

        if raw:
            # -z uses \0 separators; rename/copy entries have two paths \0-separated.
            entries = raw.split("\x00")
            i = 0
            while i < len(entries):
                entry = entries[i]
                if not entry:
                    i += 1
                    continue
                # status code is first 2 chars, then space, then path
                if len(entry) < 3:
                    i += 1
                    continue
                code = entry[:2]
                path = entry[3:]
                # Rename entries have X in 'R'/'C'; the old path comes in the next entry
                if code[0] in ("R", "C"):
                    i += 1  # skip the old path entry
                if code == "??":
                    untracked.append(path)
                else:
                    index_status, wt_status = code[0], code[1]
                    # X (index) = staged changes; Y (working-tree) = unstaged
                    if index_status == "D":
                        deleted.append(path)
                    elif index_status != " " and index_status != "?":
                        staged.append(path)
                    if wt_status == "D":
                        if path not in deleted:
                            deleted.append(path)
                    elif wt_status != " " and wt_status != "?":
                        if path not in modified:
                            modified.append(path)
                i += 1

        # ahead / behind against the tracking branch, if any.
        ahead = behind = 0
        if not detached:
            try:
                tracking = repo.active_branch.tracking_branch()
                if tracking is not None:
                    # ahead = our commits not in upstream; behind = upstream not in us
                    ahead_count = sum(1 for _ in repo.iter_commits(
                        f"{tracking}..{repo.active_branch}"
                    ))
                    behind_count = sum(1 for _ in repo.iter_commits(
                        f"{repo.active_branch}..{tracking}"
                    ))
                    ahead = ahead_count
                    behind = behind_count
            except Exception:
                pass

        is_dirty = bool(untracked or modified or staged or deleted)
        return RepoStatus(
            branch=branch_name,
            is_dirty=is_dirty,
            untracked=untracked,
            modified=modified,
            staged=staged,
            deleted=deleted,
            ahead=ahead,
            behind=behind,
            detached=detached,
        )

    def current_branch(self) -> str:
        """Return the active branch name, or 'HEAD' if detached."""
        repo = self._get_repo()
        if repo.head.is_detached:
            return "HEAD"
        return repo.active_branch.name

    def origin_url(self) -> str | None:
        """Return the configured origin URL, or None when origin is absent."""
        repo = self._get_repo()
        try:
            remote = repo.remotes.origin
        except (AttributeError, IndexError):
            return None
        try:
            urls = list(remote.urls)
        except Exception as e:
            raise GitClientError(f"failed to read origin URL: {e}") from e
        return urls[0] if urls else None

    def list_branches(self, *, local: bool = True, remote: bool = False) -> list[GitBranch]:
        """List branches. Pass local=True, remote=True, or both."""
        repo = self._get_repo()
        out: list[GitBranch] = []

        current = None if repo.head.is_detached else repo.active_branch.name

        if local:
            for head in repo.heads:
                out.append(GitBranch(
                    name=head.name,
                    is_remote=False,
                    head_sha=head.commit.hexsha,
                    is_current=(head.name == current),
                ))
        if remote:
            for r in repo.remotes:
                for ref in r.refs:
                    # Skip the symbolic HEAD ref (e.g. "origin/HEAD")
                    if ref.name.endswith("/HEAD"):
                        continue
                    out.append(GitBranch(
                        name=ref.name,
                        is_remote=True,
                        head_sha=ref.commit.hexsha,
                        is_current=False,
                    ))
        return out

    def log(
        self, *, ref: str = "HEAD", limit: int = 20, paths: Optional[list[str]] = None,
    ) -> list[GitCommit]:
        """Return commits reachable from ``ref`` (newest first).

        ``paths`` restricts to commits touching those paths.
        """
        repo = self._get_repo()
        try:
            iter_kwargs: dict[str, Any] = {"max_count": limit}
            if paths:
                iter_kwargs["paths"] = paths
            commits = list(repo.iter_commits(ref, **iter_kwargs))
        except Exception as e:
            raise GitClientError(f"log failed for {ref}: {e}") from e
        return [_to_commit(c) for c in commits]

    def show(self, ref: str) -> GitCommit:
        """Return the commit ``ref`` resolves to."""
        repo = self._get_repo()
        try:
            commit = repo.commit(ref)
        except Exception as e:
            raise GitNotFoundError(f"unknown ref: {ref}") from e
        return _to_commit(commit)

    def rev_parse(self, ref: str) -> str:
        """Return the full sha that ``ref`` resolves to."""
        repo = self._get_repo()
        try:
            return repo.git.rev_parse(ref)
        except Exception as e:
            raise GitNotFoundError(f"unknown ref: {ref}") from e

    def diff(
        self, ref_a: str = "HEAD", ref_b: Optional[str] = None,
        *, paths: Optional[list[str]] = None,
    ) -> str:
        """Return a unified diff string.

        - ``diff(ref_a)`` → working tree vs ref_a
        - ``diff(ref_a, ref_b)`` → ref_a..ref_b
        - ``paths`` restricts to those files
        """
        repo = self._get_repo()
        try:
            if ref_b is None:
                args = [ref_a]
            else:
                args = [f"{ref_a}..{ref_b}"]
            if paths:
                args.append("--")
                args.extend(paths)
            return repo.git.diff(*args)
        except Exception as e:
            raise GitClientError(f"diff failed: {e}") from e

    def diff_staged(self) -> str:
        """Return the unified diff of staged changes (``git diff --cached``).

        Feeds review_staged_diff — the "review what I'm about to commit"
        skill that closes fr_developer_6ecd0c01. Empty string when nothing
        is staged; callers decide whether that's an error.
        """
        repo = self._get_repo()
        try:
            return repo.git.diff("--cached")
        except Exception as e:
            raise GitClientError(f"diff --cached failed: {e}") from e

    # ------------------------------------------------------------------
    # Mutating operations — with guardrails on destructive ones
    # ------------------------------------------------------------------

    def checkout(self, ref: str, *, new_branch: bool = False) -> str:
        """Check out ``ref``. With ``new_branch=True``, create + switch.

        Refuses to switch when the working tree has **any** changes
        (tracked or untracked) that could be lost or clobbered — matches
        git's own safety behavior and raises :class:`GitUncommittedError`.
        Commit or stash (or delete the untracked files) before switching.
        """
        repo = self._get_repo()
        if repo.is_dirty(untracked_files=True):
            raise GitUncommittedError(
                "working tree has uncommitted or untracked changes; "
                "commit, stash, or clean them before checkout"
            )
        try:
            if new_branch:
                new = repo.create_head(ref)
                new.checkout()
            else:
                repo.git.checkout(ref)
        except Exception as e:
            msg = str(e)
            if "already exists" in msg.lower():
                raise GitClientError(f"branch {ref!r} already exists") from e
            if "did not match" in msg.lower() or "unknown revision" in msg.lower():
                raise GitNotFoundError(f"unknown ref: {ref}") from e
            raise GitClientError(f"checkout failed: {e}") from e
        return self.current_branch()

    def create_branch(self, name: str, *, base: Optional[str] = None) -> str:
        """Create (but don't check out) a new branch.

        ``base`` defaults to current HEAD.
        """
        repo = self._get_repo()
        if name in [h.name for h in repo.heads]:
            raise GitClientError(f"branch {name!r} already exists")
        try:
            if base:
                repo.create_head(name, base)
            else:
                repo.create_head(name)
        except Exception as e:
            raise GitClientError(f"create_branch failed: {e}") from e
        return name

    def delete_branch(self, name: str, *, force: bool = False) -> str:
        """Delete a local branch.

        ``force=False`` refuses to delete unmerged branches (matches
        ``git branch -d`` behavior). ``force=True`` forces deletion and
        is logged at INFO for audit.
        """
        repo = self._get_repo()
        if name not in [h.name for h in repo.heads]:
            raise GitNotFoundError(f"local branch {name!r} does not exist")
        if name == self.current_branch():
            raise GitClientError(f"cannot delete the current branch {name!r}")

        try:
            if force:
                logger.info("delete_branch(force=True): %s in %s", name, self.cwd)
                repo.delete_head(name, force=True)
            else:
                try:
                    repo.delete_head(name)
                except Exception as e:
                    if "not fully merged" in str(e).lower():
                        raise GitClientError(
                            f"branch {name!r} is not fully merged; "
                            f"pass force=True to delete anyway"
                        ) from e
                    raise
        except GitClientError:
            raise
        except Exception as e:
            raise GitClientError(f"delete_branch failed: {e}") from e
        return name

    def stage(self, paths: list[str], *, allow_all: bool = False) -> list[str]:
        """Stage the given paths (relative to repo root).

        ``paths`` must be a non-empty list of explicit paths. The
        wildcard pathspec ``.`` is refused unless ``allow_all=True`` —
        a chained shell pipeline that composed ``git add -A`` from a
        wrong cwd captures unrelated untracked files and produces
        commits whose content doesn't match the message. Explicit
        paths force the caller to know what they're staging.

        CLI flags like ``-A`` / ``--all`` / ``-u`` / ``--update``
        are refused unconditionally: those are git command-line
        switches, not pathspecs, so passing them through to
        ``repo.index.add`` would silently fail to do what the caller
        meant. The recovery path is ``stage(["."], allow_all=True)``
        for bulk-add.
        """
        if not paths:
            raise GitGuardError(
                "stage requires explicit paths; pass the files to add or "
                "use stage(['.'], allow_all=True) to opt into a bulk add"
            )
        cli_flags = [p for p in paths if p in _STAGE_CLI_FLAG_TOKENS]
        if cli_flags:
            raise GitGuardError(
                f"stage refused CLI flag tokens {cli_flags!r}: those are "
                f"git command-line switches, not pathspecs. Use "
                f"stage(['.'], allow_all=True) for bulk-add."
            )
        # ``_is_wildcard_pathspec`` normalizes equivalents (``./``,
        # ``.//``) — without that, callers could bypass the guard with
        # a trailing-separator variant.
        wildcards = [p for p in paths if _is_wildcard_pathspec(p)]
        if wildcards and not allow_all:
            raise GitGuardError(
                f"stage refused wildcard pathspec {wildcards!r}: pass "
                f"explicit relative paths, or set allow_all=True if you "
                f"really want to capture every change in the working tree"
            )
        repo = self._get_repo()
        try:
            repo.index.add(paths)
        except Exception as e:
            raise GitClientError(f"stage failed: {e}") from e
        return list(paths)

    def unstage(self, paths: list[str]) -> list[str]:
        """Unstage the given paths (keeps working-tree changes)."""
        repo = self._get_repo()
        try:
            repo.git.reset("HEAD", "--", *paths)
        except Exception as e:
            raise GitClientError(f"unstage failed: {e}") from e
        return list(paths)

    def commit(
        self, message: str, *,
        author: Optional[str] = None,
        co_authors: Optional[list[str]] = None,
        amend: bool = False,
        branch_hint: str = "",
    ) -> GitCommit:
        """Commit the staged changes.

        ``co_authors`` is a list of ``Name <email>`` strings appended as
        ``Co-Authored-By:`` trailers.

        ``amend=True`` rewrites the previous commit — destructive if that
        commit is already pushed. Logged at INFO.

        ``branch_hint`` (optional): if set, refuse the commit when the
        cwd's current branch doesn't match. Catches the wrong-cwd
        composition where a chained pipeline references one branch in
        the message but lands the commit on a different branch.
        Empty string disables the check (legacy callers).
        """
        repo = self._get_repo()
        if not message or not message.strip():
            raise GitClientError("commit requires a non-empty message")
        if branch_hint:
            current = self.current_branch()
            if current != branch_hint:
                raise GitGuardError(
                    f"branch_hint mismatch: expected {branch_hint!r}, "
                    f"cwd is on {current!r}. Refusing the commit so a "
                    f"wrong-cwd pipeline doesn't land work on the wrong "
                    f"branch. Switch branches or correct the hint."
                )

        # Pre-check for empty staging — GitPython's index.commit happily
        # creates empty commits; we want to refuse explicitly so callers
        # don't silently accumulate no-ops.
        if not amend:
            has_staged = False
            try:
                head_tree = repo.head.commit.tree
                if list(repo.index.diff(head_tree)):
                    has_staged = True
            except Exception:
                # No HEAD yet (first commit); treat any indexed file as staged
                has_staged = len(repo.index.entries) > 0
            if not has_staged:
                raise GitClientError("no staged changes to commit")

        full_message = message.rstrip()
        if co_authors:
            full_message += "\n"
            for co in co_authors:
                full_message += f"\nCo-Authored-By: {co}"

        try:
            if amend:
                logger.info("commit(amend=True) in %s", self.cwd)
                repo.git.commit("--amend", "-m", full_message)
                # After amend, HEAD is the new commit — fetch it
                commit = repo.head.commit
            else:
                # If an author override is supplied, use GitPython's Actor
                # object rather than mutating os.environ (which would leak
                # into unrelated operations in a long-running agent).
                commit_kwargs: dict[str, Any] = {}
                if author:
                    from git import Actor
                    name = author.split("<")[0].strip() or "Unknown"
                    email = (
                        author.split("<")[-1].rstrip(">").strip()
                        if "<" in author else ""
                    )
                    actor = Actor(name, email)
                    commit_kwargs["author"] = actor
                    commit_kwargs["committer"] = actor
                commit = repo.index.commit(full_message, **commit_kwargs)
        except Exception as e:
            msg = str(e).lower()
            if "nothing to commit" in msg or "no changes" in msg:
                raise GitClientError("no staged changes to commit") from e
            raise GitClientError(f"commit failed: {e}") from e
        return _to_commit(commit)

    def fetch(self, remote: str = "origin") -> str:
        """Fetch from the named remote. Returns the remote name."""
        repo = self._get_repo()
        try:
            repo.remotes[remote].fetch()
        except IndexError as e:
            raise GitNotFoundError(f"remote {remote!r} not configured") from e
        except Exception as e:
            raise GitClientError(f"fetch failed: {e}") from e
        return remote

    def pull(
        self, remote: Optional[str] = None, branch: Optional[str] = None, *,
        ff_only: bool = True,
    ) -> str:
        """Pull into the current branch.

        - Neither ``remote`` nor ``branch`` specified (default):
          ``git pull`` follows the configured upstream of the current
          branch. Correct default behavior.
        - Both specified: ``git pull <remote> <branch>``.
        - ``remote`` specified without ``branch``: ``git pull <remote>
          <current-branch>``. This matches what the caller intends
          (pull my branch from that remote), not ``git pull <remote>``
          alone (which would merge the remote's HEAD — almost never what's
          wanted).

        ``ff_only=True`` (default) refuses to merge or rebase — safest;
        matches ``git pull --ff-only``. Set False for merge/rebase flows.
        """
        repo = self._get_repo()
        try:
            args = []
            if ff_only:
                args.append("--ff-only")
            if remote and branch:
                args.extend([remote, branch])
            elif remote:
                # Explicit remote but no branch: use the current branch as
                # the remote ref, which is what the caller almost certainly
                # means. "git pull <remote>" alone would pull the remote's
                # HEAD regardless of our current branch.
                current = self.current_branch()
                if current == "HEAD":
                    raise GitClientError(
                        "cannot pull on detached HEAD without an explicit branch"
                    )
                args.extend([remote, current])
            # else: no remote specified — git pull follows configured upstream
            repo.git.pull(*args)
        except GitClientError:
            raise
        except Exception as e:
            msg = str(e).lower()
            if "not possible to fast-forward" in msg:
                raise GitUpstreamError(
                    "pull not fast-forward; branch has diverged from upstream"
                ) from e
            if "conflict" in msg:
                raise GitConflictError(f"pull produced conflicts: {e}") from e
            raise GitClientError(f"pull failed: {e}") from e
        return self.current_branch()

    def push(
        self, remote: str = "origin", branch: Optional[str] = None, *,
        force: bool = False, set_upstream: bool = False,
        allow_main: bool = False,
    ) -> dict[str, Any]:
        """Push to ``remote``.

        ``force=False`` refuses to rewrite upstream history. ``force=True``
        is logged at INFO with branch + SHA context so the audit trail
        captures the bypass.

        ``set_upstream=True`` is equivalent to ``git push -u``.

        ``allow_main=False`` (default) refuses to push when the resolved
        branch matches a name in ``PROTECTED_BRANCHES`` (``main``,
        ``master``). The ecosystem convention is branch → PR → review
        → merge; bypass is rare enough that callers should opt in
        explicitly when they need it.
        """
        repo = self._get_repo()
        current = self.current_branch()
        branch_to_push = branch or current

        # Resolve the destination side of the refspec — ``HEAD:main``
        # and ``main:main`` both push to main, and ``refs/heads/main``
        # is the same destination by another name. Without this the
        # guard would only catch a bare ``"main"`` arg.
        dst_branch = _resolve_push_dst_branch(branch_to_push)
        if dst_branch in PROTECTED_BRANCHES and not allow_main:
            raise GitGuardError(
                f"push refused: {branch_to_push!r} resolves to protected "
                f"branch {dst_branch!r}. Use a feature branch + PR, or "
                f"pass allow_main=True for the rare cases where direct-"
                f"to-{dst_branch} is intentional (release tags, etc.)."
            )

        try:
            args = []
            if force:
                args.append("--force")
                logger.info(
                    "push(force=True): %s/%s in %s", remote, branch_to_push, self.cwd,
                )
            if set_upstream:
                args.append("-u")
            args.append(remote)
            args.append(branch_to_push)
            repo.git.push(*args)
        except Exception as e:
            msg = str(e).lower()
            if "non-fast-forward" in msg or "rejected" in msg:
                raise GitUpstreamError(
                    f"push rejected (non-fast-forward); pull or rebase first"
                ) from e
            if "does not appear to be a git repository" in msg:
                raise GitNotFoundError(f"remote {remote!r} unreachable") from e
            raise GitClientError(f"push failed: {e}") from e
        return {
            "remote": remote,
            "branch": branch_to_push,
            "force": force,
            "set_upstream": set_upstream,
        }

    def pr_commit_push(
        self, branch: str, message: str, paths: list[str], *,
        remote: str = "origin",
        co_authors: Optional[list[str]] = None,
        set_upstream: bool = False,
    ) -> dict[str, Any]:
        """Stage explicit ``paths``, commit with ``message``, push.

        One-call composite that gates every step on ``branch`` matching
        the cwd's current branch — so a chained pipeline composed from
        the wrong cwd refuses fast instead of landing wrong-content
        commits with mismatched messages.

        Refuses if ``branch`` is in ``PROTECTED_BRANCHES`` (push would
        refuse anyway, but failing here is faster and avoids a stray
        commit on the local protected branch). ``paths`` must be
        explicit (no wildcards — same rules as ``stage``).

        Returns ``{"commit": GitCommit-as-dict, "push": push-result}``.
        """
        if not branch:
            raise GitGuardError("pr_commit_push requires an explicit branch")
        if branch in PROTECTED_BRANCHES:
            raise GitGuardError(
                f"pr_commit_push refused: {branch!r} is protected. "
                f"Use a feature branch."
            )
        current = self.current_branch()
        if current != branch:
            raise GitGuardError(
                f"branch mismatch: pr_commit_push expects {branch!r}, "
                f"cwd is on {current!r}. Switch branches or correct "
                f"the call so the commit lands where the message says."
            )
        self.stage(paths)
        commit = self.commit(
            message,
            co_authors=co_authors,
            branch_hint=branch,
        )
        push = self.push(
            remote=remote,
            branch=branch,
            set_upstream=set_upstream,
        )
        return {
            "commit": {
                "sha": commit.sha,
                "short_sha": commit.short_sha,
                "message": commit.message,
            },
            "push": push,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_commit(commit: Any) -> GitCommit:
    """Convert a git.Commit into our normalized :class:`GitCommit`."""
    message = commit.message
    subject = message.split("\n", 1)[0]
    return GitCommit(
        sha=commit.hexsha,
        short_sha=commit.hexsha[:8],
        author=f"{commit.author.name} <{commit.author.email}>",
        committed_at=commit.committed_datetime.isoformat(),
        message=subject,
        full_message=message,
    )
