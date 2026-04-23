"""Long-running PR fleet watcher.

Polls GitHub for PR state across a set of repos/PRs and publishes
``pr.*`` bus events on transitions. Subscribers consume these via
``bus_wait_for_event`` instead of each session re-polling GitHub
directly — one canonical watcher replaces N per-session pollers.

Architecture:

- :class:`PRWatcherStore` owns two SQLite tables in developer.db:

  - ``pr_watcher_registry`` — one row per live watcher (id, repos,
    explicit PR list, interval, started_at, last_poll_at).
  - ``pr_watcher_dedupe`` — one row per emitted transition, keyed by
    ``(watcher_id, repo, pr_number, transition_kind, dedupe_id)``.
    Ensures a given transition fires at most once per watcher lifetime,
    across agent restarts.

- :class:`PRFleetWatcher` is the per-watcher async task. On each tick
  it polls the configured PR set, diffs current state against prior
  state, and calls the injected ``publish`` callable for each new
  transition. ``publish`` is normally ``BaseAgent.publish`` wired by
  the agent; unit tests inject a list-appending fake.

- :class:`PRWatcherRegistry` is the process-level coordinator — tracks
  live tasks, starts/stops them, serializes list queries, and owns the
  shared :class:`PRWatcherStore`. The agent keeps a single registry
  instance across its lifetime.

Failure mode is contained: a single failing PR poll emits
``pr.poll_error`` and the loop keeps going. Only a watcher explicitly
stopped (or a fatal infrastructure failure) ends the task.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable, Optional

from developer.github_client import GithubClient, GithubClientError, is_copilot_login

logger = logging.getLogger(__name__)


# Publish signature: ``async def publish(topic: str, payload: dict) -> None``.
# Matches BaseAgent.publish; tests substitute a list-append fake.
PublishFn = Callable[[str, dict], Awaitable[None]]


# Bus topics this watcher publishes. Centralized as constants so
# subscribers (and tests) can reference them without string-typos.
TOPIC_REVIEW_LANDED = "pr.review_landed"
TOPIC_NEW_COMMIT = "pr.new_commit"
TOPIC_MERGED = "pr.merged"
TOPIC_MERGE_READY = "pr.merge_ready"
TOPIC_CLOSED_WITHOUT_MERGE = "pr.closed_without_merge"
TOPIC_POLL_ERROR = "pr.poll_error"
# Added by fr_developer_e2bdd869: comment-channel coverage.
# ``pr.comment_posted`` covers ``/issues/N/comments`` (top-level PR
# discussion — where Copilot's re-review verdicts land as bare
# comments instead of formal reviews).
# ``pr.inline_finding`` covers ``/pulls/N/comments`` (inline review-
# thread comments — where Copilot's per-finding comments live, anchored
# to a ``(path, line)``).
TOPIC_COMMENT_POSTED = "pr.comment_posted"
TOPIC_INLINE_FINDING = "pr.inline_finding"
# Added by fr_developer_fafb36f1: per-poll-cycle aggregate digest. Emits
# only when at least one granular transition fired in the cycle — silent
# windows stay silent. Companion to (not replacement for) the granular
# ``pr.*`` topics above; subscribers that already react per-transition
# keep their existing subscriptions unchanged.
TOPIC_FLEET_DIGEST = "pr.fleet_digest"

ALL_PR_TOPICS = (
    TOPIC_REVIEW_LANDED,
    TOPIC_NEW_COMMIT,
    TOPIC_MERGED,
    TOPIC_MERGE_READY,
    TOPIC_CLOSED_WITHOUT_MERGE,
    TOPIC_POLL_ERROR,
    TOPIC_COMMENT_POSTED,
    TOPIC_INLINE_FINDING,
    TOPIC_FLEET_DIGEST,
)


# Truncation cap on comment body previews attached to snapshot fields.
# Events emit the full body; the preview is for the compact snapshot
# view used by ``pr_fleet_status`` / fleet-digest consumers where 500
# chars is a reasonable first-glance tradeoff. Also reused by
# ``pr.fleet_digest`` event bodies (fr_developer_fafb36f1) so a
# subscriber that takes only the digest never has to do a follow-up API
# call just to learn a finding body.
COMMENT_PREVIEW_CHARS = 500


def _truncate_body(body: str) -> str:
    """Cap a comment / review body at :data:`COMMENT_PREVIEW_CHARS`.

    Used by the ``pr.fleet_digest`` payload builder so the aggregate
    event stays bounded regardless of upstream body length. Returns the
    input unchanged if it's under the cap; never raises.
    """
    if not body:
        return ""
    return body[:COMMENT_PREVIEW_CHARS]


def _repo_short(repo: str) -> str:
    """Return the ``name`` half of an ``owner/name`` repo string.

    ``pr.fleet_digest`` + ``pr_fleet_status`` use a ``<repo-short>#<num>``
    key for each fleet entry. Owners are redundant when every watcher in
    a single khonliang fleet points at the same owner, and the compact
    form reads noticeably better in log grep. A repo string without a
    slash falls through unchanged — defensive rather than raising because
    test fixtures sometimes use bare ``repo`` names.
    """
    if not repo:
        return ""
    return repo.rsplit("/", 1)[-1]


def _now_iso() -> str:
    """UTC ISO-8601 timestamp with ``Z`` suffix.

    Used for the ``t`` field on ``pr.fleet_digest`` payloads and on
    ``pr_fleet_status`` snapshot responses. Local-naive ``datetime.now``
    would serialize without a timezone marker; the explicit Z keeps
    cross-subscriber parsing unambiguous.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fleet_item_from_snapshot(snapshot: "PRSnapshot") -> dict:
    """Project a :class:`PRSnapshot` into the compact fleet-item shape.

    Shared by ``developer.pr_fleet_status`` and the ``pr.fleet_digest``
    event's per-entry ``state`` field so both views stay identical —
    consumers that hop between the one-shot snapshot and the per-cycle
    digest never see shape drift.
    """
    reviews = snapshot.reviews or []
    latest_review = reviews[-1] if reviews else {}
    issue_comments = snapshot.external_issue_comments or []
    latest_issue = issue_comments[-1] if issue_comments else {}
    inline = snapshot.inline_findings or []
    latest_inline = inline[-1] if inline else {}
    mergeable: Optional[str]
    if snapshot.mergeable is None:
        mergeable = None
    else:
        mergeable = "mergeable" if snapshot.mergeable else "blocked"
    return {
        "k": f"{_repo_short(snapshot.repo)}#{snapshot.pr_number}",
        "reviews": {
            "count": len(reviews),
            "latest_state": str(latest_review.get("state", "")) if latest_review else "",
            "latest_by": str(latest_review.get("reviewer", "")) if latest_review else "",
            "latest_at": str(latest_review.get("submitted_at", "")) if latest_review else "",
        },
        "external_issue_comments": {
            "count": len(issue_comments),
            "latest_by": str(latest_issue.get("author", "")) if latest_issue else "",
            "latest_at": str(latest_issue.get("posted_at", "")) if latest_issue else "",
        },
        "inline_findings": {
            "count": len(inline),
            "latest_by": str(latest_inline.get("author", "")) if latest_inline else "",
            "latest_at": str(latest_inline.get("posted_at", "")) if latest_inline else "",
            "latest_path": str(latest_inline.get("path") or "") if latest_inline else "",
            "latest_line": int(latest_inline.get("line") or 0) if latest_inline else 0,
        },
        "merged": bool(snapshot.merged),
        "mergeable": mergeable,
        "head_sha": str(snapshot.head_sha or ""),
        "state": str(snapshot.state or ""),
    }


