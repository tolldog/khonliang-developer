"""Developer-owned FR store.

Thin wrapper over ``KnowledgeStore`` that treats FRs as Tier.DERIVED
entries tagged ``fr``, matching the storage pattern researcher uses for
its own FRs (so the upcoming data migration — ``fr_developer_0ab2aa9b``
— can move records with minimal transformation).

**Merge-aware from day one.** Read and write paths both respect the
``metadata.merged_into`` redirect field, even though the write operation
that populates those fields (``merge_frs``) is a follow-up PR. The idea:
ship the framework now so merges plug in later without retrofitting
every lookup.

Status terminology:
- ``open`` / ``planned`` / ``in_progress`` — active
- ``completed``, ``archived``, ``merged`` — terminal (distinct meanings)

Merge redirect algorithm will (in a later PR):
1. Create a fresh FR with combined content.
2. Set each source FR's status to ``merged`` with
   ``metadata.merged_into = new_id`` and ``metadata.merge_role``.
3. Source FRs are preserved verbatim (no status toggling after the fact),
   so the stale-status-overwrite class of bugs is structurally impossible.

For PR 1, the store:
- Reads follow redirects by default (``get(follow_redirect=True)``),
  so a lookup of a merged FR returns the terminal one with a
  ``redirected_from`` hint.
- Writes resolve any FR-id reference through the redirect chain before
  storage — so a new FR's ``depends_on`` never points at a merged-away id.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from khonliang.knowledge.store import (
    KnowledgeEntry,
    KnowledgeStore,
    Tier,
    EntryStatus,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


# FR status values. These are the allowed values for the top-level ``status``
# field on an FR record. We use ``EntryStatus.DISTILLED`` as the underlying
# KnowledgeEntry.status so FRs show up in the store's distilled-entry views,
# and put the FR-level status in metadata.fr_status — a deliberate separation:
# KnowledgeStore's status is about the entry's processing lifecycle; FR status
# is about the FR's PM lifecycle. They overlap conceptually but not
# operationally.
FR_STATUS_OPEN = "open"
FR_STATUS_PLANNED = "planned"
FR_STATUS_IN_PROGRESS = "in_progress"
FR_STATUS_COMPLETED = "completed"
FR_STATUS_ARCHIVED = "archived"
FR_STATUS_MERGED = "merged"

ACTIVE_STATUSES = {FR_STATUS_OPEN, FR_STATUS_PLANNED, FR_STATUS_IN_PROGRESS}
TERMINAL_STATUSES = {FR_STATUS_COMPLETED, FR_STATUS_ARCHIVED, FR_STATUS_MERGED}
ALL_STATUSES = ACTIVE_STATUSES | TERMINAL_STATUSES

# Allowed transitions — a sparse DAG.
# No transitions out of terminal states: once merged/archived/completed,
# an FR stays there. (Any resurrection would be a fresh FR that references
# the old one, not a status toggle.)
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    FR_STATUS_OPEN: {FR_STATUS_PLANNED, FR_STATUS_IN_PROGRESS, FR_STATUS_ARCHIVED, FR_STATUS_MERGED},
    FR_STATUS_PLANNED: {FR_STATUS_IN_PROGRESS, FR_STATUS_OPEN, FR_STATUS_ARCHIVED, FR_STATUS_MERGED},
    FR_STATUS_IN_PROGRESS: {FR_STATUS_COMPLETED, FR_STATUS_PLANNED, FR_STATUS_OPEN, FR_STATUS_ARCHIVED, FR_STATUS_MERGED},
    FR_STATUS_COMPLETED: set(),
    FR_STATUS_ARCHIVED: set(),
    FR_STATUS_MERGED: set(),
}


ALLOWED_PRIORITIES = {"high", "medium", "low"}

# Cap on redirect chain depth — prevents pathological cycles from
# manually-constructed metadata.
_MAX_REDIRECT_DEPTH = 16


# ---------------------------------------------------------------------------
# Domain object
# ---------------------------------------------------------------------------


@dataclass
class FR:
    """An FR record as read out of the store.

    ``redirected_from`` is set when :meth:`FRStore.get` followed a
    ``metadata.merged_into`` pointer; it's the id the caller originally
    asked for. Unset on direct lookups.
    """

    id: str
    target: str
    title: str
    description: str
    status: str
    priority: str
    concept: str
    classification: str
    backing_papers: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    branch: str = ""
    notes_history: list[dict] = field(default_factory=list)
    merged_into: Optional[str] = None
    merged_from: list[str] = field(default_factory=list)
    merge_role: str = ""
    merge_note: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    redirected_from: Optional[str] = None

    def to_public_dict(self) -> dict[str, Any]:
        """Serializable representation for MCP / JSON consumers."""
        return {
            "id": self.id,
            "target": self.target,
            "title": self.title,
            "description": self.description,
            "status": self.status,
            "priority": self.priority,
            "concept": self.concept,
            "classification": self.classification,
            "backing_papers": list(self.backing_papers),
            "depends_on": list(self.depends_on),
            "branch": self.branch,
            "notes_history": list(self.notes_history),
            "merged_into": self.merged_into,
            "merged_from": list(self.merged_from),
            "merge_role": self.merge_role,
            "merge_note": self.merge_note,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "redirected_from": self.redirected_from,
        }


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class FRError(ValueError):
    """Raised on invalid FR operations (bad status, unknown id, etc.)."""


class FRStore:
    """Developer-side FR store.

    Persists FRs as Tier.DERIVED entries with the ``fr`` tag in the
    underlying KnowledgeStore. One KnowledgeEntry per FR.

    Capability tracking (see :meth:`_record_capability`) writes a
    separate Tier.DERIVED entry per (target, capability-name) pair so
    that the capability graph survives every entry point — not just
    the MCP/skill path. Researcher's CLI currently drops this update
    on its own writes (noted in researcher's ``initial_code_review.md``);
    the developer implementation must not replicate that bug.
    """

    def __init__(self, knowledge: KnowledgeStore):
        self.knowledge = knowledge

    # ------------------------------------------------------------------
    # Read paths
    # ------------------------------------------------------------------

    def get(self, fr_id: str, *, follow_redirect: bool = True) -> Optional[FR]:
        """Return an FR by id.

        When ``follow_redirect`` is True (default) and the FR has status
        ``merged`` with ``metadata.merged_into`` set, walk the chain and
        return the terminal FR with ``redirected_from`` set to the
        caller's original id. Raises :class:`FRError` if the chain
        exceeds ``_MAX_REDIRECT_DEPTH`` (defensive against bad data).

        Returns None if the id isn't in the store.
        """
        entry = self.knowledge.get(fr_id)
        if entry is None:
            return None
        fr = _fr_from_entry(entry)
        if not follow_redirect:
            return fr

        if fr.status != FR_STATUS_MERGED or not fr.merged_into:
            return fr

        # Walk the chain
        seen = {fr.id}
        current = fr
        requested_id = fr.id
        for _ in range(_MAX_REDIRECT_DEPTH):
            next_id = current.merged_into
            if not next_id:
                break
            if next_id in seen:
                raise FRError(
                    f"redirect cycle detected while resolving {fr_id!r}: "
                    f"{' -> '.join(list(seen) + [next_id])}"
                )
            seen.add(next_id)
            next_entry = self.knowledge.get(next_id)
            if next_entry is None:
                # Dangling pointer; return the last well-formed FR with a hint
                fr.redirected_from = requested_id
                return fr
            current = _fr_from_entry(next_entry)
            if current.status != FR_STATUS_MERGED:
                current.redirected_from = requested_id
                return current
        raise FRError(
            f"redirect chain for {fr_id!r} exceeded depth {_MAX_REDIRECT_DEPTH}"
        )

    def resolve_id(self, fr_id: str) -> str:
        """Return the terminal id after walking the merge chain.

        If ``fr_id`` isn't merged (or doesn't exist), returns it unchanged.
        """
        fr = self.get(fr_id, follow_redirect=False)
        if fr is None or fr.status != FR_STATUS_MERGED or not fr.merged_into:
            return fr_id
        resolved = self.get(fr_id, follow_redirect=True)
        return resolved.id if resolved is not None else fr_id

    def list(
        self,
        *,
        target: Optional[str] = None,
        status: Optional[str] = None,
        include_all: bool = False,
    ) -> list[FR]:
        """List FRs in the store.

        Default: active statuses only (open / planned / in_progress).
        Pass ``include_all=True`` to include terminal states
        (completed, archived, merged).
        Pass ``status=<name>`` to filter to a single status
        (ignores ``include_all`` since it's more specific).
        """
        entries = self.knowledge.get_by_tier(Tier.DERIVED)
        frs: list[FR] = []
        for entry in entries:
            if "fr" not in (entry.tags or []):
                continue
            fr = _fr_from_entry(entry)
            if target and fr.target != target:
                continue
            if status:
                if fr.status != status:
                    continue
            elif not include_all and fr.status not in ACTIVE_STATUSES:
                continue
            frs.append(fr)
        # Stable ordering — priority (high, medium, low) then created_at asc
        priority_order = {"high": 0, "medium": 1, "low": 2}
        frs.sort(key=lambda f: (priority_order.get(f.priority, 99), f.created_at))
        return frs

    # ------------------------------------------------------------------
    # Write paths
    # ------------------------------------------------------------------

    def promote(
        self,
        *,
        target: str,
        title: str,
        description: str,
        priority: str = "medium",
        concept: str = "",
        classification: str = "app",
        backing_papers: Optional[Iterable[str]] = None,
    ) -> FR:
        """Create a new FR. Returns the stored :class:`FR`.

        ``id`` is derived deterministically from (target, title, concept)
        so re-promoting the same content yields the same id (collision
        detection via pre-existing entry).
        """
        if not target or not title:
            raise FRError("promote_fr requires non-empty target and title")
        if priority not in ALLOWED_PRIORITIES:
            raise FRError(
                f"priority must be one of {sorted(ALLOWED_PRIORITIES)}, got {priority!r}"
            )
        backing = list(backing_papers or [])

        fr_id = _derive_fr_id(target, title, concept)
        if self.knowledge.get(fr_id) is not None:
            raise FRError(
                f"fr already exists with id {fr_id} (same target+title+concept as an existing FR)"
            )

        now = time.time()
        fr = FR(
            id=fr_id,
            target=target,
            title=title,
            description=description,
            status=FR_STATUS_OPEN,
            priority=priority,
            concept=concept,
            classification=classification,
            backing_papers=backing,
            depends_on=[],
            branch="",
            notes_history=[{"at": now, "status": FR_STATUS_OPEN, "notes": "promoted"}],
            merged_into=None,
            merged_from=[],
            merge_role="",
            merge_note="",
            created_at=now,
            updated_at=now,
        )
        self._store(fr)
        # New FRs start in "open"; no capability entry yet (capabilities are
        # recorded when an FR is planned / in_progress / completed).
        return fr

    def update_status(
        self,
        fr_id: str,
        status: str,
        *,
        branch: str = "",
        notes: str = "",
    ) -> FR:
        """Advance an FR's lifecycle status.

        Resolves ``fr_id`` through merge redirects before updating, so
        callers can use old ids and the update lands on the terminal FR.

        Returns the updated FR. Raises :class:`FRError` on unknown id,
        invalid status name, or disallowed transition.
        """
        if status not in ALL_STATUSES:
            raise FRError(
                f"status must be one of {sorted(ALL_STATUSES)}, got {status!r}"
            )
        resolved_id = self.resolve_id(fr_id)
        fr = self.get(resolved_id, follow_redirect=False)
        if fr is None:
            raise FRError(f"unknown fr id: {fr_id}")

        if fr.status == status:
            # Idempotent — but still append a notes entry if one was provided,
            # so the audit trail captures intent (e.g. "confirmed in_progress
            # after a crash recovery").
            if notes:
                fr.notes_history.append({
                    "at": time.time(), "status": status, "notes": notes,
                })
                self._store(fr)
            return fr

        allowed = ALLOWED_TRANSITIONS.get(fr.status, set())
        if status not in allowed:
            raise FRError(
                f"illegal transition {fr.status!r} -> {status!r} for {resolved_id}. "
                f"Allowed from {fr.status!r}: {sorted(allowed)}"
            )

        now = time.time()
        fr.status = status
        if branch:
            fr.branch = branch
        fr.notes_history.append({
            "at": now,
            "status": status,
            "branch": branch,
            "notes": notes,
        })
        fr.updated_at = now
        self._store(fr)

        # Capability tracking: update the capability graph on every
        # status transition that affects it. Must happen here so both
        # skill-path and direct-API callers get consistent behavior —
        # the researcher-CLI bug was dropping this update on one path.
        self._record_capability(fr)
        return fr

    def set_dependency(
        self,
        fr_id: str,
        depends_on: Iterable[str],
    ) -> FR:
        """Replace an FR's ``depends_on`` list.

        Each dep is resolved through merge redirects before storage, so a
        caller passing a merged-away id gets auto-forwarded to the
        terminal FR.

        Detects cycles using resolved ids (an FR can't depend on itself
        directly or transitively). Raises :class:`FRError` on cycle or
        unknown id.
        """
        resolved_id = self.resolve_id(fr_id)
        fr = self.get(resolved_id, follow_redirect=False)
        if fr is None:
            raise FRError(f"unknown fr id: {fr_id}")

        resolved_deps: list[str] = []
        for dep in depends_on:
            dep = (dep or "").strip()
            if not dep:
                continue
            resolved = self.resolve_id(dep)
            if resolved == resolved_id:
                raise FRError(
                    f"cycle: {resolved_id} cannot depend on {dep!r} "
                    f"(resolves to itself)"
                )
            if self.knowledge.get(resolved) is None:
                raise FRError(f"unknown dependency fr id: {dep!r}")
            if resolved not in resolved_deps:
                resolved_deps.append(resolved)

        # Check transitive cycle: walk each dep's chain; bail if we see
        # resolved_id in there.
        for dep in resolved_deps:
            if self._depends_transitively_on(dep, resolved_id):
                raise FRError(
                    f"transitive cycle: {dep} already depends on {resolved_id}"
                )

        fr.depends_on = resolved_deps
        fr.updated_at = time.time()
        self._store(fr)
        return fr

    # ------------------------------------------------------------------
    # Capability graph
    # ------------------------------------------------------------------

    def capabilities_for(self, target: str) -> list[dict[str, Any]]:
        """Return the capability entries for ``target`` as flat dicts.

        Capability entries are stored as separate Tier.DERIVED records
        with the ``capability`` tag and metadata.target set. One per
        (target, capability_name) pair.
        """
        entries = self.knowledge.get_by_tier(Tier.DERIVED)
        caps = []
        for entry in entries:
            if "capability" not in (entry.tags or []):
                continue
            meta = entry.metadata or {}
            if meta.get("target") != target:
                continue
            caps.append({
                "id": entry.id,
                "name": entry.title,
                "target": target,
                "status": meta.get("capability_status", "unknown"),
                "fr_id": meta.get("fr_id", ""),
                "updated_at": entry.updated_at,
            })
        caps.sort(key=lambda c: c["updated_at"], reverse=True)
        return caps

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _store(self, fr: FR) -> None:
        """Serialize an :class:`FR` back into the KnowledgeStore."""
        entry = KnowledgeEntry(
            id=fr.id,
            tier=Tier.DERIVED,
            title=fr.title,
            content=fr.description,
            source="developer.fr_store",
            scope="development",
            confidence=1.0,
            status=EntryStatus.DISTILLED,
            tags=["fr", f"target:{fr.target}", fr.classification],
            metadata={
                "fr_status": fr.status,
                "priority": fr.priority,
                "concept": fr.concept,
                "classification": fr.classification,
                "target": fr.target,
                "backing_papers": list(fr.backing_papers),
                "depends_on": list(fr.depends_on),
                "branch": fr.branch,
                "notes_history": list(fr.notes_history),
                "merged_into": fr.merged_into,
                "merged_from": list(fr.merged_from),
                "merge_role": fr.merge_role,
                "merge_note": fr.merge_note,
            },
            created_at=fr.created_at,
            updated_at=fr.updated_at,
        )
        self.knowledge.add(entry)

    def _record_capability(self, fr: FR) -> None:
        """Write/update the capability entry for an FR's current status.

        - ``planned`` or ``in_progress`` → capability status ``planned``
        - ``completed`` → capability status ``exists``
        - Terminal non-success (archived / merged) → capability marked
          ``abandoned`` so future synergize_concepts doesn't re-propose
          the same work but the evidence of the attempt is preserved.
        - ``open`` → no capability entry (FR hasn't been accepted yet).
        """
        status_to_capability = {
            FR_STATUS_PLANNED: "planned",
            FR_STATUS_IN_PROGRESS: "planned",
            FR_STATUS_COMPLETED: "exists",
            FR_STATUS_ARCHIVED: "abandoned",
            FR_STATUS_MERGED: "abandoned",
        }
        capability_status = status_to_capability.get(fr.status)
        if capability_status is None:
            return

        cap_id = _derive_capability_id(fr.target, fr.title)
        now = time.time()
        entry = KnowledgeEntry(
            id=cap_id,
            tier=Tier.DERIVED,
            title=fr.title,
            content=fr.description,
            source="developer.fr_store.capability",
            scope="development",
            confidence=1.0,
            status=EntryStatus.DISTILLED,
            tags=["capability", f"target:{fr.target}"],
            metadata={
                "target": fr.target,
                "capability_status": capability_status,
                "fr_id": fr.id,
                "updated_at": now,
            },
            created_at=now,
            updated_at=now,
        )
        self.knowledge.add(entry)

    def _depends_transitively_on(self, start: str, target: str) -> bool:
        """True if ``start`` (or any of its deps, recursively) depends on ``target``."""
        visited: set[str] = set()
        stack = [start]
        while stack:
            cur = stack.pop()
            if cur in visited:
                continue
            visited.add(cur)
            entry = self.knowledge.get(cur)
            if entry is None:
                continue
            deps = (entry.metadata or {}).get("depends_on") or []
            for dep in deps:
                if dep == target:
                    return True
                if dep not in visited:
                    stack.append(dep)
        return False


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------


def _fr_from_entry(entry: KnowledgeEntry) -> FR:
    """Convert a KnowledgeEntry back into an :class:`FR`."""
    meta = entry.metadata or {}
    return FR(
        id=entry.id,
        target=meta.get("target", ""),
        title=entry.title,
        description=entry.content,
        status=meta.get("fr_status", FR_STATUS_OPEN),
        priority=meta.get("priority", "medium"),
        concept=meta.get("concept", ""),
        classification=meta.get("classification", "app"),
        backing_papers=list(meta.get("backing_papers") or []),
        depends_on=list(meta.get("depends_on") or []),
        branch=meta.get("branch", ""),
        notes_history=list(meta.get("notes_history") or []),
        merged_into=meta.get("merged_into"),
        merged_from=list(meta.get("merged_from") or []),
        merge_role=meta.get("merge_role", ""),
        merge_note=meta.get("merge_note", ""),
        created_at=entry.created_at,
        updated_at=entry.updated_at,
    )


def _derive_fr_id(target: str, title: str, concept: str) -> str:
    """Stable FR id from (target, title, concept).

    Matches researcher's format: ``fr_<target>_<8 hex of sha256>``. Same
    input produces same id, so re-promoting the same content is a no-op
    (detected upstream in promote()).
    """
    payload = f"{target}:{title}:{concept}".encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()[:8]
    return f"fr_{target}_{digest}"


def _derive_capability_id(target: str, capability_name: str) -> str:
    """Stable capability id per (target, capability)."""
    payload = f"cap:{target}:{capability_name}".encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()[:12]
    return f"capability_{target}_{digest}"
