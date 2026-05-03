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
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from khonliang.knowledge.store import (
    KnowledgeEntry,
    KnowledgeStore,
    Tier,
    EntryStatus,
)

from developer.project_store import DEFAULT_PROJECT, normalize_project


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

# Allowed transitions — a sparse, constrained workflow graph.
# Active states may move between each other when work is reprioritized or
# re-scoped. No transitions out of terminal states: once
# merged/archived/completed, an FR stays there. (Any resurrection would be a
# fresh FR that references the old one, not a status toggle.)
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
    # Phase 3 of fr_developer_5d0a8711: project as a first-class dimension.
    # Records pre-dating this field read back as "khonliang" via the default
    # in `_fr_from_entry`, so old data keeps working without an explicit
    # migration pass (though `migrate_records_to_project` is provided to
    # tidy the metadata once and for all).
    project: str = DEFAULT_PROJECT

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
            "project": self.project,
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

        # Non-merged FRs resolve to themselves with no hint.
        if fr.status != FR_STATUS_MERGED:
            return fr

        # Merged state but no pointer — partially-formed record. Signal that
        # the caller's id "redirects to itself" by setting redirected_from.
        if not fr.merged_into:
            fr.redirected_from = fr_id
            return fr

        # Walk the chain
        seen = {fr.id}
        current = fr
        requested_id = fr.id
        for _ in range(_MAX_REDIRECT_DEPTH):
            next_id = current.merged_into
            if not next_id:
                # Partially-formed merged record (status=merged but no
                # merged_into pointer). Treat it as a terminal redirect and
                # return the last well-formed FR in the chain with a hint,
                # rather than raising "exceeded depth".
                current.redirected_from = requested_id
                return current
            if next_id in seen:
                raise FRError(
                    f"redirect cycle detected while resolving {fr_id!r}: "
                    f"{' -> '.join(list(seen) + [next_id])}"
                )
            seen.add(next_id)
            next_entry = self.knowledge.get(next_id)
            if next_entry is None:
                # Dangling pointer; return the last well-formed FR in the
                # chain (``current``), not the originally requested record.
                # For A -> B -> missing, this returns B with the hint.
                current.redirected_from = requested_id
                return current
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
        project: Optional[str] = None,
    ) -> list[FR]:
        """List FRs in the store.

        Default: active statuses only (open / planned / in_progress).
        Pass ``include_all=True`` to include terminal states
        (completed, archived, merged).
        Pass ``status=<name>`` to filter to a single status
        (ignores ``include_all`` since it's more specific).
        Pass ``project=<slug>`` to restrict to one project; ``None``
        returns every project (cross-project view). Empty / whitespace
        strings normalize to :data:`DEFAULT_PROJECT` — matches writer
        normalization and avoids bus/CLI defaults (where ``""`` is
        common) silently bypassing the filter.
        """
        # Normalize the project filter once, up front. `None` means
        # "all projects"; anything else routes through the shared
        # `normalize_project` helper so `""` / whitespace / padded
        # slugs all behave like the canonical form rather than
        # disabling the filter or failing to match.
        if project is not None:
            project = normalize_project(project)
        entries = self.knowledge.get_by_tier(Tier.DERIVED)
        frs: list[FR] = []
        for entry in entries:
            if "fr" not in (entry.tags or []):
                continue
            fr = _fr_from_entry(entry)
            if target and fr.target != target:
                continue
            if project is not None and fr.project != project:
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
        project: str = DEFAULT_PROJECT,
    ) -> FR:
        """Create a new FR. Returns the stored :class:`FR`.

        ``id`` is derived deterministically from (target, title, concept)
        so re-promoting the same content yields the same id (collision
        detection via pre-existing entry).

        ``project`` partitions the record into a project slug; defaults
        to :data:`DEFAULT_PROJECT` so pre-Phase-3 callers keep working
        without changes. See fr_developer_1c5178d2 (Phase 3) for the
        full rollout plan.
        """
        if not target or not title:
            raise FRError("promote_fr requires non-empty target and title")
        if priority not in ALLOWED_PRIORITIES:
            raise FRError(
                f"priority must be one of {sorted(ALLOWED_PRIORITIES)}, got {priority!r}"
            )
        project = normalize_project(project)
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
            project=project,
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
            # after a crash recovery"). Bump updated_at so consumers watching
            # for "did this FR change" see the history delta.
            if notes:
                now = time.time()
                fr.notes_history.append({
                    "at": now, "status": status, "notes": notes,
                })
                fr.updated_at = now
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

    def merge(
        self,
        *,
        source_ids: Iterable[str],
        title: str,
        description: str,
        priority: Optional[str] = None,
        concept: str = "",
        classification: str = "app",
        merge_note: str = "",
        merge_roles: Optional[dict[str, str]] = None,
    ) -> FR:
        """Merge multiple source FRs into a new FR.

        Creates a new FR with the combined content, then marks each source
        as ``merged`` with ``metadata.merged_into`` pointing at the new FR.
        Dependency edges from other FRs that pointed at any source are
        redirected to the new FR.

        Source content is preserved verbatim — the merge never rewrites
        source titles or descriptions. Each source's ``merge_role`` captures
        what that source contributed (optional; pass via ``merge_roles``
        keyed by source id).

        **Why this design:** no existing record's status ever gets inverted
        (``merged`` is terminal), which avoids the stale-status-overwrite
        bug class that affects researcher's current merge implementation.

        Args:
            source_ids: FRs to merge (2 or more; duplicates deduped; each
                id is resolved through any existing redirect first, so
                passing already-merged ids merges their terminal targets).
            title, description: Content for the new FR.
            priority: New FR's priority. If omitted, takes the highest
                priority among the (resolved) sources.
            concept, classification, merge_note: carried as metadata.
            merge_roles: optional ``{source_id: role_description}`` map.
                Keys may match either original or resolved ids.

        Raises:
            FRError: if fewer than 2 distinct resolved sources, if any
                source is unknown, if any source is already terminal
                (completed / archived / merged — since that would either
                waste the operation or invert a terminal state), or if
                sources target different projects.
        """
        source_ids = list(source_ids or [])
        if len(source_ids) < 2:
            raise FRError(
                "merge requires at least 2 source FRs; "
                f"got {len(source_ids)}"
            )
        title = (title or "").strip()
        description = (description or "").strip()
        if not title:
            raise FRError("merge requires a non-empty title")
        if not description:
            raise FRError("merge requires a non-empty description")
        if priority is not None and priority not in ALLOWED_PRIORITIES:
            raise FRError(
                f"priority must be one of {sorted(ALLOWED_PRIORITIES)}, got {priority!r}"
            )

        # Resolve each source through existing redirects; collect in a stable
        # order (first occurrence wins) for deterministic merged_from order.
        resolved_sources: list[FR] = []
        seen_ids: set[str] = set()
        id_map: dict[str, str] = {}  # caller-id -> resolved-id for role rewriting
        for raw_id in source_ids:
            raw_id = (raw_id or "").strip()
            if not raw_id:
                continue
            resolved_id = self.resolve_id(raw_id)
            id_map[raw_id] = resolved_id
            if resolved_id in seen_ids:
                continue
            fr = self.get(resolved_id, follow_redirect=False)
            if fr is None:
                raise FRError(f"unknown source fr id: {raw_id!r}")
            if fr.status in TERMINAL_STATUSES:
                # Already terminal (archived/completed/merged). Merging would
                # either invert a terminal state or waste the operation.
                raise FRError(
                    f"source fr {resolved_id!r} is already terminal "
                    f"(status={fr.status!r}); cannot merge"
                )
            resolved_sources.append(fr)
            seen_ids.add(resolved_id)

        if len(resolved_sources) < 2:
            raise FRError(
                "merge needs 2+ distinct (post-redirect) sources; "
                f"got {len(resolved_sources)}"
            )

        # All sources must target the same target — merging across targets
        # is almost always a mistake and has no clean semantic. (The word
        # "target" here is the FR's target agent/app, not the Phase 3
        # `project` dimension — see the project check right below.)
        targets = {fr.target for fr in resolved_sources}
        if len(targets) > 1:
            raise FRError(
                f"cannot merge sources with different targets: {sorted(targets)}"
            )
        target = resolved_sources[0].target

        # Phase 3 of fr_developer_5d0a8711: merging across projects has no
        # clean semantic either. If sources span projects the merge would
        # silently drop the project dimension (the new FR would default
        # to DEFAULT_PROJECT regardless of where its inputs came from).
        # Reject instead, matching the same-target rule's spirit. Source
        # FRs come from _fr_from_entry, which already normalizes project,
        # so this set operation compares canonical slugs.
        projects = {fr.project for fr in resolved_sources}
        if len(projects) > 1:
            raise FRError(
                f"cannot merge sources with different projects: {sorted(projects)}"
            )
        project = next(iter(projects))

        # Derive priority from sources if not explicit — take the highest.
        effective_priority = priority or _max_priority(
            [fr.priority for fr in resolved_sources]
        )

        # Generate new FR id deterministically from source ids so re-merging
        # the same set yields the same id (the second call then fails the
        # already-exists check below — the right signal for an accidental
        # re-merge).
        merged_from_ids = [fr.id for fr in resolved_sources]
        new_id = _derive_merge_id(target, merged_from_ids)
        if self.knowledge.get(new_id) is not None:
            raise FRError(
                f"merged fr already exists at {new_id} "
                f"(same sources as a prior merge)"
            )

        # Combine backing_papers (stable order, deduped).
        combined_papers: list[str] = []
        for fr in resolved_sources:
            for paper in fr.backing_papers:
                if paper and paper not in combined_papers:
                    combined_papers.append(paper)

        # Combine depends_on (resolved through redirects + deduped). Drop any
        # dep pointing at a source (or at the new id) to avoid self-reference
        # after the redirect step below.
        source_id_set = set(merged_from_ids)
        combined_deps: list[str] = []
        for fr in resolved_sources:
            for dep in fr.depends_on:
                resolved_dep = self.resolve_id(dep)
                if resolved_dep in source_id_set or resolved_dep == new_id:
                    continue
                if resolved_dep not in combined_deps:
                    combined_deps.append(resolved_dep)

        # Cycle check: a combined_deps entry that (transitively) depends on
        # any source FR would become a cycle after _redirect_dependents
        # rewrites those edges to new_id. Example that would cycle:
        #   A depends on X (so combined_deps includes X)
        #   X depends on B (a source)
        # After the merge: new depends on X, and X's dep on B gets
        # rewritten to new → new ↔ X cycle. Reject up front so we never
        # commit that state to the store.
        for dep in combined_deps:
            offending = self._transitive_dep_hits(dep, source_id_set)
            if offending is not None:
                raise FRError(
                    f"merge would create a dependency cycle: {dep!r} "
                    f"transitively depends on source {offending!r}. "
                    "Resolve the dependency before merging."
                )

        now = time.time()
        # New FR starts `open`; the merge doesn't imply the combined work is
        # planned or in-progress yet. `project` inherits from the sources
        # (they must all agree by the check above) so merged FRs stay in
        # their originating project's partition.
        new_fr = FR(
            id=new_id,
            target=target,
            title=title,
            description=description,
            status=FR_STATUS_OPEN,
            priority=effective_priority,
            concept=concept,
            classification=classification,
            project=project,
            backing_papers=combined_papers,
            depends_on=combined_deps,
            branch="",
            notes_history=[{
                "at": now,
                "status": FR_STATUS_OPEN,
                "notes": f"merged from {', '.join(merged_from_ids)}",
            }],
            merged_into=None,
            merged_from=list(merged_from_ids),
            merge_role="",
            merge_note=merge_note,
            created_at=now,
            updated_at=now,
        )
        self._store(new_fr)

        # Mark each source as merged. Content preserved; only status +
        # merged_into + merge_role change.
        roles = merge_roles or {}
        for fr in resolved_sources:
            fr.status = FR_STATUS_MERGED
            fr.merged_into = new_id
            # Role lookup: caller-provided original id wins, else resolved id.
            role = ""
            for caller_id, resolved_id in id_map.items():
                if resolved_id == fr.id and caller_id in roles:
                    role = roles[caller_id]
                    break
            if not role and fr.id in roles:
                role = roles[fr.id]
            fr.merge_role = role
            fr.notes_history.append({
                "at": now,
                "status": FR_STATUS_MERGED,
                "notes": f"merged into {new_id}" + (f" ({role})" if role else ""),
            })
            fr.updated_at = now
            self._store(fr)
            # Capability: mark abandoned. The old entry's fr_id ref remains
            # for audit; a new capability entry for the merged FR will be
            # created when that FR transitions to planned/in_progress.
            self._record_capability(fr)

        # Redirect dependency edges on any other FR that pointed at a source.
        self._redirect_dependents(merged_from_ids, new_id)

        return new_fr

    def update(
        self,
        fr_id: str,
        *,
        title: Optional[str] = None,
        description: Optional[str] = None,
        priority: Optional[str] = None,
        concept: Optional[str] = None,
        classification: Optional[str] = None,
        backing_papers: Optional[Iterable[str]] = None,
        notes: str = "",
    ) -> FR:
        """Modify an existing FR in place.

        **Field semantics — all uniform on None-means-no-change:**

        - Any field passed as ``None`` (the default) is left untouched.
        - Any non-None value is applied, after normalization:
          - ``title`` / ``description`` — stripped; must be non-empty.
            An empty or whitespace-only value raises :class:`FRError`
            (these fields are required on an FR by the same invariant
            that :meth:`promote` enforces). Bus handlers translate
            caller-provided empty strings to ``None`` before calling
            this API to keep their "omit for no change" ergonomic.
          - ``concept`` / ``classification`` — stored as-is, including
            empty-string (which clears the field).
          - ``backing_papers`` — each entry is stripped and empty values
            filtered out. Pass an empty list to clear entirely.

        Resolves ``fr_id`` through merge redirects, so updating an old id
        lands on the terminal FR. Terminal FRs (merged / archived /
        completed) cannot be edited — the audit trail must stay immutable.

        ``notes`` is appended to ``notes_history`` with the current status
        (no status change). Useful for recording "re-scoped description"
        or "priority bumped after X incident."
        """
        resolved_id = self.resolve_id(fr_id)
        fr = self.get(resolved_id, follow_redirect=False)
        if fr is None:
            raise FRError(f"unknown fr id: {fr_id}")
        if fr.status in TERMINAL_STATUSES:
            raise FRError(
                f"cannot update {resolved_id}: status is {fr.status!r} "
                "(terminal FRs are immutable)"
            )

        if priority is not None and priority not in ALLOWED_PRIORITIES:
            raise FRError(
                f"priority must be one of {sorted(ALLOWED_PRIORITIES)}, got {priority!r}"
            )

        changed = False

        if title is not None:
            stripped = title.strip()
            if not stripped:
                raise FRError("title must be non-empty when provided")
            if stripped != fr.title:
                fr.title = stripped
                changed = True

        if description is not None:
            stripped = description.strip()
            if not stripped:
                raise FRError("description must be non-empty when provided")
            if stripped != fr.description:
                fr.description = stripped
                changed = True

        if priority is not None and priority != fr.priority:
            fr.priority = priority
            changed = True
        if concept is not None and concept != fr.concept:
            fr.concept = concept
            changed = True
        if classification is not None and classification != fr.classification:
            fr.classification = classification
            changed = True
        if backing_papers is not None:
            # Strip entries so stored values are clean (filter was only
            # controlling which were kept; we also normalize what we keep).
            new_papers = [p.strip() for p in backing_papers if p and p.strip()]
            if new_papers != fr.backing_papers:
                fr.backing_papers = new_papers
                changed = True

        if not changed and not notes:
            # Nothing to do — idempotent no-op. Don't bump updated_at.
            return fr

        now = time.time()
        history_note = notes or "edited in place"
        fr.notes_history.append({
            "at": now,
            "status": fr.status,
            "notes": history_note,
        })
        fr.updated_at = now
        self._store(fr)
        return fr

    def _filter_scope(
        self,
        *,
        target: Optional[str] = None,
        project: Optional[str] = None,
        concept: Optional[str] = None,
        fr_id_set: Optional[set[str]] = None,
    ):
        """Yield FRs matching ``(target, project, concept, fr_id_set)``,
        independent of readiness (status + deps). Shared between
        :meth:`next_fr` and :meth:`count_in_scope` so the scope-filter
        rules can't drift between "is this FR a candidate?" and
        "does this FR exist in the scope at all?". PR #66 review
        pass-4.

        Whitespace normalization is centralized here: ``target`` and
        ``concept`` both go through ``str(...).strip() or None`` so a
        padded value like ``" developer "`` matches a stored target.
        Callers don't have to replicate the normalization, and a
        future scope-using path (or a direct ``next_fr`` consumer)
        gets the same forgiving behavior. PR #66 review pass-6.
        """
        # ``str(value or "").strip() or None`` — coerces non-string
        # inputs (an internal caller passing an int / Path / etc.)
        # before strip rather than crashing with AttributeError on
        # the bare ``.strip()`` call. The previous ``next_fr`` impl
        # only compared values without normalization, so a non-string
        # target wouldn't crash there either; matching that
        # robustness here keeps the centralization regression-free.
        # PR #66 review pass-7.
        target_norm = str(target or "").strip() or None
        concept_norm = str(concept or "").strip() or None
        for fr in self.list(include_all=True, project=project):
            if target_norm and fr.target != target_norm:
                continue
            if concept_norm and (fr.concept or "").strip() != concept_norm:
                continue
            if fr_id_set is not None and fr.id not in fr_id_set:
                continue
            yield fr

    def count_in_scope(
        self,
        *,
        target: Optional[str] = None,
        project: Optional[str] = None,
        concept: Optional[str] = None,
        fr_id_set: Optional[set[str]] = None,
    ) -> int:
        """Count how many FRs match a scope, ignoring readiness.

        ``next_fr`` filters by status + dep readiness; this helper
        applies only the same scope filters (target, project,
        concept, fr_id_set) so callers can disambiguate "scope is
        empty" from "scope has FRs but none are ready". Used by
        ``handle_next_fr_local`` on the empty-result path to
        produce a better-tailored failure reason. PR #66 review
        pass-4.
        """
        return sum(1 for _ in self._filter_scope(
            target=target, project=project, concept=concept, fr_id_set=fr_id_set,
        ))

    def next_fr(
        self,
        *,
        target: Optional[str] = None,
        project: Optional[str] = None,
        concept: Optional[str] = None,
        fr_id_set: Optional[set[str]] = None,
    ) -> Optional[FR]:
        """Pick the highest-priority FR that's ready to work on.

        "Ready" means:
        - Status is `open` or `planned` (not in_progress — someone's already
          on it; not terminal — already done/abandoned/merged)
        - Every FR in `depends_on` is either `completed` or `merged` into a
          completed FR (dependency graph unblocked)

        Among ready FRs, pick by: highest priority first, then oldest
        `created_at` (first-in, first-out). Returns None when nothing
        qualifies.

        ``target`` optionally restricts to a single agent/app target.
        ``project`` optionally restricts to a single project slug;
        None returns every project (cross-project view). Filtering is
        delegated to :meth:`list`, which normalizes the value.

        ``concept`` optionally restricts to FRs whose ``concept`` field
        matches exactly (whitespace-stripped). Closes the
        ``fr_developer_39a58719`` dogfood gap: when actively building
        one cluster, ``next_fr`` without this filter would surface
        unrelated cross-project FRs — so callers had to manually
        enumerate within their concept lane.

        ``fr_id_set`` optionally restricts the search to a specific
        set of FR ids — typically a milestone's bundle. The
        ``handle_next_fr_local`` skill resolves this from a
        ``milestone_id`` arg.
        """
        # Scope filter is shared with ``count_in_scope`` so the two
        # methods can't drift on what "in this scope" means; the
        # readiness checks (status + deps) layer on top here.
        candidates = []
        for fr in self._filter_scope(
            target=target, project=project, concept=concept, fr_id_set=fr_id_set,
        ):
            if fr.status not in (FR_STATUS_OPEN, FR_STATUS_PLANNED):
                continue
            if not self._deps_unblocked(fr):
                continue
            candidates.append(fr)

        if not candidates:
            return None

        priority_order = {"high": 0, "medium": 1, "low": 2}
        candidates.sort(key=lambda f: (
            priority_order.get(f.priority, 99),
            f.created_at,
        ))
        return candidates[0]

    def _deps_unblocked(self, fr: FR) -> bool:
        """True if every FR in fr.depends_on is completed (or points, via
        merge, at a completed FR).

        Merged dependencies are resolved through the chain — if A depended
        on B, and B merged into C, then A's dep is effectively on C.
        """
        for dep_id in fr.depends_on:
            resolved_dep = self.get(dep_id, follow_redirect=True)
            if resolved_dep is None:
                # Dangling dep — treat as unmet. Caller should clean up.
                return False
            if resolved_dep.status != FR_STATUS_COMPLETED:
                return False
        return True

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
        """Serialize an :class:`FR` back into the KnowledgeStore.

        ``KnowledgeStore.add`` overwrites ``updated_at`` with its own clock
        to guarantee monotonic timestamps. We read the stored value back
        and sync it onto the caller's ``fr`` so the returned object
        matches what's persisted — otherwise an in-memory idempotent
        compare (e.g. ``update(no-change)``) would see stale times.
        """
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
                "project": fr.project or DEFAULT_PROJECT,
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
        # Sync timestamps from the stored entry so fr reflects reality
        stored = self.knowledge.get(fr.id)
        if stored is not None:
            fr.created_at = stored.created_at
            fr.updated_at = stored.updated_at

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

    def _transitive_dep_hits(
        self, start: str, targets: set[str], *, _visited: Optional[set[str]] = None,
    ) -> Optional[str]:
        """Return the first target id reachable from ``start`` via deps, or None.

        Used by :meth:`merge` to pre-validate that combined_deps don't
        transitively depend on any source FR — such a dep would become
        a cycle once _redirect_dependents rewrites source-pointing edges
        to ``new_id``.
        """
        if _visited is None:
            _visited = set()
        if start in _visited:
            return None
        _visited.add(start)
        entry = self.knowledge.get(start)
        if entry is None:
            return None
        deps = (entry.metadata or {}).get("depends_on") or []
        for dep in deps:
            resolved = self.resolve_id(dep)
            if resolved in targets:
                return resolved
            hit = self._transitive_dep_hits(resolved, targets, _visited=_visited)
            if hit is not None:
                return hit
        return None

    def _redirect_dependents(self, source_ids: Iterable[str], new_id: str) -> None:
        """Rewrite depends_on edges that point at any source to point at new_id.

        Called from :meth:`merge` so other FRs that referenced a merged-away
        source now resolve their dependency to the new consolidated FR.
        Dedupes edges (if an FR already depended on both a source and the
        new id, we don't produce a duplicate).
        """
        source_set = set(source_ids)
        for entry in self.knowledge.get_by_tier(Tier.DERIVED):
            if "fr" not in (entry.tags or []):
                continue
            if entry.id in source_set or entry.id == new_id:
                continue
            meta = entry.metadata or {}
            deps = meta.get("depends_on") or []
            if not any(d in source_set for d in deps):
                continue

            rewritten: list[str] = []
            changed = False
            for dep in deps:
                if dep in source_set:
                    if new_id not in rewritten:
                        rewritten.append(new_id)
                    changed = True
                else:
                    if dep not in rewritten:
                        rewritten.append(dep)

            if not changed:
                continue

            fr = _fr_from_entry(entry)
            fr.depends_on = rewritten
            fr.updated_at = time.time()
            self._store(fr)

    # ------------------------------------------------------------------
    # fr_developer_1c5178d2 — project-dimension migration helper
    # ------------------------------------------------------------------

    def migrate_records_to_project(
        self, project: str = DEFAULT_PROJECT
    ) -> int:
        """Stamp ``project`` onto FR records whose metadata lacks it.

        Read-time fallback in :func:`_fr_from_entry` already surfaces
        pre-Phase-3 records as ``project=DEFAULT_PROJECT``; this helper
        writes the value into persisted metadata for callers that want
        the data canonicalized rather than fallback-interpreted.

        Narrow contract: patches ONLY ``metadata["project"]``. Clones
        the existing :class:`KnowledgeEntry` with
        :func:`dataclasses.replace` and adds the key, preserving tags,
        title, content, and any unknown metadata keys legacy rows may
        carry. Re-serializing through :meth:`_store` would rewrite the
        entry through the current shape and silently drop anything the
        dataclass doesn't know about, which isn't what "stamp a missing
        key" promises.

        ``updated_at`` is not preserved: ``KnowledgeStore.add`` refreshes
        its own clock on write to guarantee monotonic timestamps. If the
        caller needs the original ``updated_at`` retained they must go
        around this helper.

        Idempotent: only touches records whose ``metadata.project`` is
        missing, empty, or whitespace-only (write-side and read-side
        filters strip before comparing, so an unnormalized slug would
        silently fail to match anything). Returns the number of
        records actually rewritten so callers can log / assert the
        migration ran.
        """
        import dataclasses
        project = normalize_project(project)
        rewritten = 0
        for entry in self.knowledge.get_by_tier(Tier.DERIVED):
            if "fr" not in (entry.tags or []):
                continue
            meta = dict(entry.metadata or {})
            # Skip records that already carry an intentional project
            # slug; stamp the ones whose value is missing, empty, or
            # whitespace-only so they match read-side filters (which
            # normalize the same way). We look at the raw stored value
            # here — not `normalize_project(existing)` — so a record
            # already pinned to DEFAULT_PROJECT isn't rewritten as a
            # no-op; only truly-unset records get touched.
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


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------


