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

from developer.project_store import DEFAULT_PROJECT

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
#
# On write, :meth:`MilestoneStore.update_status` normalizes incoming
# ``archived`` to ``abandoned`` (with a ``normalized_from`` audit marker)
# so there is exactly one terminal-abandon status going forward.
# :meth:`MilestoneStore.list` still filters both values out when
# ``include_archived=False`` so legacy on-disk ``archived`` rows stay
# hidden by default. PR #43 Copilot R6 finding 4.
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

# Allowed forward transitions. The lifecycle is monotonic-forward:
# proposed → planned → in_progress → (completed | abandoned). Terminal
# states have no outgoing edges.
#
# ``superseded`` is deliberately NOT listed as a reachable target here
# — it's reachable only via :meth:`MilestoneStore.supersede` (which
# takes the required ``superseded_by`` back-pointer), and
# :meth:`MilestoneStore.update_status` rejects ``status="superseded"``
# before reaching this table. Listing it as a target would make the
# error messages / inline docs lie about reachability.
#
# ``archived`` is the legacy synonym for ``abandoned`` and is likewise
# NOT listed as a target: incoming ``status="archived"`` is normalized
# to ``abandoned`` in :meth:`update_status` BEFORE the transition check
# runs, so the archived value is never compared against this table. On
# read, a milestone whose stored status is ``archived`` is treated as
# ``abandoned`` during the current-state lookup (see ``update_status``
# below), so legacy on-disk rows still behave correctly. PR #43 Copilot
# R7 finding 1.
#
# Forward shortcuts (allowed without force):
#   * proposed → in_progress — skip the ``planned`` step when the caller
#     has already decided to start work immediately. Kept because forcing
#     callers through ``planned`` first is ceremony without signal; the
#     ``proposed → in_progress`` path is a common lightweight flow.
#
# Sideways jumps to the terminal ``abandoned`` state from any active
# status are allowed without force since they do not reopen work.
#
# ANY edge not in this table — backward rollbacks (completed →
# in_progress, in_progress → planned, planned → proposed) AND forward
# jumps that skip intermediate states (planned → completed) — requires
# the caller to pass ``force=True`` to
# :meth:`MilestoneStore.update_status` (and is recorded in
# ``notes_history`` with a ``force_override=True`` marker — "override"
# rather than "rollback" since not every forced transition is backward).
ALLOWED_MILESTONE_TRANSITIONS: dict[str, set[str]] = {
    MILESTONE_STATUS_PROPOSED: {
        MILESTONE_STATUS_PLANNED,
        MILESTONE_STATUS_IN_PROGRESS,
        MILESTONE_STATUS_ABANDONED,
    },
    MILESTONE_STATUS_PLANNED: {
        MILESTONE_STATUS_IN_PROGRESS,
        MILESTONE_STATUS_ABANDONED,
    },
    MILESTONE_STATUS_IN_PROGRESS: {
        MILESTONE_STATUS_COMPLETED,
        MILESTONE_STATUS_ABANDONED,
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
    # Phase 3 of fr_developer_5d0a8711: project as a first-class dimension.
    # Read-time fallback to DEFAULT_PROJECT keeps pre-existing records
    # valid; migrate_records_to_project() canonicalizes the metadata.
    project: str = DEFAULT_PROJECT

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "target": self.target,
            "status": self.status,
            "summary": self.summary,
            "project": self.project,
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
        project: Optional[str] = None,
    ) -> list[Milestone]:
        if status and status not in ALL_MILESTONE_STATUSES:
            raise MilestoneError(f"status must be one of: {sorted(ALL_MILESTONE_STATUSES)}")

        # Normalize the project filter up front. `None` = all projects;
        # any string (including "" or whitespace) maps to a concrete
        # slug, so bus/CLI defaults that send "" filter for the default
        # project rather than silently disabling the filter.
        normalized_project: Optional[str]
        if project is None:
            normalized_project = None
        else:
            normalized_project = project.strip() or DEFAULT_PROJECT

        # Mirror ``update_status``' write-time normalization on the read
        # path: ``archived`` is the legacy synonym for ``abandoned``, so
        # a ``status=abandoned`` filter must also surface legacy on-disk
        # rows stored as ``archived`` (otherwise callers see an empty
        # list even though the rows exist). A ``status=archived`` filter
        # is treated symmetrically — match both canonical and legacy
        # values — so the two filter inputs are interchangeable.
        # PR #43 Copilot R8 finding 3.
        status_match: set[str] = set()
        if status:
            if status in (MILESTONE_STATUS_ABANDONED, MILESTONE_STATUS_ARCHIVED):
                status_match = {
                    MILESTONE_STATUS_ABANDONED,
                    MILESTONE_STATUS_ARCHIVED,
                }
            else:
                status_match = {status}

        entries = self.knowledge.get_by_tier(Tier.DERIVED)
        milestones: list[Milestone] = []
        for entry in entries:
            if "milestone" not in (entry.tags or []):
                continue
            milestone = _milestone_from_entry(entry)
            if target and milestone.target != target:
                continue
            if normalized_project is not None and milestone.project != normalized_project:
                continue
            if status_match and milestone.status not in status_match:
                continue
            # ``archived`` is the legacy synonym for ``abandoned`` (see the
            # module-level status notes). Treat them as one terminal
            # category for filtering purposes so legacy on-disk rows and
            # freshly-abandoned milestones behave the same way when
            # ``include_archived=False``. PR #43 Copilot R6 finding 4.
            #
            # When the caller explicitly filters for abandoned/archived,
            # honor that over ``include_archived=False`` — otherwise the
            # filter and the default flag would contradict each other and
            # return an empty list. PR #43 Copilot R8 finding 3.
            explicitly_requested_terminal = status in (
                MILESTONE_STATUS_ABANDONED,
                MILESTONE_STATUS_ARCHIVED,
            )
            if (
                not include_archived
                and not explicitly_requested_terminal
                and milestone.status in (
                    MILESTONE_STATUS_ARCHIVED,
                    MILESTONE_STATUS_ABANDONED,
                )
            ):
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
        project: Optional[str] = None,
    ) -> Milestone:
        """Create or update a proposed milestone from a ranked work unit.

        ``project`` partitions the milestone into a project slug (Phase 3
        of fr_developer_5d0a8711). Semantics:

        - ``None`` (default) means "preserve existing": on re-propose,
          the stored milestone's project is retained; on first creation,
          :data:`DEFAULT_PROJECT` is used.
        - ``""`` normalizes to :data:`DEFAULT_PROJECT`.
        - An explicit slug (including :data:`DEFAULT_PROJECT` itself)
          overrides any previously-stored project — that's how callers
          deliberately move a milestone back to the default project,
          which a plain-string default couldn't express.
        """
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

        # `project=None` means "preserve existing if any"; `""` and any
        # other value normalize to a concrete slug. The preserve branch
        # runs below once we know whether there's an existing milestone.
        preserve_existing_project = project is None
        if not preserve_existing_project:
            project = project.strip() or DEFAULT_PROJECT

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
            if preserve_existing_project:
                project = existing.project or DEFAULT_PROJECT
        else:
            notes_history = [{
                "at": now,
                "status": MILESTONE_STATUS_PROPOSED,
                "notes": "proposed",
            }]
            superseded_by = ""
            if preserve_existing_project:
                project = DEFAULT_PROJECT
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
            project=project,
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

        Default (``force=False``) enforces the monotonic-forward
        transition graph in :data:`ALLOWED_MILESTONE_TRANSITIONS`:
        proposed → planned → in_progress → terminal. One forward
        shortcut is allowed without force: ``proposed → in_progress``
        (skipping ``planned``) for the common "decided to start
        immediately" flow. Sideways jumps to the terminal ``abandoned``
        state from any active status are also allowed without force.

        ``status="superseded"`` is rejected by this skill — use
        :meth:`supersede` (which takes the required ``superseded_by``
        back-pointer). ``status="archived"`` is normalized to
        ``abandoned`` on write with a ``normalized_from`` audit marker;
        see module-level notes on the archived/abandoned synonym.

        Any edge not in the forward table (backward rollbacks like
        completed → in_progress, in_progress → planned, planned →
        proposed, as well as forward jumps like planned → completed
        that skip in_progress) requires ``force=True``; the audit
        entry records the force via ``force_override=True`` so the
        history makes it obvious. The name is "override" rather than
        "rollback" because not every forced transition is a rollback
        — some are forward shortcuts.

        The idempotent case (``status`` already matches) still appends
        an audit note when ``notes`` is provided, so "confirmed in-
        progress after crash recovery" leaves a trail.
        """
        if status not in ALL_MILESTONE_STATUSES:
            raise MilestoneError(
                f"status must be one of {sorted(ALL_MILESTONE_STATUSES)}, got {status!r}"
            )
        if status == MILESTONE_STATUS_SUPERSEDED:
            # update_status has no ``superseded_by`` parameter, so letting
            # it set ``superseded`` would always produce an invalid
            # milestone (status=superseded, superseded_by=""). Route the
            # caller to the dedicated skill that takes the back-pointer.
            raise MilestoneError(
                "use supersede_milestone(superseded_id, superseded_by_id, "
                "rationale=...) to set superseded status — update_status "
                "cannot supply the required superseded_by pointer"
            )
        # ``archived`` is the legacy synonym for ``abandoned``. Normalize
        # incoming writes so there is exactly one terminal-abandon value
        # going forward; the audit row records the pre-normalization
        # request so the history still reflects what the caller asked
        # for. PR #43 Copilot R6 finding 4.
        normalized_from: Optional[str] = None
        if status == MILESTONE_STATUS_ARCHIVED:
            normalized_from = MILESTONE_STATUS_ARCHIVED
            status = MILESTONE_STATUS_ABANDONED

        milestone = self.get(milestone_id)
        if milestone is None:
            raise MilestoneError(f"unknown milestone id: {milestone_id}")

        # Legacy on-disk rows may carry ``status='archived'`` (the
        # historical synonym for ``abandoned``). Treat them as
        # ``abandoned`` for the current-state comparison and the
        # transition-table lookup so the same update_status call that
        # works on a fresh ``abandoned`` milestone also works on a
        # legacy ``archived`` one. No write-back — the stored row stays
        # intact; this is a read-time alias only. PR #43 Copilot R7
        # finding 2 (Option A).
        current_status = milestone.status
        if current_status == MILESTONE_STATUS_ARCHIVED:
            current_status = MILESTONE_STATUS_ABANDONED

        now = time.time()

        if current_status == status:
            if notes or normalized_from:
                entry: dict[str, Any] = {
                    "at": now, "status": status, "notes": notes,
                }
                if normalized_from:
                    entry["normalized_from"] = normalized_from
                milestone.notes_history.append(entry)
                milestone.updated_at = now
                self._refresh_draft_spec(milestone)
                self._store(milestone)
            return milestone

        allowed = ALLOWED_MILESTONE_TRANSITIONS.get(current_status, set())
        forced = False
        if status not in allowed:
            if not force:
                raise MilestoneError(
                    f"illegal transition {current_status!r} -> {status!r} "
                    f"for {milestone_id}. Allowed from {current_status!r}: "
                    f"{sorted(allowed)}. Pass force=True to override "
                    "(transition not in the forward graph)."
                )
            forced = True

        entry = {
            "at": now,
            "status": status,
            "notes": notes,
            "from_status": current_status,
        }
        if forced:
            entry["force_override"] = True
        if normalized_from:
            entry["normalized_from"] = normalized_from
        milestone.notes_history.append(entry)
        # Force-rolling OUT of ``superseded`` must clear the stale
        # back-pointer — otherwise the milestone ends up in (e.g.)
        # ``in_progress`` status while still carrying a superseded_by id
        # that references an unrelated replacement. The only supported
        # way to re-enter ``superseded`` is ``supersede_milestone``,
        # which re-sets the pointer explicitly.
        if (
            milestone.status == MILESTONE_STATUS_SUPERSEDED
            and status != MILESTONE_STATUS_SUPERSEDED
        ):
            milestone.superseded_by = ""
        milestone.status = status
        milestone.updated_at = now
        # Recompute the cached ``draft_spec`` markdown so the embedded
        # ``**Status:** ...`` line matches the new status. Callers of
        # ``draft_spec_from_milestone`` otherwise see a stale line after
        # every transition. PR #43 Copilot R6 finding 1.
        self._refresh_draft_spec(milestone)
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
        # Refresh the cached draft_spec so the ``**Status:**`` line
        # matches the new superseded status. PR #43 Copilot R6 finding 1.
        self._refresh_draft_spec(superseded)
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

        ``add_fr_ids`` / ``remove_fr_ids`` accept either an iterable of
        ids or a single bare ``str`` id. The bare-``str`` case is
        normalized to a single-element list here so a caller passing
        e.g. ``add_fr_ids="fr_developer_foo"`` doesn't get iterated
        character-by-character — classic Python footgun.
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

        adds_iter = _normalize_fr_ids(add_fr_ids)
        removes_iter = _normalize_fr_ids(remove_fr_ids)
        adds = [str(x).strip() for x in adds_iter if str(x).strip()]
        removes = {str(x).strip() for x in removes_iter if str(x).strip()}

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
        # ``work_unit["frs"]`` is the source of truth for
        # :meth:`review_scope` and :func:`_draft_spec`; keeping it in
        # sync with ``fr_ids`` avoids stale bundles leaking into scope
        # review, duplicate detection, and the rendered draft_spec.
        # Preserve descriptions/priorities for surviving entries and
        # drop removed ones; newly-added entries get a minimal dict with
        # the id alone so downstream helpers don't crash on missing
        # keys. PR #43 Copilot R6 finding 2.
        _sync_work_unit_frs(milestone.work_unit, new_ids)
        audit = {
            "at": now,
            "status": milestone.status,
            "notes": notes or "fr bundle updated",
            "added_fr_ids": list(added),
            "removed_fr_ids": list(removed),
        }
        milestone.notes_history.append(audit)
        milestone.updated_at = now
        # Refresh the cached draft_spec so its "## Feature Requests"
        # bullet list matches the new bundle. PR #43 Copilot R6
        # finding 2.
        self._refresh_draft_spec(milestone)
        self._store(milestone)
        return milestone

    def delete(
        self,
        milestone_id: str,
        *,
        fr_store: Any,
        reason: str = "",
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

        Recovery tree when delete refuses:

        * Non-seed history present → use :meth:`supersede` (which works
          regardless of status) to retire the milestone while keeping
          the audit trail intact.
        * FR lookup raised on a ``proposed`` milestone → call
          :meth:`update_frs` to drop the unreachable FR id(s) from the
          bundle, then retry delete. ``update_frs`` only works on
          ``proposed`` milestones.
        * FR lookup raised on a non-``proposed`` milestone → use
          :meth:`supersede` instead; the bundle is considered fixed
          for audit once work has started.
        * FR(s) genuinely in_progress → move them to a different
          milestone or complete them first, then retry delete.

        ``fr_store`` is a required :class:`developer.fr_store.FRStore`
        (required keyword-only; no default). Making it mandatory kills
        the silent-bypass footgun that arose when callers — especially
        REPL / direct-pipeline users — forgot to wire it: the
        in-progress guard would be skipped with only a
        ``skipped_fr_check`` marker, invalidating the safety promise.
        Callers that legitimately cannot supply one (e.g. the bundle is
        known-empty) must still pass the store; the method short-circuits
        the FR check when ``milestone.fr_ids`` is empty. PR #43 Copilot
        R5 finding 1.
        """
        if fr_store is None:
            # Explicit None is also rejected. The whole point of making
            # fr_store required is to kill the silent-bypass footgun;
            # accepting None here would reintroduce it through the back
            # door. Callers that genuinely have no FR store wired (tests,
            # REPL) must construct one — there is no skip-the-check
            # escape hatch. PR #43 Copilot R5 finding 1.
            raise TypeError(
                "MilestoneStore.delete() requires fr_store (got None). "
                "Pass the pipeline's FRStore so the in-progress guard "
                "can run — there is no opt-in to bypass the check."
            )

        milestone = self.get(milestone_id)
        if milestone is None:
            raise MilestoneError(f"unknown milestone id: {milestone_id}")

        if _has_non_seed_history(milestone.notes_history):
            raise MilestoneError(
                f"cannot delete milestone {milestone_id!r}: notes_history has "
                f"{len(milestone.notes_history)} entries recorded (including "
                "any creation seed; mutations beyond the seed are present). "
                "Use supersede_milestone to preserve the audit trail."
            )

        # Blocking entries track BOTH the original bundled fr_id and the
        # resolved fr.id post-redirect, so the refusal message lets the
        # caller correlate the blocker back to ``milestone.fr_ids``.
        # Without the original id, a merge redirect (bundle carries
        # ``fr_x`` → fr_store returns fr with ``id=fr_y``) produces an
        # error message referencing ``fr_y``, which the caller cannot
        # find in ``milestone.fr_ids`` at all. PR #43 Copilot R8 finding 1.
        blocking_frs: list[tuple[str, str]] = []
        if milestone.fr_ids:
            for fr_id in milestone.fr_ids:
                try:
                    fr = fr_store.get(fr_id)
                except Exception as exc:
                    # FR store raised — we cannot safely verify whether
                    # this FR is in_progress, and silently continuing
                    # would bypass the safety guard (e.g. on a redirect
                    # cycle or DB issue). Refuse the delete and surface
                    # the failure. The recovery path depends on the
                    # milestone's status, since ``update_frs`` only
                    # accepts ``proposed`` milestones: proposed →
                    # clear the offending id via update_milestone_frs
                    # and retry; non-proposed → use supersede_milestone
                    # instead (which does not require FR verification
                    # and preserves the audit trail).
                    if milestone.status == MILESTONE_STATUS_PROPOSED:
                        recovery = (
                            "Clear the FR bundle via update_milestone_frs "
                            "first if the FR is unreachable by design, "
                            "then retry delete."
                        )
                    else:
                        recovery = (
                            f"Milestone status is {milestone.status!r}; "
                            "use supersede_milestone instead "
                            "(update_milestone_frs only accepts "
                            "'proposed' milestones)."
                        )
                    raise MilestoneError(
                        f"cannot delete milestone {milestone_id!r}: "
                        f"failed to verify FR state for {fr_id!r} "
                        f"({type(exc).__name__}: {exc}). {recovery}"
                    ) from exc
                if fr is None:
                    continue
                # Access the same status field surface the FR dataclass
                # exposes; keep this loose so the module doesn't hard
                # import fr_store for typing.
                status = getattr(fr, "status", "")
                if status == "in_progress":
                    blocking_frs.append((fr_id, getattr(fr, "id", fr_id)))
            if blocking_frs:
                blocker_details = ", ".join(
                    f"{original!r} (resolved to {resolved!r})"
                    if original != resolved
                    else f"{original!r}"
                    for original, resolved in blocking_frs
                )
                raise MilestoneError(
                    f"cannot delete milestone {milestone_id!r}: FR(s) in "
                    f"progress reference this milestone's bundle: "
                    f"{blocker_details}. Move those FRs to another milestone "
                    "or complete them first."
                )

        removed = self.knowledge.remove(milestone_id)
        return {
            "milestone_id": milestone_id,
            "removed": bool(removed),
            "reason": reason,
        }

    def _refresh_draft_spec(self, milestone: Milestone) -> None:
        """Recompute ``milestone.draft_spec`` from current milestone state.

        The cached ``draft_spec`` markdown embeds ``**Status:** ...`` and
        a bullet list of the FR bundle; any mutation that changes
        ``status``, ``title``, ``summary``, ``target``, or the FR bundle
        must recompute it in lock-step or ``draft_spec_from_milestone``
        callers see stale content. Centralized here so every mutation
        path (update_status, supersede, update_frs) calls the same
        recompute. PR #43 Copilot R6 findings 1 + 2.
        """
        milestone.draft_spec = _draft_spec(
            milestone.title,
            milestone.target,
            milestone.summary,
            milestone.work_unit,
            status=milestone.status,
        )

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
                "project": milestone.project or DEFAULT_PROJECT,
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

    # ------------------------------------------------------------------
    # fr_developer_1c5178d2 — project-dimension migration helper
    # ------------------------------------------------------------------

    def migrate_records_to_project(
        self, project: str = DEFAULT_PROJECT
    ) -> int:
        """Stamp ``project`` onto milestone records whose metadata lacks it.

        In-place metadata patch via :func:`dataclasses.replace` — see
        :meth:`FRStore.migrate_records_to_project` for the rationale
        around not round-tripping through the dataclass serializer.
        Idempotent: only touches records whose ``metadata.project`` is
        missing, empty, or whitespace-only. Returns the number of
        records actually rewritten.
        """
        import dataclasses
        project = (project or DEFAULT_PROJECT).strip() or DEFAULT_PROJECT
        rewritten = 0
        for entry in self.knowledge.get_by_tier(Tier.DERIVED):
            if "milestone" not in (entry.tags or []):
                continue
            meta = dict(entry.metadata or {})
            # Match write-side normalization: treat whitespace-only
            # project values as effectively empty so the migration
            # doesn't leave records that read-side filters (also
            # strip-normalized) won't match.
            existing = meta.get("project")
            if isinstance(existing, str) and existing.strip():
                continue
            if existing is not None and not isinstance(existing, str) and existing:
                continue
            meta["project"] = project
            patched = dataclasses.replace(entry, metadata=meta)
            self.knowledge.add(patched)
            rewritten += 1
        return rewritten


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
        project=meta.get("project") or DEFAULT_PROJECT,
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