def comment_looks_like_bot_verdict(body: str, author: str = "") -> bool:
    """Heuristic: does this issue comment look like Copilot's re-review
    verdict posted as a bare comment?

    Copilot's re-review verdicts land as ``/issues/N/comments`` entries
    (not formal reviews) and quote back the triggering ``@copilot please
    re-review`` mention before delivering the verdict. Detection requires
    both signals: a quoted ``@copilot please re-review`` on the first
    non-empty line AND a Copilot-shaped author login. Content-only
    detection is too eager (a human could quote the same phrase) and
    author-only detection is too loose (non-verdict Copilot comments
    would also match).

    Subscribers use this as a hint — the event still fires for any
    external comment; this just lets consumers prioritize bot-verdict
    comments differently when they care to.
    """
    if not body:
        return False
    # Share the Copilot-identity set with github_client rather than
    # duplicating it here: a new auth variant (e.g. ``copilot-swe-agent``)
    # only has to land once. Historically this module kept its own
    # narrower set (``copilot`` / ``copilot-pull-request-reviewer`` only)
    # that drifted out of sync with github_client's broader set; PR #39
    # Copilot R3 flagged the duplication and consolidated on
    # :func:`is_copilot_login`, which now carries the union of what both
    # sites used to accept (plus the ``-swe-agent`` variants).
    #
    # ``is_copilot_login`` treats an empty string as not-copilot (the
    # set doesn't contain ``""``), so the empty-author case naturally
    # returns False without a separate guard.
    if not is_copilot_login(author or ""):
        return False
    # First non-empty line of the body should start with ">" (a quote)
    # and mention @copilot please re-review.
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(">") and "@copilot please re-review" in stripped.lower():
            return True
        return False
    return False


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


_SCHEMA = """
CREATE TABLE IF NOT EXISTS pr_watcher_registry (
    watcher_id TEXT PRIMARY KEY,
    repos TEXT NOT NULL,
    pr_numbers TEXT NOT NULL,
    interval_s INTEGER NOT NULL,
    started_at REAL NOT NULL,
    last_poll_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS pr_watcher_dedupe (
    watcher_id TEXT NOT NULL,
    repo TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    transition_kind TEXT NOT NULL,
    dedupe_id TEXT NOT NULL,
    emitted_at REAL NOT NULL,
    PRIMARY KEY (watcher_id, repo, pr_number, transition_kind, dedupe_id)
);
"""


class PRWatcherStore:
    """SQLite-backed persistence for watcher registry + dedupe state.

    Uses the same ``developer.db`` file as the rest of developer's
    stores (via pipeline config), but keeps its tables isolated from
    the KnowledgeStore / TripleStore schemas. A separate connection per
    method call matches the KnowledgeStore pattern — cheap enough for
    per-poll writes and avoids cross-thread connection issues.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    # -- registry --

    def register_watcher(
        self,
        watcher_id: str,
        repos: list[str],
        pr_numbers: list[int],
        interval_s: int,
        started_at: float,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO pr_watcher_registry
                    (watcher_id, repos, pr_numbers, interval_s, started_at, last_poll_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    watcher_id,
                    ",".join(repos),
                    ",".join(str(n) for n in pr_numbers),
                    int(interval_s),
                    float(started_at),
                    0.0,
                ),
            )

    def touch_last_poll(self, watcher_id: str, at: float) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE pr_watcher_registry SET last_poll_at = ? WHERE watcher_id = ?",
                (float(at), watcher_id),
            )

    def remove_watcher(self, watcher_id: str) -> None:
        """Remove a watcher + all its dedupe entries.

        Called on ``stop_pr_watcher``. Keeps the table from growing
        unboundedly when callers cycle watchers.
        """
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM pr_watcher_registry WHERE watcher_id = ?",
                (watcher_id,),
            )
            conn.execute(
                "DELETE FROM pr_watcher_dedupe WHERE watcher_id = ?",
                (watcher_id,),
            )

    def list_watchers(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM pr_watcher_registry ORDER BY started_at ASC"
            ).fetchall()
        return [
            {
                "watcher_id": r["watcher_id"],
                "repos": _split_csv(r["repos"]),
                "pr_numbers": [int(n) for n in _split_csv(r["pr_numbers"]) if n],
                "interval_s": int(r["interval_s"]),
                "started_at": float(r["started_at"]),
                "last_poll_at": float(r["last_poll_at"]),
            }
            for r in rows
        ]

    # -- dedupe --

    def has_emitted(
        self,
        watcher_id: str,
        repo: str,
        pr_number: int,
        transition_kind: str,
        dedupe_id: str,
    ) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM pr_watcher_dedupe
                WHERE watcher_id = ? AND repo = ? AND pr_number = ?
                  AND transition_kind = ? AND dedupe_id = ?
                LIMIT 1
                """,
                (watcher_id, repo, int(pr_number), transition_kind, str(dedupe_id)),
            ).fetchone()
        return row is not None

    def mark_emitted(
        self,
        watcher_id: str,
        repo: str,
        pr_number: int,
        transition_kind: str,
        dedupe_id: str,
        at: float,
    ) -> bool:
        """Record a dedupe key; returns True if this was a new entry.

        Uses ``INSERT OR IGNORE`` so concurrent watchers (unlikely given
        the registry, but cheap insurance) or race-induced double-calls
        can't double-emit. Returns False when the row already existed.
        """
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO pr_watcher_dedupe
                    (watcher_id, repo, pr_number, transition_kind, dedupe_id, emitted_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    watcher_id,
                    repo,
                    int(pr_number),
                    transition_kind,
                    str(dedupe_id),
                    float(at),
                ),
            )
            return cur.rowcount > 0


def _split_csv(value: str) -> list[str]:
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


# ---------------------------------------------------------------------------
# Transition detection
# ---------------------------------------------------------------------------


def _review_is_from_bot(review: dict) -> bool:
    """Detect whether a review row originates from a bot reviewer.

    Two signals, either sufficient: GitHub's user ``type`` equals
    ``"Bot"``, or the login ends in ``"[bot]"`` (matches both the raw
    GH representation and the resolved format seen in some surfaces).
    The reviewer metadata may be stored either as a nested ``user``
    sub-dict (raw GH shape) or as a flat ``reviewer`` string (our
    snapshot normalization in :func:`_snapshot_from_github`); we
    check both so ``merge_ready`` stays robust to either shape.
    """
    user = review.get("user")
    if isinstance(user, dict):
        if str(user.get("type", "")).upper() == "BOT":
            return True
        if str(user.get("login", "")).lower().endswith("[bot]"):
            return True
    reviewer = str(review.get("reviewer", "")).lower()
    if reviewer.endswith("[bot]"):
        return True
    return False


