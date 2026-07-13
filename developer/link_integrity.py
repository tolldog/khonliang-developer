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
- ``reverse_link_on_merged_fr``: a reverse link landed on an FR whose
  status is ``merged`` — drift from a population path that ran before
  (or independently of) the merge. Per the FR's merge-redirect
  invariant, every reverse link belongs on the terminal FR; a
  merged-away FR carrying one is always wrong. Flagged on status alone
  (not on ``merged_into`` being set) — ``FRStore.update_status`` can
  set status to ``merged`` directly, without ever going through
  ``merge()``, producing a "merged but no merged_into pointer" shape
  that must be just as visible to the audit (Codex R8 on PR #93).
"""

from __future__ import annotations

from typing import Any, Optional

from developer.fr_store import FR_STATUS_MERGED, FRStore
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
        if fr.status != FR_STATUS_MERGED:
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
            frs.add_linked_milestone(terminal_fr_id, milestone_id)
            # Backfill: the FR may already carry PRs that predate this
            # milestone bundling (or were moved here by an earlier
            # reverse_link_on_merged_fr repair below) — without this,
            # "linked_prs is the union of PRs touching any bundled FR"
            # stays stale until an unrelated future PR merge (Codex R2
            # on PR #93). ``milestone_id`` is safe here — the mismatch
            # itself proves fr_id is currently in this milestone's
            # fr_ids, unlike the historical-membership case below.
            milestones.sync_linked_prs(frs, milestone_id)
            repaired.append(mismatch)
        elif kind == "reverse_link_on_merged_fr":
            terminal_id = frs.resolve_id(mismatch["fr_id"])
            if terminal_id == mismatch["fr_id"]:
                # resolve_id() falls back to the stale id itself when
                # merged_into points at a missing/broken target
                # (dangling redirect on corrupted/legacy data) — moving
                # links to "the terminal" would just re-add them to the
                # SAME record right before clear_reverse_links wipes it,
                # destroying the only copy (Codex R3 on PR #93). Refuse
                # rather than repair onto a target that isn't real.
                skipped.append({
                    **mismatch,
                    "reason": (
                        "merged_into is missing/broken; resolve_id "
                        "can't find a real terminal fr — refusing to "
                        "move (and then wipe) reverse links"
                    ),
                })
                continue
            stale = frs.get(mismatch["fr_id"], follow_redirect=False)
            if stale is None:
                skipped.append({**mismatch, "reason": "stale fr no longer exists"})
                continue
            # Replay every stale PR through add_linked_pr unconditionally
            # (Codex R10 on PR #93 — supersedes the R7 fix, which
            # pre-filtered out any PR the terminal already had a
            # {repo, number} entry for). That pre-filter was itself a
            # downgrade bug: add_linked_pr now compares completeness
            # (Codex R9) and safely no-ops on an already-current
            # terminal entry OR upgrades it when the stale copy is
            # actually the more complete one — skipping it here instead
            # threw away real upgrades, the exact legacy-drift case
            # repair exists to fix.
            # Pass the ORIGINAL stale id, not the pre-resolved
            # terminal_id (Codex R12 on PR #93) — add_linked_* already
            # does its own redirect resolution and sets
            # redirected_from when the id it was given differs from
            # where it landed; pre-resolving here meant the id we
            # passed always equaled the terminal, so that provenance
            # marker was silently dropped on every repaired entry.
            for pr in stale.linked_prs:
                frs.add_linked_pr(mismatch["fr_id"], pr)
            for spec in stale.linked_specs:
                frs.add_linked_spec(mismatch["fr_id"], spec)
            for ms_id in stale.linked_milestones:
                frs.add_linked_milestone(mismatch["fr_id"], ms_id)
            frs.clear_reverse_links(mismatch["fr_id"])
            terminal = frs.get(terminal_id, follow_redirect=False)
            if terminal is not None:
                # terminal.linked_milestones is historical (Codex R4 on
                # PR #93) — sync_linked_prs recomputes from each
                # milestone's actual current fr_ids, so a milestone the
                # FR was later removed from correctly doesn't gain this
                # PR back.
                for ms_id in terminal.linked_milestones:
                    milestones.sync_linked_prs(frs, ms_id)
            repaired.append(mismatch)
        else:  # pragma: no cover — defensive, audit only emits the two kinds above
            skipped.append({**mismatch, "reason": f"unknown mismatch type {kind!r}"})

    return {
        "dry_run": dry_run,
        "repaired": repaired,
        "skipped": skipped,
        "total_mismatches": len(audit["mismatches"]),
    }
