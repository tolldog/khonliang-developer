"""Developer-owned bug store.

Phase 1 of the tracking-infrastructure stack (``fr_developer_f669bd33``).
CRUD-only slice: creation, lookup, lifecycle status, PR linkage, closure,
duplicate marking, and list/filter. Triage promotion (to FRs) and
``report_gap`` integration are deferred to Phase 2.

Mirrors :mod:`developer.fr_store` on storage: one ``Tier.DERIVED``
``KnowledgeEntry`` per bug, tagged ``bug``. Keeps the store independent
of :class:`developer.fr_store.FRStore`; the Phase 2 triage path will
layer cross-store linkage on top of these primitives.

Schema (per ``fr_developer_f669bd33``):
    id           — ``bug_<target>_<8 hex of sha256>``
    title
    description
    reproduction
    observed_at  — epoch seconds
    observed_entity
    severity     — blocker / high / medium / low  (default medium)
    status       — open / triaged / in_progress / fixed / wontfix / duplicate
                   (default open)
    target
    reporter
    linked_frs   — list[str]  (populated by Phase 2)
    linked_pr    — str
    duplicate_of — str  (set by :meth:`mark_duplicate`)
    source       — optional attribution struct (see ``fr_developer_47271f34``).
                   Shape: ``{kind, url, repo, number, author, labels,
                   created_at, issue_title, body_hash}``. Default ``None``;
                   populated by Phase 2's GitHub issue ingest.

Seed data: on first construction when zero ``bug``-tagged entries exist,
writes two curated entries pulled from the FR body (distiller
RL-mis-tag + Substack 403). Idempotent: subsequent constructions are
no-ops because the empty check finds the seed rows.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from khonliang.knowledge.store import (
    EntryStatus,
    KnowledgeEntry,
    KnowledgeStore,
    Tier,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BUG_SEVERITY_BLOCKER = "blocker"
BUG_SEVERITY_HIGH = "high"
BUG_SEVERITY_MEDIUM = "medium"
BUG_SEVERITY_LOW = "low"

ALLOWED_SEVERITIES = {
    BUG_SEVERITY_BLOCKER,
    BUG_SEVERITY_HIGH,
    BUG_SEVERITY_MEDIUM,
    BUG_SEVERITY_LOW,
}

# Rank used for severity_min filtering — lower int = more severe.
_SEVERITY_RANK: dict[str, int] = {
    BUG_SEVERITY_BLOCKER: 0,
    BUG_SEVERITY_HIGH: 1,
    BUG_SEVERITY_MEDIUM: 2,
    BUG_SEVERITY_LOW: 3,
}

BUG_STATUS_OPEN = "open"
BUG_STATUS_TRIAGED = "triaged"
BUG_STATUS_IN_PROGRESS = "in_progress"
BUG_STATUS_FIXED = "fixed"
BUG_STATUS_WONTFIX = "wontfix"
BUG_STATUS_DUPLICATE = "duplicate"

ACTIVE_STATUSES = {BUG_STATUS_OPEN, BUG_STATUS_TRIAGED, BUG_STATUS_IN_PROGRESS}
TERMINAL_STATUSES = {BUG_STATUS_FIXED, BUG_STATUS_WONTFIX, BUG_STATUS_DUPLICATE}
ALL_STATUSES = ACTIVE_STATUSES | TERMINAL_STATUSES


# ---------------------------------------------------------------------------
# Domain object
# ---------------------------------------------------------------------------


@dataclass
class Bug:
    """A bug record as read out of the store."""

    id: str
    target: str
    title: str
    description: str
    reproduction: str
    observed_entity: str
    severity: str
    status: str
    reporter: str
    linked_frs: list[str] = field(default_factory=list)
    linked_pr: str = ""
    duplicate_of: str = ""
    observed_at: float = 0.0
    created_at: float = 0.0
    updated_at: float = 0.0
    notes_history: list[dict] = field(default_factory=list)
    source: Optional[dict[str, Any]] = None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "target": self.target,
            "title": self.title,
            "description": self.description,
            "reproduction": self.reproduction,
            "observed_entity": self.observed_entity,
            "severity": self.severity,
            "status": self.status,
            "reporter": self.reporter,
            "linked_frs": list(self.linked_frs),
            "linked_pr": self.linked_pr,
            "duplicate_of": self.duplicate_of,
            "observed_at": self.observed_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "notes_history": list(self.notes_history),
            "source": dict(self.source) if self.source else None,
        }

    def to_brief_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "observed_at": self.observed_at,
            "updated_at": self.updated_at,
        }

    def to_compact_dict(self) -> dict[str, Any]:
        d = self.to_brief_dict()
        d["severity"] = self.severity
        d["target"] = self.target
        return d


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class BugError(ValueError):
    """Raised on invalid bug operations (bad severity/status, unknown id)."""


class BugStore:
    """Developer-side bug store.

    Persists bugs as ``Tier.DERIVED`` entries with the ``bug`` tag in the
    underlying :class:`KnowledgeStore`. One entry per bug.

    Seeds two curated entries on the first construction (from
    ``fr_developer_f669bd33``'s body) if the store contains zero
    ``bug``-tagged entries. Idempotent across restarts.
    """

    def __init__(self, knowledge: KnowledgeStore, *, seed: bool = True):
        self.knowledge = knowledge
        if seed:
            self._seed_if_empty()

    # ------------------------------------------------------------------
    # Read paths
    # ------------------------------------------------------------------

    def get(self, bug_id: str) -> Optional[Bug]:
        entry = self.knowledge.get(bug_id)
        if entry is None or "bug" not in (entry.tags or []):
            return None
        return _bug_from_entry(entry)

    def list(
        self,
        *,
        target: str = "",
        severity_min: str = "",
        status: Optional[Iterable[str]] = None,
        include_terminal: bool = False,
    ) -> list[Bug]:
        """List bugs in the store.

        ``status`` accepts None (default), ``"all"``, an iterable of names,
        or a comma-separated string. When None, excludes terminal statuses
        (fixed / wontfix / duplicate). ``"all"`` overrides. ``severity_min``
        filters out anything less severe than the given cutoff (e.g.
        ``"medium"`` keeps medium / high / blocker). Ordering is newest
        ``observed_at`` first.
        """
        allowed_statuses = _parse_status_filter(status, include_terminal=include_terminal)
        cutoff_rank = _SEVERITY_RANK.get(severity_min) if severity_min else None

        entries = self.knowledge.get_by_tier(Tier.DERIVED)
        bugs: list[Bug] = []
        for entry in entries:
            if "bug" not in (entry.tags or []):
                continue
            bug = _bug_from_entry(entry)
            if target and bug.target != target:
                continue
            if allowed_statuses is not None and bug.status not in allowed_statuses:
                continue
            if cutoff_rank is not None:
                rank = _SEVERITY_RANK.get(bug.severity, 99)
                if rank > cutoff_rank:
                    continue
            bugs.append(bug)
        bugs.sort(key=lambda b: b.observed_at, reverse=True)
        return bugs

    # ------------------------------------------------------------------
    # Write paths
    # ------------------------------------------------------------------

    def file_bug(
        self,
        *,
        target: str,
        title: str,
        description: str,
        reproduction: str = "",
        observed_entity: str = "",
        severity: str = BUG_SEVERITY_MEDIUM,
        reporter: str = "",
        source: Optional[dict[str, Any]] = None,
        observed_at: Optional[float] = None,
    ) -> Bug:
        """File a new bug. Returns the stored :class:`Bug`.

        ``id`` is derived deterministically from (target, title, description,
        observed_entity) so re-filing the same content yields the same id
        and an already-exists error.
        """
        if not target or not title:
            raise BugError("file_bug requires non-empty target and title")
        if severity not in ALLOWED_SEVERITIES:
            raise BugError(
                f"severity must be one of {sorted(ALLOWED_SEVERITIES)}, got {severity!r}"
            )

        bug_id = _derive_bug_id(target, title, description, observed_entity)
        existing = self.knowledge.get(bug_id)
        if existing is not None and "bug" in (existing.tags or []):
            raise BugError(
                f"bug already exists with id {bug_id} "
                "(same target+title+description+observed_entity as an existing bug)"
            )

        now = time.time()
        observed = observed_at if observed_at is not None else now
        bug = Bug(
            id=bug_id,
            target=target,
            title=title,
            description=description,
            reproduction=reproduction,
            observed_entity=observed_entity,
            severity=severity,
            status=BUG_STATUS_OPEN,
            reporter=reporter,
            linked_frs=[],
            linked_pr="",
            duplicate_of="",
            observed_at=observed,
            created_at=now,
            updated_at=now,
            notes_history=[{"at": now, "status": BUG_STATUS_OPEN, "notes": "filed"}],
            source=dict(source) if source else None,
        )
        self._store(bug)
        return bug

    def update_status(
        self,
        bug_id: str,
        status: str,
        *,
        notes: str = "",
    ) -> Bug:
        """Advance a bug's lifecycle status.

        Terminal statuses (fixed / wontfix / duplicate) are enforced by
        dedicated methods (:meth:`close_bug`, :meth:`mark_duplicate`); the
        general :meth:`update_status` accepts any name in ``ALL_STATUSES``
        but is intended mainly for active-state transitions. Callers can
        still pass a terminal status here for symmetry — the FR body lists
        ``fixed / wontfix / duplicate`` as valid terminal statuses.
        """
        if status not in ALL_STATUSES:
            raise BugError(
                f"status must be one of {sorted(ALL_STATUSES)}, got {status!r}"
            )
        bug = self.get(bug_id)
        if bug is None:
            raise BugError(f"unknown bug id: {bug_id}")

        if bug.status in TERMINAL_STATUSES and bug.status != status:
            raise BugError(
                f"cannot update {bug_id}: status is {bug.status!r} "
                "(terminal bugs are immutable)"
            )

        now = time.time()
        if bug.status == status:
            if notes:
                bug.notes_history.append({"at": now, "status": status, "notes": notes})
                bug.updated_at = now
                self._store(bug)
            return bug

        bug.status = status
        bug.notes_history.append({"at": now, "status": status, "notes": notes})
        bug.updated_at = now
        self._store(bug)
        return bug

    def link_bug_pr(self, bug_id: str, pr_url: str) -> Bug:
        """Record the PR URL fixing this bug."""
        bug = self.get(bug_id)
        if bug is None:
            raise BugError(f"unknown bug id: {bug_id}")
        if bug.status in TERMINAL_STATUSES:
            raise BugError(
                f"cannot link PR on {bug_id}: status is {bug.status!r} (terminal)"
            )
        pr_url = (pr_url or "").strip()
        if not pr_url:
            raise BugError("pr_url must be non-empty")
        bug.linked_pr = pr_url
        now = time.time()
        bug.notes_history.append({
            "at": now, "status": bug.status, "notes": f"linked pr {pr_url}",
        })
        bug.updated_at = now
        self._store(bug)
        return bug

    def close_bug(self, bug_id: str, resolution: str) -> Bug:
        """Close a bug with a terminal non-duplicate resolution.

        ``resolution`` must be ``fixed`` or ``wontfix``. For duplicates
        use :meth:`mark_duplicate`.
        """
        if resolution not in {BUG_STATUS_FIXED, BUG_STATUS_WONTFIX}:
            raise BugError(
                f"resolution must be 'fixed' or 'wontfix', got {resolution!r} "
                "(for duplicates call mark_duplicate)"
            )
        bug = self.get(bug_id)
        if bug is None:
            raise BugError(f"unknown bug id: {bug_id}")
        if bug.status in TERMINAL_STATUSES:
            raise BugError(
                f"cannot close {bug_id}: status is {bug.status!r} (already terminal)"
            )
        now = time.time()
        bug.status = resolution
        bug.notes_history.append({
            "at": now, "status": resolution, "notes": f"closed as {resolution}",
        })
        bug.updated_at = now
        self._store(bug)
        return bug

    def mark_duplicate(self, bug_id: str, duplicate_of: str) -> Bug:
        """Terminal: mark ``bug_id`` as a duplicate of ``duplicate_of``."""
        duplicate_of = (duplicate_of or "").strip()
        if not duplicate_of:
            raise BugError("duplicate_of must be non-empty")
        if duplicate_of == bug_id:
            raise BugError("a bug cannot be marked a duplicate of itself")
        if self.get(duplicate_of) is None:
            raise BugError(f"unknown duplicate target id: {duplicate_of!r}")
        bug = self.get(bug_id)
        if bug is None:
            raise BugError(f"unknown bug id: {bug_id}")
        if bug.status in TERMINAL_STATUSES:
            raise BugError(
                f"cannot mark duplicate on {bug_id}: status is {bug.status!r} (terminal)"
            )
        now = time.time()
        bug.status = BUG_STATUS_DUPLICATE
        bug.duplicate_of = duplicate_of
        bug.notes_history.append({
            "at": now,
            "status": BUG_STATUS_DUPLICATE,
            "notes": f"duplicate of {duplicate_of}",
        })
        bug.updated_at = now
        self._store(bug)
        return bug

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _store(self, bug: Bug) -> None:
        entry = KnowledgeEntry(
            id=bug.id,
            tier=Tier.DERIVED,
            title=bug.title,
            content=bug.description,
            source="developer.bug_store",
            scope="development",
            confidence=1.0,
            status=EntryStatus.DISTILLED,
            tags=["bug", f"target:{bug.target}", f"severity:{bug.severity}"],
            metadata={
                "bug_status": bug.status,
                "severity": bug.severity,
                "target": bug.target,
                "reproduction": bug.reproduction,
                "observed_entity": bug.observed_entity,
                "observed_at": bug.observed_at,
                "reporter": bug.reporter,
                "linked_frs": list(bug.linked_frs),
                "linked_pr": bug.linked_pr,
                "duplicate_of": bug.duplicate_of,
                "notes_history": list(bug.notes_history),
                "source": dict(bug.source) if bug.source else None,
            },
            created_at=bug.created_at,
            updated_at=bug.updated_at,
        )
        self.knowledge.add(entry)
        stored = self.knowledge.get(bug.id)
        if stored is not None:
            bug.created_at = stored.created_at
            bug.updated_at = stored.updated_at

    def _count_bugs(self) -> int:
        count = 0
        for entry in self.knowledge.get_by_tier(Tier.DERIVED):
            if "bug" in (entry.tags or []):
                count += 1
        return count

    def _seed_if_empty(self) -> None:
        """Write the curated seed entries if no bug entries exist yet.

        Seeds pulled verbatim from ``fr_developer_f669bd33``'s body.
        Idempotent: if any bug rows exist, this is a no-op. Called from
        ``__init__`` so first construction on a fresh DB produces a
        pre-populated tracker.
        """
        if self._count_bugs() > 0:
            return
        self.file_bug(
            target="researcher",
            title="Distiller tagged non-RL article as reinforcement-learning",
            description=(
                "Distiller auto-tagged a procedural-memory / workflow-portability "
                "article (entry 49a51fb54de6acaa, Marius Ursache \"Platform-Proof "
                "Work\") with domains multi-agent and reinforcement-learning. "
                "The RL tag is incorrect; the article is about human workflow "
                "portability, not reinforcement learning."
            ),
            observed_entity="distiller",
            severity=BUG_SEVERITY_LOW,
            reporter="user",
        )
        self.file_bug(
            target="researcher",
            title="fetch_paper returns 403 on Substack despite browser UA",
            description=(
                "fetch_paper returned 403 Forbidden on Substack despite the "
                "browser-like UA headers in researcher/fetcher.py:56-61. "
                "Workaround required WebFetch + ingest_file; should work "
                "end-to-end through fetch_paper."
            ),
            observed_entity="researcher/fetcher.py:56-61",
            severity=BUG_SEVERITY_MEDIUM,
            reporter="user",
        )


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------


def _bug_from_entry(entry: KnowledgeEntry) -> Bug:
    meta = entry.metadata or {}
    src = meta.get("source")
    return Bug(
        id=entry.id,
        target=meta.get("target", ""),
        title=entry.title,
        description=entry.content,
        reproduction=meta.get("reproduction", ""),
        observed_entity=meta.get("observed_entity", ""),
        severity=meta.get("severity", BUG_SEVERITY_MEDIUM),
        status=meta.get("bug_status", BUG_STATUS_OPEN),
        reporter=meta.get("reporter", ""),
        linked_frs=list(meta.get("linked_frs") or []),
        linked_pr=meta.get("linked_pr", ""),
        duplicate_of=meta.get("duplicate_of", ""),
        observed_at=float(meta.get("observed_at") or entry.created_at),
        created_at=entry.created_at,
        updated_at=entry.updated_at,
        notes_history=list(meta.get("notes_history") or []),
        source=dict(src) if isinstance(src, dict) else None,
    )


def _derive_bug_id(target: str, title: str, description: str, observed_entity: str) -> str:
    """Stable bug id per (target, title, description, observed_entity).

    Same content → same id, so re-filing the exact same bug is detected
    as a collision rather than silently duplicating rows.
    """
    payload = f"{target}:{title}:{description}:{observed_entity}".encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()[:8]
    return f"bug_{target}_{digest}"


def _parse_status_filter(
    status: Optional[Iterable[str] | str],
    *,
    include_terminal: bool,
) -> Optional[set[str]]:
    """Translate the public ``status`` argument into a set to filter on.

    Returns None when "no filter" (all statuses accepted). The default
    (status=None, include_terminal=False) filters to ACTIVE_STATUSES.
    ``"all"`` or include_terminal=True returns None.
    """
    if status is None:
        if include_terminal:
            return None
        return set(ACTIVE_STATUSES)
    if isinstance(status, str):
        if status.strip().lower() == "all":
            return None
        parts = [s.strip() for s in status.split(",") if s.strip()]
    else:
        parts = [str(s).strip() for s in status if str(s).strip()]
    if not parts:
        return set(ACTIVE_STATUSES) if not include_terminal else None
    allowed = set()
    for p in parts:
        if p.lower() == "all":
            return None
        if p not in ALL_STATUSES:
            raise BugError(
                f"status filter contains unknown value {p!r}; "
                f"allowed: {sorted(ALL_STATUSES | {'all'})}"
            )
        allowed.add(p)
    return allowed