def _normalize_fr_ids(val: Any) -> list[str]:
    """Coerce a caller-supplied fr-ids argument into a list.

    A bare ``str`` is a valid single-id value, but it's also a Python
    ``Iterable[str]`` (of its own characters). Without this shim a
    caller passing ``add_fr_ids="fr_developer_foo"`` would get their
    string iterated character-by-character and each char treated as a
    separate FR id. Normalize early so downstream code can treat every
    input shape uniformly.
    """
    if val is None:
        return []
    if isinstance(val, str):
        return [val]
    return list(val)


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


def _sync_work_unit_frs(work_unit: dict[str, Any], new_fr_ids: list[str]) -> None:
    """Align ``work_unit["frs"]`` with ``new_fr_ids`` in-place.

    Preserves existing description/priority metadata for surviving
    entries so :meth:`review_scope` and :func:`_draft_spec` keep their
    context. Removed entries are dropped; added entries land as minimal
    ``{"fr_id": ...}`` dicts (downstream helpers tolerate missing
    description/priority). Order follows ``new_fr_ids``.

    Operates on both list-of-dict and list-of-str shapes, since
    ``propose_from_work_unit`` already normalizes bare-str fr items via
    ``_fr_id_from_item``. PR #43 Copilot R6 finding 2.
    """
    existing = work_unit.get("frs") or []
    by_id: dict[str, Any] = {}
    for item in existing:
        fid = _fr_id_from_item(item)
        if fid:
            by_id[fid] = item
    rebuilt: list[Any] = []
    for fid in new_fr_ids:
        if fid in by_id:
            rebuilt.append(by_id[fid])
        else:
            rebuilt.append({"fr_id": fid})
    work_unit["frs"] = rebuilt


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