def _fr_from_entry(entry: KnowledgeEntry) -> FR:
    """Convert a KnowledgeEntry back into an :class:`FR`.

    Records written before fr_developer_1c5178d2 don't have ``project``
    in their metadata, and records with empty / whitespace-only /
    non-string ``project`` values are normalized the same way:
    :func:`normalize_project` coerces the raw metadata value, strips
    whitespace, and falls back to :data:`DEFAULT_PROJECT` when the
    result is empty. Readers, writers, list filters, and migration all
    route through the same helper so filters work regardless of how
    the stored value was produced. ``migrate_records_to_project`` can
    stamp the field onto the persisted data once the caller is ready
    to tidy up.
    """
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
        project=normalize_project(meta.get("project")),
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


def _derive_merge_id(target: str, source_ids: Iterable[str]) -> str:
    """Stable id for a merge of a given set of source FRs.

    Sorts source ids so order-of-arguments doesn't change the id (the same
    set of sources should always produce the same merged FR id, letting the
    existing-entry check catch accidental re-merges).
    """
    payload = "merge:" + ":".join(sorted(source_ids))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]
    return f"fr_{target}_{digest}"


def _derive_capability_id(target: str, capability_name: str) -> str:
    """Stable capability id per (target, capability)."""
    payload = f"cap:{target}:{capability_name}".encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()[:12]
    return f"capability_{target}_{digest}"


def _max_priority(priorities: Iterable[str]) -> str:
    """Return the highest priority (``high`` > ``medium`` > ``low``).

    Used by :meth:`FRStore.merge` when the caller doesn't pin a priority —
    the merged FR inherits the maximum across its sources so a high-priority
    source doesn't silently get demoted.
    """
    order = {"high": 2, "medium": 1, "low": 0}
    best = "medium"
    best_score = 1
    for p in priorities:
        score = order.get(p, 1)
        if score > best_score:
            best = p
            best_score = score
    return best
