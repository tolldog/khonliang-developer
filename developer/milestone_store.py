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
from typing import Any, Optional

from khonliang.knowledge.store import (
    EntryStatus,
    KnowledgeEntry,
    KnowledgeStore,
    Tier,
)


MILESTONE_STATUS_PROPOSED = "proposed"
MILESTONE_STATUS_PLANNED = "planned"
MILESTONE_STATUS_IN_PROGRESS = "in_progress"
MILESTONE_STATUS_COMPLETED = "completed"
MILESTONE_STATUS_ARCHIVED = "archived"

ALL_MILESTONE_STATUSES = {
    MILESTONE_STATUS_PROPOSED,
    MILESTONE_STATUS_PLANNED,
    MILESTONE_STATUS_IN_PROGRESS,
    MILESTONE_STATUS_COMPLETED,
    MILESTONE_STATUS_ARCHIVED,
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

        frs = [fr for fr in (milestone.work_unit.get("frs") or []) if isinstance(fr, dict)]
        duplicate_groups = _duplicate_fr_groups(frs)
        term_hits = _term_hits(frs, review_terms or ["AutoGen", "GRA"])
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


def _normalized_fr_description(fr: dict[str, Any]) -> str:
    text = str(fr.get("description") or fr.get("title") or "").lower()
    text = re.sub(r"\s*(?:->|→)\s*[\w-]+\s*", " ", text)
    text = re.sub(r"\[(?:high|medium|low)\]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()