@dataclass
class PRSnapshot:
    """Bounded view of a PR's state used for transition detection.

    ``external_issue_comments`` and ``inline_findings`` (added by
    ``fr_developer_e2bdd869``) carry the filtered-non-self entries from
    ``/issues/N/comments`` and ``/pulls/N/comments`` respectively. Each
    entry is a dict shaped as expected by the emit logic — the snapshot
    itself is the source of truth; the derived summary fields
    (``*_count`` / ``latest_*``) are convenience views for callers
    (notably ``pr_fleet_status``) that want a compact digest without
    iterating the whole list.
    """

    repo: str
    pr_number: int
    head_sha: str
    state: str = "open"           # open | closed
    merged: bool = False
    merged_at: str = ""
    mergeable: Optional[bool] = None
    merge_state: str = ""          # clean | blocked | dirty | ...
    reviews: list[dict] = field(default_factory=list)
    review_decision: str = ""      # APPROVED | CHANGES_REQUESTED | REVIEW_REQUIRED | ""
    # Comment-channel fields (fr_developer_e2bdd869).
    external_issue_comments: list[dict] = field(default_factory=list)
    inline_findings: list[dict] = field(default_factory=list)
    # Derived summary fields, populated by :func:`_populate_comment_summaries`
    # after the lists are assembled. Kept on the snapshot (not computed
    # on read) so consumers that serialize it don't have to rebuild the
    # view every access.
    external_issue_comments_count: int = 0
    latest_issue_comment_at: Optional[str] = None
    latest_issue_comment_by: Optional[str] = None
    latest_issue_comment_preview: str = ""
    inline_findings_count: int = 0
    latest_inline_at: Optional[str] = None
    latest_inline_by: Optional[str] = None
    latest_inline_path: Optional[str] = None
    latest_inline_line: Optional[int] = None
    latest_inline_preview: str = ""

    def merge_ready(self) -> bool:
        """Strictly conservative merge-ready check.

        Requires mergeable == True, a clean-ish merge state, and no
        outstanding CHANGES_REQUESTED reviews from non-bot reviewers.
        Intentionally doesn't try to re-derive GitHub's
        ``review_decision`` — we only emit ``merge_ready`` once the
        simple signals all line up.

        Bot CHANGES_REQUESTED (e.g. a Copilot re-review block) is
        filtered out here because GitHub-style review gates treat bot
        reviews as advisory; a human approval with a bot block still
        represents a merge-ready state under the FR's intended
        semantics. Bot identity is detected by the review's ``user``
        sub-dict: ``type == "Bot"`` OR a login ending in ``[bot]``.
        """
        if self.merged or self.state != "open":
            return False
        if self.mergeable is not True:
            return False
        if self.merge_state not in ("clean", "unstable", "has_hooks"):
            return False
        blocking = [
            r for r in self.reviews
            if str(r.get("state", "")).upper() == "CHANGES_REQUESTED"
            and not _review_is_from_bot(r)
        ]
        return not blocking


# ---------------------------------------------------------------------------
# Watcher
# ---------------------------------------------------------------------------


# Abstraction over GithubClient so unit tests can swap in a fake
# without monkey-patching the module-level async calls.
FetchSnapshotFn = Callable[[str, int], Awaitable[PRSnapshot]]
ListOpenPRsFn = Callable[[str], Awaitable[list[int]]]


@dataclass
class PRWatcherConfig:
    """Resolved inputs for a single watcher."""

    watcher_id: str
    repos: list[str]
    pr_numbers: list[int]          # explicit list (possibly empty)
    interval_s: int
    started_at: float

    def public_dict(self) -> dict:
        return {
            "id": self.watcher_id,
            "repos": list(self.repos),
            "pr_numbers": list(self.pr_numbers),
            "interval_s": self.interval_s,
            "started_at": self.started_at,
        }


