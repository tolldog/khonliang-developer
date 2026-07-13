"""Bidirectional link integrity: audit + repair (fr_developer_cfe3001c).

FR <-> milestone <-> spec <-> PR references are populated one-sidedly by
several independent call sites (``FRStore.add_linked_*``,
``MilestoneStore.update_frs``/``propose_from_work_unit``,
``SpecReader.list_specs``, the PR watcher's on-merge hook). This module
is the read-only cross-check over that graph, plus an opt-in repair
pass that only touches what the audit already flagged.

Two mismatch classes:

- ``milestone_fr_missing_reverse_link`` / ``milestone_fr_unresolvable``:
  a milestone's ``fr_ids`` entry has no matching ``linked_milestones``
  entry on the (redirect-resolved) FR it names — a bundling gap, most
  often from FR/milestone mutations that predate this FR or that ran
  without an ``fr_store`` handle.
- ``reverse_link_on_merged_fr``: a reverse link landed on an FR that
  has SINCE been merged into another (``merged_into`` set) — drift
  from a population path that ran before the merge. Per the FR's
  merge-redirect invariant, every reverse link belongs on the terminal
  FR; a merged-away FR carrying one is always wrong.
"""

from __future__ import annotations

from typing import Any, Optional

from developer.fr_store import FRStore
from developer.milestone_store import MilestoneStore


def audit_link_integrity(
    frs: FRStore, milestones: MilestoneStore, *, project: Optional[str] = None,
) -> dict[str, Any]:
    """Read-only scan; never mutates either store."""
    all_frs = frs.list(include_all=True, project=project)
    fr_by_id = {fr.id: fr for fr in all_frs}
    all_milestones = milestones.list(include_archived=True, project=project)

    mismatches: list[dict[str, Any]] = []

    for ms in all_milestones:
        for fr_id in ms.fr_ids:
            terminal_id = frs.resolve_id(fr_id)
            terminal = fr_by_id.get(terminal_id)
            if terminal is None:
                mismatches.append({
                    "type": "milestone_fr_unresolvable",
                    "milestone_id": ms.id,
                    "fr_id": fr_id,
                })
                continue
            if ms.id not in terminal.linked_milestones:
                mismatches.append({
                    "type": "milestone_fr_missing_reverse_link",
                    "milestone_id": ms.id,
                    "fr_id": fr_id,
                    "terminal_fr_id": terminal_id,
                })

    for fr in all_frs:
        if not fr.merged_into:
            continue
        if fr.linked_prs or fr.linked_specs or fr.linked_milestones:
            mismatches.append({
                "type": "reverse_link_on_merged_fr",
                "fr_id": fr.id,
                "merged_into": fr.merged_into,
                "linked_prs": len(fr.linked_prs),
                "linked_specs": len(fr.linked_specs),
                "linked_milestones": len(fr.linked_milestones),
            })

    return {
        "mismatches": mismatches,
        "checked_frs": len(all_frs),
        "checked_milestones": len(all_milestones),
    }


def repair_link_integrity(
    frs: FRStore,
    milestones: MilestoneStore,
    *,
    project: Optional[str] = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Reconcile what :func:`audit_link_integrity` flags.

    ``dry_run=True`` (default): reports what *would* be repaired without
    touching either store — damage is impossible without opt-in.
    """
    audit = audit_link_integrity(frs, milestones, project=project)
    repaired: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for mismatch in audit["mismatches"]:
        kind = mismatch["type"]
        if dry_run:
            skipped.append(mismatch)
            continue
        if kind == "milestone_fr_missing_reverse_link":
            terminal_fr_id = mismatch["terminal_fr_id"]
            milestone_id = mismatch["milestone_id"]
            fr = frs.add_linked_milestone(terminal_fr_id, milestone_id)
            # Backfill: the FR may already carry PRs that predate this
            # milestone bundling (or were moved here by an earlier
            # reverse_link_on_merged_fr repair below) — without this,
            # "linked_prs is the union of PRs touching any bundled FR"
            # stays stale until an unrelated future PR merge (Codex R2
            # on PR #93).
            for pr in fr.linked_prs:
                milestones.add_linked_pr(milestone_id, pr)
            repaired.append(mismatch)
        elif kind == "reverse_link_on_merged_fr":
            terminal_id = frs.resolve_id(mismatch["fr_id"])
            stale = frs.get(mismatch["fr_id"], follow_redirect=False)
            if stale is None:
                skipped.append({**mismatch, "reason": "stale fr no longer exists"})
                continue
            for pr in stale.linked_prs:
                frs.add_linked_pr(terminal_id, pr)
            for spec in stale.linked_specs:
                frs.add_linked_spec(terminal_id, spec)
            for ms_id in stale.linked_milestones:
                frs.add_linked_milestone(terminal_id, ms_id)
            frs.clear_reverse_links(mismatch["fr_id"])
            terminal = frs.get(terminal_id, follow_redirect=False)
            if terminal is not None:
                for ms_id in terminal.linked_milestones:
                    for pr in terminal.linked_prs:
                        milestones.add_linked_pr(ms_id, pr)
            repaired.append(mismatch)
        else:  # pragma: no cover — defensive, audit only emits the two kinds above
            skipped.append({**mismatch, "reason": f"unknown mismatch type {kind!r}"})

    return {
        "dry_run": dry_run,
        "repaired": repaired,
        "skipped": skipped,
        "total_mismatches": len(audit["mismatches"]),
    }
