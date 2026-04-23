"""Developer-owned milestone store.

Milestones are the durable handoff between a ranked FR work unit and
implementation. They intentionally mirror FRStore's KnowledgeStore-backed
pattern: one Tier.DERIVED entry per milestone, tagged for search and
serialized through a small domain object.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from khonliang.knowledge.store import (
    EntryStatus,
    KnowledgeEntry,
    KnowledgeStore,
    Tier,
)

# The FR store is passed into :meth:`MilestoneStore.delete` as an
# argument rather than imported here so milestone_store stays free of
# any hard dependency on fr_store. The only place we need FR state is
# the "FR in_progress in this milestone" safety guard on delete, and
# duck-typing the argument keeps the module easy to unit-test in
# isolation and avoids the (currently hypothetical) import cycle.


MILESTONE_STATUS_PROPOSED = "proposed"
MILESTONE_STATUS_PLANNED = "planned"
MILESTONE_STATUS_IN_PROGRESS = "in_progress"
MILESTONE_STATUS_COMPLETED = "completed"
MILESTONE_STATUS_ABANDONED = "abandoned"
MILESTONE_STATUS_SUPERSEDED = "superseded"
# ``archived`` predates this FR and is kept as a legacy synonym for
# ``abandoned`` so existing on-disk milestones keep loading cleanly.
# New code should prefer ``abandoned``.
MILESTONE_STATUS_ARCHIVED = "archived"

ACTIVE_MILESTONE_STATUSES = {
    MILESTONE_STATUS_PROPOSED,
    MILESTONE_STATUS_PLANNED,
    MILESTONE_STATUS_IN_PROGRESS,
}
TERMINAL_MILESTONE_STATUSES = {
    MILESTONE_STATUS_COMPLETED,
    MILESTONE_STATUS_ABANDONED,
    MILESTONE_STATUS_SUPERSEDED,
    MILESTONE_STATUS_ARCHIVED,
}
ALL_MILESTONE_STATUSES = ACTIVE_MILESTONE_STATUSES | TERMINAL_MILESTONE_STATUSES

# Allowed forward transitions. Terminal states have no outgoing edges —
# rolling back out of a terminal state requires the caller to pass
# ``force=True`` to :meth:`MilestoneStore.update_status` (and is
# recorded in ``notes_history`` with a ``force_rollback`` marker).
ALLOWED_MILESTONE_TRANSITIONS: dict[str, set[str]] = {
    MILESTONE_STATUS_PROPOSED: {
        MILESTONE_STATUS_PLANNED,
        MILESTONE_STATUS_IN_PROGRESS,
        MILESTONE_STATUS_ABANDONED,
        MILESTONE_STATUS_SUPERSEDED,
        MILESTONE_STATUS_ARCHIVED,
    },
    MILESTONE_STATUS_PLANNED: {
        MILESTONE_STATUS_PROPOSED,
        MILESTONE_STATUS_IN_PROGRESS,
        MILESTONE_STATUS_ABANDONED,
        MILESTONE_STATUS_SUPERSEDED,
        MILESTONE_STATUS_ARCHIVED,
    },
    MILESTONE_STATUS_IN_PROGRESS: {
        MILESTONE_STATUS_PLANNED,
        MILESTONE_STATUS_COMPLETED,
        MILESTONE_STATUS_ABANDONED,
        MILESTONE_STATUS_SUPERSEDED,
        MILESTONE_STATUS_ARCHIVED,
    },
    MILESTONE_STATUS_COMPLETED: set(),
    MILESTONE_STATUS_ABANDONED: set(),
    MILESTONE_STATUS_SUPERSEDED: set(),
    MILESTONE_STATUS_ARCHIVED: set(),
}


class MilestoneError(ValueError):
    """Raised on invalid milestone operations."""


@dataclass
class Milestone:
    """A durable work unit ready for spec drafting and implementation."""

    id: str
    title: str
    target: str
    status: str
    summary: str
    fr_ids: list[str] = field(default_factory=list)
    work_unit: dict[str, Any] = field(default_factory=dict)
    source: str = "work_unit"
    rank: int = 0
    draft_spec: str = ""
    notes_history: list[dict] = field(default_factory=list)
    superseded_by: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "target": self.target,
            "status": self.status,
            "summary": self.summary,
            "fr_ids": list(self.fr_ids),
            "work_unit": dict(self.work_unit),
            "source": self.source,
            "rank": self.rank,
            "draft_spec": self.draft_spec,
            "notes_history": list(self.notes_history),
            "superseded_by": self.superseded_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class MilestoneStore:
    """Store milestones in developer.db via KnowledgeStore."""

    def __init__(self, knowledge: KnowledgeStore):
        self.knowledge = knowledge

    def get(self, milestone_id: str) -> Optional[Milestone]:
        entry = self.knowledge.get(milestone_id)
        if entry is None or "milestone" not in (entry.tags or []):
            return None
        return _milestone_from_entry(entry)

    def list(
        self,
        *,
        target: str = "",
        status: str = "",
        include_archived: bool = False,
    ) -> list[Milestone]:
        if status and status not in ALL_MILESTONE_STATUSES:
            raise MilestoneError(f"status must be one of: {sorted(ALL_MILESTONE_STATUSES)}")

        entries = self.knowledge.get_by_tier(Tier.DERIVED)
        milestones: list[Milestone] = []
        for entry in entries:
            if "milestone" not in (entry.tags or []):
                continue
            milestone = _milestone_from_entry(entry)
            if target and milestone.target != target:
                continue
            if status and milestone.status != status:
                continue
            if not include_archived and milestone.status == MILESTONE_STATUS_ARCHIVED:
                continue
            milestones.append(milestone)

        milestones.sort(key=lambda ms: (ms.status != MILESTONE_STATUS_IN_PROGRESS, -ms.updated_at))
        return milestones

    def propose_from_work_unit(
        self,
        work_unit: dict[str, Any],
        *,
        target: str = "",
        title: str = "",
        summary: str = "",
        source: str = "work_unit",
    ) -> Milestone:
        """Create or update a proposed milestone from a ranked work unit."""
        if not isinstance(work_unit, dict) or not work_unit:
            raise MilestoneError("work_unit is required")

        frs = work_unit.get("frs") or []
        if not isinstance(frs, list) or not frs:
            raise MilestoneError("work_unit must include a non-empty frs list")

        fr_ids = [_fr_id_from_item(fr) for fr in frs]
        if any(not fr_id for fr_id in fr_ids):
            raise MilestoneError("every work_unit fr must include a non-empty fr_id")

        inferred_target = target or _infer_target(work_unit)
        if not inferred_target:
            raise MilestoneError(
                "target is required when work_unit has missing or ambiguous targets"
            )

        milestone_title = title.strip() or str(work_unit.get("name") or "FR work unit").strip()
        milestone_summary = summary.strip() or _summarize_work_unit(work_unit)
        rank = int(work_unit.get("rank") or 0)
        milestone_id = _derive_milestone_id(inferred_target, milestone_title, fr_ids)
        now = time.time()

        existing = self.get(milestone_id)
        created_at = existing.created_at if existing else now
        status = existing.status if existing else MILESTONE_STATUS_PROPOSED
        # Preserve existing history + supersede pointer on re-propose;
        # seed a ``proposed`` entry on first creation so every milestone
        # has at least one audit row.
        if existing is not None:
            notes_history = list(existing.notes_history)
            superseded_by = existing.superseded_by
        else:
            notes_history = [{
                "at": now,
                "status": MILESTONE_STATUS_PROPOSED,
                "notes": "proposed",
            }]
            superseded_by = ""
        milestone = Milestone(
            id=milestone_id,
            title=milestone_title,
            target=inferred_target,
            status=status,
            summary=milestone_summary,
            fr_ids=fr_ids,
            work_unit=work_unit,
            source=source,
            rank=rank,
            draft_spec=_draft_spec(
                milestone_title,
                inferred_target,
                milestone_summary,
                work_unit,
                status=status,
            ),
            notes_history=notes_history,
            superseded_by=superseded_by,
            created_at=created_at,
            updated_at=now,
        )
        self._store(milestone)
        return milestone

    def review_scope(
        self,
        milestone_id: str,
        *,
        review_terms: list[str] | None = None,
    ) -> dict[str, Any]:
        """Return deterministic scope warnings before implementation starts."""
        milestone = self.get(milestone_id)
        if milestone is None:
            raise MilestoneError(f"unknown milestone id: {milestone_id}")

        frs = [
            fr
            for fr in (_review_fr_item(fr) for fr in (milestone.work_unit.get("frs") or []))
            if fr["fr_id"] or fr["description"]
        ]
        duplicate_groups = _duplicate_fr_groups(frs)
        terms = ["AutoGen", "GRA"] if review_terms is None else review_terms
        term_hits = _term_hits(frs, terms)
        issue_count = len(duplicate_groups) + len(term_hits)
        return {
            "milestone_id": milestone.id,
            "title": milestone.title,
            "status": milestone.status,
            "fr_count": len(milestone.fr_ids),
            "duplicate_groups": duplicate_groups,
            "review_term_hits": term_hits,
            "recommendation": "refine_before_implementation" if issue_count else "ready_for_spec",
        }

    # ------------------------------------------------------------------
    # Lifecycle mutations (fr_developer_91a5a072)
    # ------------------------------------------------------------------

    def update_status(
        self,
        milestone_id: str,
        status: str,
        *,
        notes: str = "",
        force: bool = False,
    ) -> Milestone:
        """Transition a milestone among lifecycle statuses.

        Default (``force=False``) enforces the forward-only transition
        graph in :data:`ALLOWED_MILESTONE_TRANSITIONS`. Passing
        ``force=True`` allows rollback out of a terminal state (the
        audit entry records the force so the history makes it obvious).

        The idempotent case (``status`` already matches) still appends
        an audit note when ``notes`` is provided, so "confirmed in-
        progress after crash recovery" leaves a trail.
        """
        if status not in ALL_MILESTONE_STATUSES:
            raise MilestoneError(
                f"status must be one of {sorted(ALL_MILESTONE_STATUSES)}, got {status!r}"
            )
        milestone = self.get(milestone_id)
        if milestone is None:
            raise MilestoneError(f"unknown milestone id: {milestone_id}")

        now = time.time()

        if milestone.status == status:
            if notes:
                milestone.notes_history.append({
                    "at": now, "status": status, "notes": notes,
                })
                milestone.updated_at = now
                self._store(milestone)
            return milestone

        allowed = ALLOWED_MILESTONE_TRANSITIONS.get(milestone.status, set())
        forced = False
        if status not in allowed:
            if not force:
                raise MilestoneError(
                    f"illegal transition {milestone.status!r} -> {status!r} "
                    f"for {milestone_id}. Allowed from {milestone.status!r}: "
                    f"{sorted(allowed)}. Pass force=True to override "
                    "(rollback from terminal state)."
                )
            forced = True

        entry = {
            "at": now,
            "status": status,
            "notes": notes,
            "from_status": milestone.status,
        }
        if forced:
            entry["force_rollback"] = True
        milestone.notes_history.append(entry)
        milestone.status = status
        milestone.updated_at = now
        self._store(milestone)
        return milestone

    def supersede(
        self,
        superseded_id: str,
        superseded_by_id: str,
        *,
        rationale: str = "",
    ) -> Milestone:
        """Mark ``superseded_id`` as superseded by ``superseded_by_id``.

        Writes a ``superseded_by`` back-pointer on the superseded
        milestone and sets its status to ``superseded``. The new
        milestone is unaffected — this intentionally does NOT cascade
        to FR state (FRs may legitimately span multiple milestones).

        Returns the updated superseded milestone.
        """
        superseded_id = (superseded_id or "").strip()
        superseded_by_id = (superseded_by_id or "").strip()
        if not superseded_id or not superseded_by_id:
            raise MilestoneError(
                "supersede requires both superseded_id and superseded_by_id"
            )
        if superseded_id == superseded_by_id:
            raise MilestoneError(
                "a milestone cannot supersede itself"
            )
        superseded = self.get(superseded_id)
        if superseded is None:
            raise MilestoneError(f"unknown milestone id: {superseded_id}")
        superseder = self.get(superseded_by_id)
        if superseder is None:
            raise MilestoneError(
                f"unknown superseder milestone id: {superseded_by_id}"
            )

        now = time.time()
        note_text = rationale.strip() or f"superseded by {superseded_by_id}"
        superseded.status = MILESTONE_STATUS_SUPERSEDED
        superseded.superseded_by = superseded_by_id
        superseded.notes_history.append({
            "at": now,
            "status": MILESTONE_STATUS_SUPERSEDED,
            "notes": note_text,
            "superseded_by": superseded_by_id,
        })
        superseded.updated_at = now
        self._store(superseded)
        return superseded

    def update_frs(
        self,
        milestone_id: str,
        *,
        add_fr_ids: Optional[Iterable[str]] = None,
        remove_fr_ids: Optional[Iterable[str]] = None,
        notes: str = "",
    ) -> Milestone:
        """Mutate the FR bundle on a ``proposed`` milestone.

        Refuses on any status other than ``proposed`` — once work has
        started (or the milestone is terminal) the FR set should be
        considered fixed for audit purposes. Removing an already-absent
        id is a no-op (not an error); adding an already-present id is
        likewise a no-op. Order within ``fr_ids`` is preserved for
        surviving entries; new entries land at the end in the order
        given.
        """
        milestone = self.get(milestone_id)
        if milestone is None:
            raise MilestoneError(f"unknown milestone id: {milestone_id}")
        if milestone.status != MILESTONE_STATUS_PROPOSED:
            raise MilestoneError(
                f"cannot update_frs on milestone {milestone_id!r}: "
                f"status is {milestone.status!r} (only 'proposed' is mutable; "
                "use supersede_milestone to replace a milestone that's "
                "already moved on)"
            )

        adds = [str(x).strip() for x in (add_fr_ids or []) if str(x).strip()]
        removes = {str(x).strip() for x in (remove_fr_ids or []) if str(x).strip()}

        new_ids = [fid for fid in milestone.fr_ids if fid not in removes]
        added: list[str] = []
        for fid in adds:
            if fid in new_ids:
                continue
            new_ids.append(fid)
            added.append(fid)
        removed = [fid for fid in milestone.fr_ids if fid in removes]

        if not added and not removed and not notes:
            # Nothing to persist; return unchanged.
            return milestone

        now = time.time()
        milestone.fr_ids = new_ids
        audit = {
            "at": now,
            "status": milestone.status,
            "notes": notes or "fr bundle updated",
            "added_fr_ids": list(added),
            "removed_fr_ids": list(removed),
        }
        milestone.notes_history.append(audit)
        milestone.updated_at = now
        self._store(milestone)
        return milestone

    def delete(
        self,
        milestone_id: str,
        *,
        reason: str = "",
        fr_store: Any = None,
    ) -> dict[str, Any]:
        """Hard-delete a milestone.

        Refuses if either:

        * any FR in the milestone's ``fr_ids`` currently has status
          ``in_progress`` (caller is expected to move those FRs off
          the milestone or close them first), OR
        * the milestone has any non-seed ``notes_history`` entries
          (the seed is the single ``proposed`` row written at
          creation time; anything beyond that means real mutations
          have been recorded, and dropping the milestone would lose
          that audit trail — use :meth:`supersede` instead).

        ``fr_store`` is an optional :class:`developer.fr_store.FRStore`.
        When not supplied, the in-progress check is skipped with a
        ``skipped_fr_check`` marker in the returned payload — this is
        the documented fallback for callers that don't have an FR store
        wired in (tests, offline tools). The primary agent wiring is
        expected to always supply it.
        """
        milestone = self.get(milestone_id)
        if milestone is None:
            raise MilestoneError(f"unknown milestone id: {milestone_id}")

        if _has_non_seed_history(milestone.notes_history):
            raise MilestoneError(
                f"cannot delete milestone {milestone_id!r}: notes_history has "
                f"{len(milestone.notes_history)} entries beyond the initial "
                "seed (mutations recorded). Use supersede_milestone to "
                "preserve the audit trail."
            )

        fr_check_skipped = False
        blocking_frs: list[str] = []
        if fr_store is not None and milestone.fr_ids:
            for fr_id in milestone.fr_ids:
                try:
                    fr = fr_store.get(fr_id)
                except Exception:
                    # FR store raised on a specific id — skip, don't block
                    # the whole delete on a store-side glitch.
                    continue
                if fr is None:
                    continue
                # Access the same status field surface the FR dataclass
                # exposes; keep this loose so the module doesn't hard
                # import fr_store for typing.
                status = getattr(fr, "status", "")
                if status == "in_progress":
                    blocking_frs.append(fr.id)
            if blocking_frs:
                raise MilestoneError(
                    f"cannot delete milestone {milestone_id!r}: FR(s) in "
                    f"progress reference this milestone's bundle: "
                    f"{blocking_frs}. Move those FRs to another milestone "
                    "or complete them first."
                )
        elif fr_store is None and milestone.fr_ids:
            fr_check_skipped = True

        removed = self.knowledge.remove(milestone_id)
        return {
            "milestone_id": milestone_id,
            "removed": bool(removed),
            "reason": reason,
            "skipped_fr_check": fr_check_skipped,
        }

    def _store(self, milestone: Milestone) -> None:
        entry = KnowledgeEntry(
            id=milestone.id,
            tier=Tier.DERIVED,
            title=milestone.title,
            content=milestone.summary,
            source="developer.milestone_store",
            scope="development",
            confidence=1.0,
            status=EntryStatus.DISTILLED,
            tags=["milestone", f"target:{milestone.target}", f"status:{milestone.status}"],
            metadata={
                "milestone_status": milestone.status,
                "target": milestone.target,
                "fr_ids": list(milestone.fr_ids),
                "work_unit": dict(milestone.work_unit),
                "source": milestone.source,
                "rank": milestone.rank,
                "draft_spec": milestone.draft_spec,
                "notes_history": list(milestone.notes_history),
                "superseded_by": milestone.superseded_by,
            },
            created_at=milestone.created_at,
            updated_at=milestone.updated_at,
        )
        self.knowledge.add(entry)
        stored = self.knowledge.get(milestone.id)
        if stored is not None:
            milestone.created_at = stored.created_at
            milestone.updated_at = stored.updated_at


def _milestone_from_entry(entry: KnowledgeEntry) -> Milestone:
    meta = entry.metadata or {}
    # ``notes_history`` and ``superseded_by`` were added after the first
    # batch of milestones was persisted; default them to empty on read so
    # older records load without a migration pass.
    return Milestone(
        id=entry.id,
        title=entry.title,
        target=meta.get("target", ""),
        status=meta.get("milestone_status", MILESTONE_STATUS_PROPOSED),
        summary=entry.content,
        fr_ids=list(meta.get("fr_ids") or []),
        work_unit=dict(meta.get("work_unit") or {}),
        source=meta.get("source", "work_unit"),
        rank=int(meta.get("rank") or 0),
        draft_spec=meta.get("draft_spec", ""),
        notes_history=list(meta.get("notes_history") or []),
        superseded_by=meta.get("superseded_by", "") or "",
        created_at=entry.created_at,
        updated_at=entry.updated_at,
    )


def _derive_milestone_id(target: str, title: str, fr_ids: list[str]) -> str:
    payload = f"{target}:{title}:{','.join(sorted(fr_ids))}".encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()[:8]
    return f"ms_{target}_{digest}"


def _fr_id_from_item(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("fr_id") or item.get("id") or "").strip()
    return str(item).strip()


def _review_fr_item(item: Any) -> dict[str, str]:
    if isinstance(item, dict):
        fr_id = _fr_id_from_item(item)
        description = str(item.get("description") or item.get("title") or fr_id).strip()
        priority = str(item.get("priority") or "").strip()
        return {"fr_id": fr_id, "description": description, "priority": priority}

    fr_id = _fr_id_from_item(item)
    return {"fr_id": fr_id, "description": fr_id, "priority": ""}


def _infer_target(work_unit: dict[str, Any]) -> str:
    targets = work_unit.get("targets") or []
    if isinstance(targets, list) and len(targets) == 1:
        return str(targets[0]).strip()
    return ""


def _summarize_work_unit(work_unit: dict[str, Any]) -> str:
    name = str(work_unit.get("name") or "FR work unit").strip()
    size = work_unit.get("size") or len(work_unit.get("frs") or [])
    max_priority = work_unit.get("max_priority") or "unknown"
    return f"{name}: {size} FRs, max priority {max_priority}."


def _draft_spec(
    title: str,
    target: str,
    summary: str,
    work_unit: dict[str, Any],
    *,
    status: str = MILESTONE_STATUS_PROPOSED,
) -> str:
    lines = [
        f"# {title}",
        "",
        f"**Target:** `{target}`",
        f"**Status:** {status}",
        "",
        "## Summary",
        "",
        summary,
        "",
        "## Feature Requests",
        "",
    ]
    for fr in work_unit.get("frs") or []:
        if isinstance(fr, dict):
            fr_id = _fr_id_from_item(fr)
            lines.append(f"- `{fr_id}` {_fr_description_with_priority(fr)}".rstrip())
        else:
            lines.append(f"- `{str(fr).strip()}`")
    lines.extend([
        "",
        "## Acceptance Criteria",
        "",
        "- Milestone scope is explicit and bounded to the listed FRs.",
        "- Implementation work can be tracked against this milestone id.",
        "- A follow-up spec can refine design decisions before coding starts.",
    ])
    return "\n".join(lines)


def _fr_description_with_priority(fr: dict[str, Any]) -> str:
    description = str(fr.get("description") or fr.get("title") or "").strip()
    priority = str(fr.get("priority") or "").strip()
    if priority and not description.endswith(f"[{priority}]"):
        description = f"{description} [{priority}]".strip()
    return description


def _duplicate_fr_groups(frs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, str]]] = {}
    for fr in frs:
        key = _normalized_fr_description(fr)
        if not key:
            continue
        groups.setdefault(key, []).append({
            "fr_id": _fr_id_from_item(fr),
            "description": str(fr.get("description") or fr.get("title") or "").strip(),
        })
    return [
        {"normalized_description": key, "frs": values}
        for key, values in sorted(groups.items())
        if len(values) > 1
    ]


def _term_hits(frs: list[dict[str, Any]], terms: list[str]) -> list[dict[str, Any]]:
    hits = []
    for term in terms:
        clean = str(term).strip()
        if not clean:
            continue
        matched = []
        needle = clean.lower()
        for fr in frs:
            description = str(fr.get("description") or fr.get("title") or "")
            if needle in description.lower():
                matched.append({
                    "fr_id": _fr_id_from_item(fr),
                    "description": description.strip(),
                })
        if matched:
            hits.append({"term": clean, "frs": matched})
    return hits


def _has_non_seed_history(history: list[dict]) -> bool:
    """True if ``history`` contains any entry beyond the creation seed.

    The seed is the single ``proposed`` row written by
    :meth:`MilestoneStore.propose_from_work_unit`. For backward
    compatibility with milestones that predated the notes_history
    field entirely, an empty history is also treated as "no
    non-seed entries" (nothing to lose).
    """
    if not history:
        return False
    if len(history) > 1:
        return True
    entry = history[0] if history else {}
    # A legitimate seed row: status=proposed, notes is the canonical
    # "proposed" marker. Anything else is a real mutation that
    # predates this implementation (e.g. a prior update_status pass).
    return not (
        entry.get("status") == MILESTONE_STATUS_PROPOSED
        and str(entry.get("notes", "")).strip() == "proposed"
    )


def _normalized_fr_description(fr: dict[str, Any]) -> str:
    text = str(fr.get("description") or fr.get("title") or "").lower()
    text = re.sub(r"\s*(?:->|→)\s*[\w-]+\s*", " ", text)
    text = re.sub(r"\[(?:high|medium|low)\]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()