class PRFleetWatcher:
    """One long-running watcher instance.

    Composes the persistence layer, the GH fetch surface, and the
    bus publish callback. Owns the poll loop but not the task lifetime —
    :class:`PRWatcherRegistry` wraps this in an asyncio Task.
    """

    def __init__(
        self,
        config: PRWatcherConfig,
        store: PRWatcherStore,
        publish: PublishFn,
        fetch_snapshot: FetchSnapshotFn,
        list_open_prs: ListOpenPRsFn,
        now_fn: Callable[[], float] = time.time,
    ):
        self.config = config
        self.store = store
        self._publish = publish
        self._fetch_snapshot = fetch_snapshot
        self._list_open_prs = list_open_prs
        self._now = now_fn
        self._pr_count: int = 0
        # In-memory per-(repo, pr_number) caches of already-observed
        # comment ids, one per channel. Used to skip the ``_emit`` call
        # (and its ``INSERT OR IGNORE`` into ``pr_watcher_dedupe``) when
        # the snapshot hasn't changed between polls. Long PR threads
        # would otherwise churn the dedupe table on every tick even
        # though nothing new has arrived.
        #
        # Cross-restart correctness still comes from the SQLite dedupe
        # table: on a fresh process these caches are empty, so the first
        # poll re-observes every id via ``_emit`` and ``mark_emitted``
        # returns False for rows that were persisted before the restart
        # (no re-emission). After that first pass the cache is primed
        # and subsequent unchanged polls touch zero rows.
        self._seen_issue_comment_ids: dict[tuple[str, int], set[str]] = {}
        self._seen_inline_finding_ids: dict[tuple[str, int], set[str]] = {}
        # Latest-observed snapshot per PR, keyed the same way. Populated
        # on every successful ``_fetch_snapshot`` so that
        # :meth:`PRWatcherRegistry.fleet_snapshot` (and through it the
        # ``developer.pr_fleet_status`` skill) can project the compact
        # shape without re-polling GitHub. Added by
        # ``fr_developer_fafb36f1``. Pruned together with the seen-id
        # caches so closed/merged PRs don't linger in memory.
        self._latest_snapshots: dict[tuple[str, int], PRSnapshot] = {}
        # Per-cycle transition buffer for :data:`TOPIC_FLEET_DIGEST`.
        # Reset at the top of each :meth:`poll_once`. Keys are the same
        # ``(repo, pr_number)`` tuples as the caches above; values are
        # ordered lists of digest event dicts ready to drop into the
        # payload's ``events`` array.
        self._cycle_transitions: dict[tuple[str, int], list[dict]] = {}

    @property
    def pr_count(self) -> int:
        """Number of PRs observed on the most recent poll."""
        return self._pr_count

    def _prune_seen_caches(self, active_keys: set[tuple[str, int]]) -> None:
        """Drop in-memory seen-id entries for PRs no longer in the active set.

        Called once per poll after ``_resolve_pr_set``. In "watch all
        open PRs" mode (no explicit ``pr_numbers``), closed/merged PRs
        stop appearing in the resolved set — their cache entries would
        otherwise accumulate forever. Pinning to ``active_keys`` keeps
        the cache bounded by fleet size.
        """
        for cache in (
            self._seen_issue_comment_ids,
            self._seen_inline_finding_ids,
            self._latest_snapshots,
        ):
            stale = [key for key in cache if key not in active_keys]
            for key in stale:
                cache.pop(key, None)

    async def _resolve_pr_set(self) -> list[tuple[str, int]]:
        """Return ``(repo, pr_number)`` pairs to poll this cycle.

        Explicit ``pr_numbers`` win when provided. They apply to every
        listed repo (mirroring the caller's existing model of
        "these PR numbers in these repos"). If no explicit numbers are
        configured, we enumerate open PRs per repo via the injected
        ``list_open_prs`` callable.
        """
        pairs: list[tuple[str, int]] = []
        if self.config.pr_numbers:
            for repo in self.config.repos:
                for pr_number in self.config.pr_numbers:
                    pairs.append((repo, pr_number))
            return pairs
        for repo in self.config.repos:
            try:
                open_numbers = await self._list_open_prs(repo)
            except asyncio.CancelledError:
                # CancelledError subclasses Exception (3.8+); never
                # catch-and-continue past cooperative cancellation.
                raise
            except Exception as e:
                await self._emit_poll_error(repo, pr_number=0, reason=str(e))
                continue
            for n in open_numbers:
                pairs.append((repo, n))
        return pairs

    async def poll_once(self) -> int:
        """Run a single poll cycle and return count of transitions emitted.

        Called from the async loop; also invoked directly by tests to
        step the watcher without sleeping.
        """
        pairs = await self._resolve_pr_set()
        self._pr_count = len(pairs)
        # Prune in-memory seen caches for PRs that have dropped out of
        # the active set (closed / merged PRs in "watch all open PRs"
        # mode no longer show up in ``_resolve_pr_set``). Without this
        # the caches grow monotonically over the watcher's lifetime.
        # Cross-restart dedupe remains the SQLite table's job; this
        # only reclaims process memory.
        active_keys = {(repo, pr_number) for repo, pr_number in pairs}
        self._prune_seen_caches(active_keys)
        # Reset the per-cycle digest buffer. Anything still hanging off
        # a previous cycle is stale; ``pr.fleet_digest`` fires at most
        # once per cycle and only when this buffer picks up entries
        # from the emit path below.
        self._cycle_transitions = {}
        emitted = 0
        for repo, pr_number in pairs:
            try:
                snapshot = await self._fetch_snapshot(repo, pr_number)
            except asyncio.CancelledError:
                # CancelledError subclasses Exception; without this the
                # per-PR "keep the watcher alive" fallback below would
                # swallow cooperative shutdown mid-poll.
                raise
            except GithubClientError as e:
                await self._emit_poll_error(repo, pr_number, reason=str(e))
                continue
            except Exception as e:
                # Keep the watcher alive through unexpected non-client
                # errors too (e.g. transient network glitches that
                # bubble up as raw httpx exceptions). Log and move on.
                logger.warning(
                    "PR watcher %s: unexpected error polling %s#%d: %s",
                    self.config.watcher_id, repo, pr_number, e,
                )
                await self._emit_poll_error(repo, pr_number, reason=str(e))
                continue
            # Cache the latest observed snapshot for ``pr_fleet_status``
            # consumers. Stored before transition detection so even a
            # cycle that produces zero new transitions still surfaces the
            # current PR state through the snapshot skill.
            self._latest_snapshots[(snapshot.repo, snapshot.pr_number)] = snapshot
            emitted += await self._emit_transitions(snapshot)
        self.store.touch_last_poll(self.config.watcher_id, self._now())
        # Emit the aggregate digest only when at least one transition
        # fired this cycle. Silent windows cost zero digest events —
        # subscribers that care about "something happened" don't have
        # to filter a heartbeat stream.
        if self._cycle_transitions:
            await self._emit_fleet_digest()
        return emitted

    def latest_snapshots(self) -> list[PRSnapshot]:
        """Return the list of most-recent snapshots, one per active PR.

        ``developer.pr_fleet_status`` uses this to project the compact
        fleet-item shape without re-polling GitHub. Order matches
        insertion order (roughly the resolve-order from
        :meth:`_resolve_pr_set`); consumers that need a specific order
        should sort on the fleet-item ``k`` field.
        """
        return list(self._latest_snapshots.values())

    async def run(self, stop_event: asyncio.Event) -> None:
        """Loop until ``stop_event`` is set.

        The wait-with-timeout pattern lets stop() interrupt an in-flight
        sleep without waiting for a full ``interval_s``.
        """
        while not stop_event.is_set():
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Catastrophic: poll_once itself shouldn't raise because
                # it handles per-PR failures, but defense in depth.
                logger.error(
                    "PR watcher %s: poll_once failed: %s",
                    self.config.watcher_id, e,
                )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.config.interval_s)
            except asyncio.TimeoutError:
                pass

    # -- emit helpers --

    async def _emit_transitions(self, snapshot: PRSnapshot) -> int:
        """Diff snapshot against stored dedupe state and publish new events."""
        emitted = 0
        # Where this PR's per-cycle digest events accumulate. Appended
        # to alongside every granular ``_emit`` that actually publishes;
        # the digest layer reads this at the end of ``poll_once`` and
        # never mutates it. Intentionally tied to the buffer dict's
        # lifetime (reset on every poll cycle) so a prior cycle's events
        # can't leak forward.
        pr_key = (snapshot.repo, snapshot.pr_number)

        def _record_digest(event: dict) -> None:
            self._cycle_transitions.setdefault(pr_key, []).append(event)

        # 1. New commits. dedupe_id = head_sha.
        # NOTE: we intentionally don't include a ``pushed_at`` timestamp.
        # :meth:`GithubClient.get_pr` doesn't currently surface a commit
        # push time (would need a follow-up call to the commits endpoint),
        # and an always-empty field would invite subscribers to build on
        # a guarantee we can't keep. If a consumer ever needs the push
        # time, extend GithubClient + :class:`PRSnapshot` together and
        # re-introduce the field with a real value.
        if snapshot.head_sha:
            if await self._emit(
                TOPIC_NEW_COMMIT,
                repo=snapshot.repo,
                pr_number=snapshot.pr_number,
                transition_kind="new_commit",
                dedupe_id=snapshot.head_sha,
                payload={
                    "repo": snapshot.repo,
                    "pr_number": snapshot.pr_number,
                    "head_sha": snapshot.head_sha,
                },
            ):
                emitted += 1
                _record_digest({"kind": "new_commit", "sha": snapshot.head_sha[:12]})

        # 2. Reviews landed. dedupe_id = review_id.
        for review in snapshot.reviews:
            review_id = review.get("id")
            if review_id is None:
                continue
            if await self._emit(
                TOPIC_REVIEW_LANDED,
                repo=snapshot.repo,
                pr_number=snapshot.pr_number,
                transition_kind="review_landed",
                dedupe_id=str(review_id),
                payload={
                    "repo": snapshot.repo,
                    "pr_number": snapshot.pr_number,
                    "reviewer": review.get("reviewer", ""),
                    "state": review.get("state", ""),
                    "submitted_at": review.get("submitted_at", ""),
                    "review_id": review_id,
                },
            ):
                emitted += 1
                _record_digest({
                    "kind": "review",
                    "state": str(review.get("state", "")),
                    "by": str(review.get("reviewer", "")),
                    "body": _truncate_body(str(review.get("body", "") or "")),
                })

        # 2a. Issue comments (fr_developer_e2bdd869).
        # Each external (non-self) comment fires exactly one
        # pr.comment_posted, deduped by comment_id.
        #
        # Long PR threads keep accumulating comments; without in-memory
        # diffing against the prior snapshot we'd call ``_emit`` (and
        # its ``INSERT OR IGNORE`` into ``pr_watcher_dedupe``) for every
        # stored comment on every poll. Instead we track seen ids per
        # (repo, pr_number) and only hit ``_emit`` for the delta.
        key = (snapshot.repo, snapshot.pr_number)
        seen_issue = self._seen_issue_comment_ids.setdefault(key, set())
        for comment in snapshot.external_issue_comments:
            comment_id = comment.get("id")
            if comment_id is None:
                continue
            id_str = str(comment_id)
            if id_str in seen_issue:
                continue
            if await self._emit(
                TOPIC_COMMENT_POSTED,
                repo=snapshot.repo,
                pr_number=snapshot.pr_number,
                transition_kind="comment_posted",
                dedupe_id=id_str,
                payload={
                    "repo": snapshot.repo,
                    "pr_number": snapshot.pr_number,
                    "comment_id": comment_id,
                    "author": comment.get("author", ""),
                    "posted_at": comment.get("posted_at", ""),
                    "body": comment.get("body", ""),
                },
            ):
                emitted += 1
                _record_digest({
                    "kind": "comment",
                    "by": str(comment.get("author", "")),
                    "body": _truncate_body(str(comment.get("body", "") or "")),
                })
            # Mark seen regardless of whether ``_emit`` actually
            # published — if the SQLite dedupe table already had the
            # row (cross-restart case), ``_emit`` returns False, but
            # we still don't want to revisit it next poll.
            seen_issue.add(id_str)

        # 2b. Inline review-thread comments (fr_developer_e2bdd869).
        # Each external (non-self) inline finding fires exactly one
        # pr.inline_finding, deduped by comment_id. review_id correlates
        # with the containing pr.review_landed when the comment is part
        # of a formal review; None when it's a standalone thread reply.
        seen_inline = self._seen_inline_finding_ids.setdefault(key, set())
        for finding in snapshot.inline_findings:
            comment_id = finding.get("id")
            if comment_id is None:
                continue
            id_str = str(comment_id)
            if id_str in seen_inline:
                continue
            if await self._emit(
                TOPIC_INLINE_FINDING,
                repo=snapshot.repo,
                pr_number=snapshot.pr_number,
                transition_kind="inline_finding",
                dedupe_id=id_str,
                payload={
                    "repo": snapshot.repo,
                    "pr_number": snapshot.pr_number,
                    "comment_id": comment_id,
                    "author": finding.get("author", ""),
                    "posted_at": finding.get("posted_at", ""),
                    "path": finding.get("path"),
                    "line": finding.get("line"),
                    "body": finding.get("body", ""),
                    "review_id": finding.get("review_id"),
                },
            ):
                emitted += 1
                line_raw = finding.get("line")
                _record_digest({
                    "kind": "inline_finding",
                    "by": str(finding.get("author", "")),
                    "path": str(finding.get("path") or ""),
                    "line": int(line_raw) if line_raw is not None else 0,
                    "body": _truncate_body(str(finding.get("body", "") or "")),
                    "review_id": finding.get("review_id"),
                })
            seen_inline.add(id_str)

        # 3. Merged. dedupe_id = merged_at timestamp (stable once set).
        if snapshot.merged and snapshot.merged_at:
            if await self._emit(
                TOPIC_MERGED,
                repo=snapshot.repo,
                pr_number=snapshot.pr_number,
                transition_kind="merged",
                dedupe_id=snapshot.merged_at,
                payload={
                    "repo": snapshot.repo,
                    "pr_number": snapshot.pr_number,
                    "merged_at": snapshot.merged_at,
                },
            ):
                emitted += 1
                _record_digest({"kind": "merged"})
            # A merged PR is also terminal; don't emit merge_ready after
            # the fact and don't emit closed_without_merge.
            return emitted

        # 4. Closed without merge. dedupe_id = literal "closed" (fires once).
        if snapshot.state == "closed" and not snapshot.merged:
            if await self._emit(
                TOPIC_CLOSED_WITHOUT_MERGE,
                repo=snapshot.repo,
                pr_number=snapshot.pr_number,
                transition_kind="closed_without_merge",
                dedupe_id="closed",
                payload={
                    "repo": snapshot.repo,
                    "pr_number": snapshot.pr_number,
                },
            ):
                emitted += 1
                _record_digest({
                    "kind": "state_change", "from": "open", "to": "closed",
                })
            return emitted

        # 5. Merge-ready. dedupe_id = head_sha so a fresh commit can
        #    retrigger the merge-ready signal after new review.
        if snapshot.merge_ready() and snapshot.head_sha:
            if await self._emit(
                TOPIC_MERGE_READY,
                repo=snapshot.repo,
                pr_number=snapshot.pr_number,
                transition_kind="merge_ready",
                dedupe_id=snapshot.head_sha,
                payload={
                    "repo": snapshot.repo,
                    "pr_number": snapshot.pr_number,
                },
            ):
                emitted += 1

        return emitted

    async def _emit(
        self,
        topic: str,
        *,
        repo: str,
        pr_number: int,
        transition_kind: str,
        dedupe_id: str,
        payload: dict,
    ) -> bool:
        """Persist dedupe key then publish; skip if already emitted.

        The persist-before-publish ordering means a publish failure still
        leaves a dedupe record (we won't retry the same key). That's
        preferable to the alternative — re-emitting on every restart
        until publish succeeds would flood subscribers with duplicates.
        Operators see the publish failure in logs.
        """
        is_new = self.store.mark_emitted(
            watcher_id=self.config.watcher_id,
            repo=repo,
            pr_number=pr_number,
            transition_kind=transition_kind,
            dedupe_id=dedupe_id,
            at=self._now(),
        )
        if not is_new:
            return False
        try:
            await self._publish(topic, payload)
        except asyncio.CancelledError:
            # CancelledError subclasses Exception; don't log-and-swallow
            # cooperative cancellation during publish.
            raise
        except Exception as e:
            logger.warning(
                "PR watcher %s: publish %s failed: %s",
                self.config.watcher_id, topic, e,
            )
        return True

    async def _emit_fleet_digest(self) -> None:
        """Publish the per-cycle :data:`TOPIC_FLEET_DIGEST` event.

        Called once at the end of :meth:`poll_once` when the cycle
        transition buffer is non-empty. The payload aggregates every
        PR whose granular events fired this cycle into a single
        ``changed`` list — subscribers that only care "did anything
        happen across the fleet?" get a single event per cycle instead
        of N per-transition events to filter.

        Intentionally additive to the granular ``pr.*`` topics — those
        fire first (the digest event collects them as a side effect of
        their emit path). A subscriber may legitimately take either
        surface; the digest's per-entry ``state`` projection is the
        same compact shape ``pr_fleet_status`` returns so consumers
        never see shape drift between the two views.
        """
        changed: list[dict] = []
        for (repo, pr_number), events in self._cycle_transitions.items():
            snapshot = self._latest_snapshots.get((repo, pr_number))
            if snapshot is None:
                # Transition buffered without a corresponding snapshot
                # cached — shouldn't happen on the normal path
                # (``poll_once`` caches the snapshot before
                # ``_emit_transitions`` can append), but skip defensively
                # rather than emitting an entry with no state.
                continue
            changed.append({
                "k": f"{_repo_short(repo)}#{pr_number}",
                "events": list(events),
                "state": _fleet_item_from_snapshot(snapshot),
            })
        if not changed:
            return
        payload = {
            "t": _now_iso(),
            "watcher_id": self.config.watcher_id,
            "changed": changed,
        }
        try:
            await self._publish(TOPIC_FLEET_DIGEST, payload)
        except asyncio.CancelledError:
            # CancelledError subclasses Exception; don't log-and-swallow
            # cooperative cancellation during publish.
            raise
        except Exception as e:
            logger.warning(
                "PR watcher %s: publish %s failed: %s",
                self.config.watcher_id, TOPIC_FLEET_DIGEST, e,
            )

    async def _emit_poll_error(self, repo: str, pr_number: int, reason: str) -> None:
        """Low-severity error signal — never deduped (reset per poll).

        Intentionally not persisted: poll errors are per-tick and
        callers typically only care about the current failure mode.
        Deduping across a restart would mask persistent breakage.
        """
        try:
            await self._publish(
                TOPIC_POLL_ERROR,
                {
                    "repo": repo,
                    "pr_number": pr_number,
                    "watcher_id": self.config.watcher_id,
                    "reason": reason,
                    "at": self._now(),
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(
                "PR watcher %s: publish pr.poll_error failed: %s",
                self.config.watcher_id, e,
            )


# ---------------------------------------------------------------------------
# Registry (process-level)
# ---------------------------------------------------------------------------


@dataclass
class _LiveWatcher:
    """In-memory handle wrapping the watcher + its stop event + task."""

    watcher: PRFleetWatcher
    stop_event: asyncio.Event
    task: asyncio.Task


class PRWatcherRegistry:
    """Owns the set of active watchers on a single agent process.

    The agent constructs one of these at first use (lazily) and reuses
    it across ``watch_pr_fleet`` / ``list_pr_watchers`` /
    ``stop_pr_watcher`` calls. ``factory`` is how the registry builds a
    watcher from its config — normally ``_default_factory`` wiring to
    :class:`GithubClient`, but tests inject a fake factory.
    """

    def __init__(
        self,
        store: PRWatcherStore,
        publish: PublishFn,
        factory: Optional[Callable[[PRWatcherConfig], PRFleetWatcher]] = None,
    ):
        self.store = store
        self._publish = publish
        self._factory = factory or self._default_factory
        self._watchers: dict[str, _LiveWatcher] = {}
        # Guard against concurrent start/stop calls mutating the dict
        # while the agent is processing multiple bus requests.
        self._lock = asyncio.Lock()

    async def start(
        self,
        *,
        repos: list[str],
        pr_numbers: list[int],
        interval_s: int,
    ) -> str:
        """Create + schedule a watcher. Returns its id."""
        if not repos:
            raise ValueError("repos must be non-empty")
        if interval_s <= 0:
            raise ValueError("interval_s must be positive")
        config = PRWatcherConfig(
            watcher_id=_new_watcher_id(),
            repos=list(repos),
            pr_numbers=list(pr_numbers),
            interval_s=interval_s,
            started_at=time.time(),
        )
        await self._spawn(config, persist=True)
        return config.watcher_id

    async def rehydrate(self) -> list[str]:
        """Resume every persisted watcher as a live task.

        Called on agent startup so that an agent restart picks up
        watchers the user previously started without them re-emitting
        already-seen transitions — the dedupe table is keyed by
        ``watcher_id`` and we reuse the same id here. Returns the list
        of watcher ids that were spawned (may be empty if nothing was
        persisted or if every row was already live).

        Idempotent: calling ``rehydrate()`` twice won't double-spawn a
        live watcher (the in-memory ``_watchers`` map is the source of
        truth for "already running"). This matters for the odd case
        where the caller ran ``start`` before ``rehydrate`` — we don't
        want to clobber a freshly-spawned task.
        """
        spawned: list[str] = []
        rows = self.store.list_watchers()
        for row in rows:
            watcher_id = row["watcher_id"]
            # Skip anything we've already got a live task for — re-spawn
            # of an already-running watcher would leak the prior task
            # and risk duplicate publishes even with dedupe.
            async with self._lock:
                if watcher_id in self._watchers:
                    continue
            config = PRWatcherConfig(
                watcher_id=watcher_id,
                repos=list(row["repos"]),
                pr_numbers=list(row["pr_numbers"]),
                interval_s=int(row["interval_s"]),
                started_at=float(row["started_at"]),
            )
            # persist=False: the registry row already exists, and
            # rewriting it would reset last_poll_at and started_at.
            await self._spawn(config, persist=False)
            spawned.append(watcher_id)
        return spawned

    async def _spawn(self, config: PRWatcherConfig, *, persist: bool) -> None:
        """Build a live watcher task for ``config``.

        Shared by :meth:`start` (new watcher → persist registry row)
        and :meth:`rehydrate` (existing row → reuse). A no-op if a live
        watcher with the same id is already tracked; that makes the
        "already exists" case (e.g. rehydrate races with a caller that
        happened to start() by the same id) fall through silently
        instead of raising.
        """
        async with self._lock:
            if config.watcher_id in self._watchers:
                return
        watcher = self._factory(config)
        if persist:
            self.store.register_watcher(
                watcher_id=config.watcher_id,
                repos=config.repos,
                pr_numbers=config.pr_numbers,
                interval_s=config.interval_s,
                started_at=config.started_at,
            )
        stop_event = asyncio.Event()
        task = asyncio.create_task(
            watcher.run(stop_event), name=f"pr_watcher_{config.watcher_id}"
        )
        async with self._lock:
            # Re-check under the lock — if a concurrent caller beat us,
            # tear down our task rather than stomping theirs.
            if config.watcher_id in self._watchers:
                stop_event.set()
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
                return
            self._watchers[config.watcher_id] = _LiveWatcher(
                watcher=watcher, stop_event=stop_event, task=task,
            )

    async def stop(self, watcher_id: str) -> bool:
        """Cancel the watcher task and remove persistent state.

        Returns True if a matching watcher was stopped, False if unknown.
        """
        async with self._lock:
            live = self._watchers.pop(watcher_id, None)
        if live is None:
            # Still remove DB state in case a previous restart left it
            # dangling — stop should be idempotent and cleaning.
            self.store.remove_watcher(watcher_id)
            return False
        live.stop_event.set()
        try:
            await asyncio.wait_for(live.task, timeout=5.0)
        except asyncio.TimeoutError:
            live.task.cancel()
            try:
                await live.task
            except (asyncio.CancelledError, Exception):
                pass
        except asyncio.CancelledError:
            # If the caller themselves is being cancelled while waiting
            # on live.task, propagate — don't swallow cooperative
            # cancellation into the "shutdown succeeded" path.
            raise
        except Exception:
            pass
        self.store.remove_watcher(watcher_id)
        return True

    def list_watchers(self) -> list[dict]:
        """List live watchers with their poll metadata.

        Reads from the persistent store so registry membership matches
        the DB even under replica/restore scenarios. Adds ``pr_count``
        from the live in-memory watcher when available; defaults to 0
        otherwise.
        """
        rows = self.store.list_watchers()
        for row in rows:
            live = self._watchers.get(row["watcher_id"])
            row["pr_count"] = live.watcher.pr_count if live else 0
        return rows

    def fleet_snapshot(self, watcher_id: Optional[str] = None) -> dict:
        """Return the ``developer.pr_fleet_status`` response shape.

        Walks live watchers' cached snapshots and projects them into the
        compact fleet-item form. When ``watcher_id`` is supplied, only
        that watcher's PRs land in the ``fleet`` list (and only that
        watcher's row lands in ``watchers``); when ``None``, every live
        watcher is included. An unknown ``watcher_id`` yields an empty
        fleet and an empty watchers list — the caller learns about the
        typo without us raising.

        Intentionally synchronous: it reads in-memory state and a single
        SQLite row per watcher (via :meth:`PRWatcherStore.list_watchers`)
        — no GitHub traffic. That's what makes this a cheap "what's
        going on" query compared to a subscription on ``pr.*`` events.
        """
        rows = self.store.list_watchers()
        if watcher_id is not None:
            rows = [r for r in rows if r["watcher_id"] == watcher_id]
        watchers_out: list[dict] = []
        fleet_out: list[dict] = []
        for row in rows:
            wid = row["watcher_id"]
            live = self._watchers.get(wid)
            # ``pr_fleet_status`` is strictly a live-fleet view: rows that
            # exist in the registry table but have no in-memory watcher
            # handle (startup before rehydrate, a stopped-but-not-yet-
            # cleaned row, or a row owned by a sibling registry sharing
            # the same store) must not appear. A previous revision
            # surfaced persisted-only rows in ``watchers`` with an empty
            # fleet, which made the response lie about what was actually
            # live — bug caught in Copilot R1 on PR #41. Historical
            # / stopped-watcher views, if ever wanted, belong on a
            # separate skill with its own schema.
            if live is None:
                continue
            pr_count = live.watcher.pr_count
            # ISO-format started_at alongside the float so downstream
            # consumers aren't forced to know which epoch base we used.
            started_at_raw = float(row.get("started_at", 0.0) or 0.0)
            started_at_iso = (
                datetime.fromtimestamp(started_at_raw, tz=timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ")
                if started_at_raw > 0 else ""
            )
            watchers_out.append({
                "id": wid,
                "started_at": started_at_iso,
                "pr_count": pr_count,
            })
            for snapshot in live.watcher.latest_snapshots():
                fleet_out.append(_fleet_item_from_snapshot(snapshot))
        return {
            "t": _now_iso(),
            "watchers": watchers_out,
            "fleet": fleet_out,
        }

    async def shutdown(self) -> None:
        """Cancel every live watcher (called on agent shutdown)."""
        ids = list(self._watchers.keys())
        for watcher_id in ids:
            try:
                await self.stop(watcher_id)
            except asyncio.CancelledError:
                # CancelledError subclasses Exception; without this the
                # log-and-continue fallback would mask cooperative
                # cancellation of the shutdown task itself.
                raise
            except Exception as e:
                logger.warning("registry shutdown: stop %s failed: %s", watcher_id, e)

    def _default_factory(self, config: PRWatcherConfig) -> PRFleetWatcher:
        """Wire a watcher against the real :class:`GithubClient`.

        Resolves ``self_login`` lazily on first fetch and caches it for
        the watcher's lifetime (an authenticated GitHub token's login
        doesn't change under us). This is the identity used to filter
        self-authored comments on both comment channels — without it the
        watcher would re-emit our own ``@copilot please re-review``
        triggers and bookkeeping replies as external events.

        Cache uses a single-slot list (closure mutable) rather than a
        class attribute so per-watcher state stays scoped to the factory
        closure; each watcher runs at its own pace and may have different
        token wiring in a future multi-token setup.
        """
        gh = GithubClient()
        self_login_cache: list[str | None] = [None]

        async def _resolve_self_login() -> str:
            if self_login_cache[0] is None:
                try:
                    self_login_cache[0] = await gh.get_authenticated_user_login()
                except asyncio.CancelledError:
                    # CancelledError subclasses Exception; degrading to
                    # empty here would swallow cooperative cancellation
                    # of the watcher task mid-resolve.
                    raise
                except Exception:
                    # On failure, degrade to empty — filter disabled,
                    # watcher keeps running. Subscribers may see
                    # self-authored comments as external until next
                    # successful resolve; acceptable and noisy-loud.
                    self_login_cache[0] = ""
            return self_login_cache[0] or ""

        async def fetch_snapshot(repo: str, pr_number: int) -> PRSnapshot:
            self_login = await _resolve_self_login()
            return await _snapshot_from_github(gh, repo, pr_number, self_login)

        async def list_open_prs(repo: str) -> list[int]:
            return await _list_open_pr_numbers(gh, repo)

        return PRFleetWatcher(
            config=config,
            store=self.store,
            publish=self._publish,
            fetch_snapshot=fetch_snapshot,
            list_open_prs=list_open_prs,
        )


def _new_watcher_id() -> str:
    return f"prw_{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# GitHub fetch helpers
# ---------------------------------------------------------------------------


async def _snapshot_from_github(
    gh: GithubClient, repo: str, pr_number: int,
    self_login: str = "",
) -> PRSnapshot:
    """Fetch PR metadata + reviews + comment channels and compose a
    :class:`PRSnapshot`.

    Uses :meth:`GithubClient.get_pr` for PR state,
    :meth:`GithubClient.list_pr_reviews` for formal reviews,
    :meth:`GithubClient.list_pr_issue_comments` for top-level PR
    discussion (``/issues/N/comments``), and
    :meth:`GithubClient.list_pr_review_comments` for inline review-
    thread comments (``/pulls/N/comments``).

    ``self_login`` filters out self-authored comments on both comment
    channels so the watcher's own ``@copilot please re-review`` triggers
    and bookkeeping replies don't fire ``pr.comment_posted`` events.
    An empty ``self_login`` disables the filter (e.g. tests that don't
    care, or unauthenticated clients where we can't discover the login).
    """
    pr = await gh.get_pr(repo, pr_number)
    reviews_raw = await gh.list_pr_reviews(repo, pr_number)
    # ``body`` is populated here so ``pr.fleet_digest`` review entries can
    # carry the review's summary text without a second API call — the
    # underlying ``list_pr_reviews`` call already fetches it (see
    # :class:`GithubReview`), so this is pure projection, no extra traffic.
    # A previous revision left ``body`` unmapped and the digest emitter's
    # ``_truncate_body`` call was dead — bug caught in Copilot R1 on PR #41.
    reviews = [
        {
            "id": r.id,
            "reviewer": r.reviewer,
            "state": r.state,
            "body": r.body or "",
            "submitted_at": r.submitted_at or "",
        }
        for r in reviews_raw
    ]

    # Comment channels (fr_developer_e2bdd869). Failures on these are
    # deliberately surfaced rather than swallowed: the caller
    # (:meth:`PRFleetWatcher.poll_once`) already catches
    # :class:`GithubClientError` and emits ``pr.poll_error`` — so a
    # transient comment-endpoint hiccup lands in the same path as every
    # other fetch error instead of silently hiding missed events.
    issue_comments_raw = await gh.list_pr_issue_comments(repo, pr_number)
    inline_comments_raw = await gh.list_pr_review_comments(repo, pr_number)
    # GitHub logins are case-insensitive. Normalize once and compare
    # via ``.casefold()`` (proper Unicode-aware case folding, not
    # ``.lower()``). Empty ``self_login`` disables filtering entirely.
    self_login_cf = self_login.casefold() if self_login else ""

    def _is_self(login: str) -> bool:
        return bool(self_login_cf) and (login or "").casefold() == self_login_cf

    external_issue_comments = [
        {
            "id": c.get("id"),
            "author": c.get("user", ""),
            "body": c.get("body", "") or "",
            "posted_at": c.get("created_at", "") or "",
        }
        for c in issue_comments_raw
        if not _is_self(c.get("user", ""))
    ]
    inline_findings = [
        {
            "id": c.id,
            "author": c.reviewer,
            "body": c.body or "",
            "posted_at": c.created_at or "",
            "path": c.path,
            "line": c.line,
            "review_id": c.pull_request_review_id,
        }
        for c in inline_comments_raw
        if not _is_self(c.reviewer or "")
    ]

    merged = bool(pr.get("state") == "closed" and _pr_was_merged(pr))
    merged_at = _extract_merged_at(pr)
    snapshot = PRSnapshot(
        repo=repo,
        pr_number=pr_number,
        head_sha=str(pr.get("head_sha") or ""),
        state=str(pr.get("state") or "open"),
        merged=merged,
        merged_at=merged_at,
        mergeable=pr.get("mergeable"),
        merge_state=str(pr.get("mergeable_state") or ""),
        reviews=reviews,
        external_issue_comments=external_issue_comments,
        inline_findings=inline_findings,
    )
    _populate_comment_summaries(snapshot)
    return snapshot


def _populate_comment_summaries(snapshot: PRSnapshot) -> None:
    """Fill the derived ``latest_*`` / ``*_count`` snapshot fields.

    Kept as a free function so tests that construct a snapshot with
    pre-assembled lists can call it too — the summary fields are
    consistent with the lists however the snapshot is built.
    """
    snapshot.external_issue_comments_count = len(snapshot.external_issue_comments)
    if snapshot.external_issue_comments:
        latest = snapshot.external_issue_comments[-1]
        snapshot.latest_issue_comment_at = latest.get("posted_at") or None
        snapshot.latest_issue_comment_by = latest.get("author") or None
        body = latest.get("body") or ""
        snapshot.latest_issue_comment_preview = body[:COMMENT_PREVIEW_CHARS]
    snapshot.inline_findings_count = len(snapshot.inline_findings)
    if snapshot.inline_findings:
        latest = snapshot.inline_findings[-1]
        snapshot.latest_inline_at = latest.get("posted_at") or None
        snapshot.latest_inline_by = latest.get("author") or None
        snapshot.latest_inline_path = latest.get("path")
        snapshot.latest_inline_line = latest.get("line")
        body = latest.get("body") or ""
        snapshot.latest_inline_preview = body[:COMMENT_PREVIEW_CHARS]


def _pr_was_merged(pr: dict) -> bool:
    """Was this PR merged? Derivation from get_pr output.

    As of ``fr_developer_207ff0fb``, :meth:`GithubClient.get_pr`
    surfaces both ``merged`` (bool) and ``merged_at`` (ISO8601 str or
    ``None``). We accept either signal — ``merged=True`` alone, or a
    non-null ``merged_at`` — so the helper stays tolerant of older
    dict shapes still produced in tests or other call paths.
    """
    return bool(pr.get("merged") or pr.get("merged_at"))


def _extract_merged_at(pr: dict) -> str:
    raw = pr.get("merged_at")
    if raw is None:
        return ""
    return str(raw)


async def _list_open_pr_numbers(gh: GithubClient, repo: str) -> list[int]:
    """Enumerate open PRs in ``repo``.

    Uses githubkit's REST pulls.list with ``state=open``. No author
    filter: the FR's "authored by configured GH identity" variant is
    deferred — callers who need author filtering pass ``pr_numbers``
    explicitly today. The shape stays compatible so a future
    enhancement can slot in without breaking the current skill.
    """
    client = gh._client()
    owner, name = GithubClient._split_repo(repo)
    try:
        resp = await client.rest.pulls.async_list(
            owner=owner, repo=name, state="open", per_page=100,
        )
    except asyncio.CancelledError:
        # CancelledError subclasses Exception; re-raise so cooperative
        # cancellation doesn't get wrapped as a GithubClientError.
        raise
    except Exception as e:
        raise GithubClientError(f"list_open_prs({repo}): {e}") from e
    return [pr.number for pr in resp.parsed_data]


# ---------------------------------------------------------------------------
# Utilities used by the skill layer (argument parsing)
# ---------------------------------------------------------------------------


def parse_repos_arg(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [v.strip() for v in str(value).split(",") if v.strip()]


def parse_pr_numbers_arg(value: Any) -> list[int]:
    """Parse ``pr_numbers`` from bus args (list or comma-separated).

    Rejects non-integer entries with :class:`ValueError` so the caller
    learns about the typo instead of silently ignoring it.
    """
    if value is None or value == "":
        return []
    if isinstance(value, list):
        parts: Iterable[Any] = value
    else:
        parts = str(value).split(",")
    result: list[int] = []
    for part in parts:
        s = str(part).strip()
        if not s:
            continue
        try:
            result.append(int(s))
        except ValueError as e:
            raise ValueError(f"pr_numbers contains non-integer {part!r}") from e
    return result
