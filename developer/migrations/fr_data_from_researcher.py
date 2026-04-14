"""Migrate FR records (and capability entries) from researcher.db into developer.db.

This is the data side of the PM ownership handoff
(``fr_developer_0ab2aa9b``). PR #10 landed the FRStore on developer;
PRs #11 and #13 completed the write surface. All that's missing is
moving the records that currently live in researcher's KnowledgeStore
over to developer's so developer becomes the single source of truth.

**Design:**

- Read-only on the source. Opens researcher.db via a ``file:...?mode=ro``
  URI — no shared writer, no risk of partial corruption.
- Idempotent. Re-running is safe: entries already present on the
  developer side are skipped (content diffed; if content changed,
  we log and skip rather than overwrite, so the caller can triage).
- Dry-run mode by default (call with ``apply=True`` to actually write).
- Records migrated:
  - Every ``tier=DERIVED`` entry tagged ``fr`` — gets written through
    :class:`FRStore` so its metadata normalization applies
  - Every ``tier=DERIVED`` entry tagged ``capability`` — copied via
    the underlying KnowledgeStore (FRStore's own capability tracking
    re-fires when status transitions, but we preserve pre-migration
    state so history is complete from day one)
- Tag-to-status translation: researcher tags FR status via ``fr:<state>``
  tags (``fr:archived``, ``fr:merged_into:<id>``, etc.). We normalize
  into developer's ``metadata.fr_status`` + the merge-redirect fields
  FRStore expects, so the read-side framework's redirect walks work
  immediately after migration.

**Not migrated:**
- Non-FR Tier.DERIVED entries (paper summaries, concept-matrix caches,
  etc.). Those are researcher's knowledge output and stay on researcher's
  side.
- Triples, digests, raw knowledge entries. Researcher owns the corpus.

**Output:** a :class:`MigrationReport` with counts and skipped-entry
reasons. Callers should inspect and decide whether to re-run in
``apply=True`` mode.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from khonliang.knowledge.store import EntryStatus, KnowledgeEntry, KnowledgeStore, Tier

from developer.fr_store import (
    FR_STATUS_ARCHIVED,
    FR_STATUS_COMPLETED,
    FR_STATUS_MERGED,
    FR_STATUS_OPEN,
    FRStore,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Report shape
# ---------------------------------------------------------------------------


@dataclass
class MigrationReport:
    """Summary of what the migration did (or would do, in dry-run)."""

    dry_run: bool
    source_db: str
    target_db: str

    frs_found: int = 0
    frs_migrated: int = 0
    frs_already_present: int = 0
    frs_skipped: list[dict[str, str]] = field(default_factory=list)

    capabilities_found: int = 0
    capabilities_migrated: int = 0
    capabilities_already_present: int = 0

    def summary(self) -> str:
        mode = "DRY-RUN" if self.dry_run else "APPLY"
        return (
            f"{mode} migration {self.source_db} -> {self.target_db}\n"
            f"  FRs:          found={self.frs_found} migrated={self.frs_migrated} "
            f"already_present={self.frs_already_present} skipped={len(self.frs_skipped)}\n"
            f"  Capabilities: found={self.capabilities_found} migrated={self.capabilities_migrated} "
            f"already_present={self.capabilities_already_present}"
        )


# ---------------------------------------------------------------------------
# Tag parsing — researcher's convention for FR status
# ---------------------------------------------------------------------------


# Researcher encodes FR status and merge-redirect info in tags:
#   "fr:archived"
#   "fr:merged_into:fr_developer_xxxx"
#   "fr:completed"
# (open/planned/in_progress have no explicit tag; they're the default
# "fr" tag alone.)
_MERGED_INTO_RE = re.compile(r"^fr:merged_into:(?P<target>fr_[a-z0-9_]+)$")
_STATUS_TAG_RE = re.compile(r"^fr:(?P<status>[a-z_]+)$")

# Valid FR statuses we know how to translate from tag form.
_TAG_STATUS_MAP = {
    "archived": FR_STATUS_ARCHIVED,
    "completed": FR_STATUS_COMPLETED,
    "merged": FR_STATUS_MERGED,
}


def _extract_fr_status_and_merge(tags: list[str]) -> tuple[str, Optional[str]]:
    """Given a researcher FR's tags, return (fr_status, merged_into_id?).

    Defaults to ``open`` when no explicit tag is present (researcher's
    convention for fresh FRs).
    """
    merged_into: Optional[str] = None
    status = FR_STATUS_OPEN
    for tag in tags:
        m_merge = _MERGED_INTO_RE.match(tag)
        if m_merge:
            status = FR_STATUS_MERGED
            merged_into = m_merge.group("target")
            continue
        m_status = _STATUS_TAG_RE.match(tag)
        if m_status:
            key = m_status.group("status")
            if key in _TAG_STATUS_MAP:
                status = _TAG_STATUS_MAP[key]
    return status, merged_into


# ---------------------------------------------------------------------------
# Main migration
# ---------------------------------------------------------------------------


def migrate(
    source_db: str | Path,
    target_store: FRStore,
    *,
    apply: bool = False,
) -> MigrationReport:
    """Copy FR + capability records from ``source_db`` into ``target_store``.

    ``apply=False`` (default) does a dry run — no writes, just counts
    and skip reasons.

    Idempotent: records already present on the target (matched by id +
    content) are counted under ``already_present`` rather than overwritten.
    Records whose content would change are added to ``skipped`` with a
    reason; the caller decides whether to resolve manually.
    """
    source_path = str(Path(source_db).resolve())
    target_path = target_store.knowledge.db_path
    report = MigrationReport(
        dry_run=not apply,
        source_db=source_path,
        target_db=target_path,
    )

    # Read-only connection to the source; URI form ensures we never write.
    src = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    try:
        _migrate_frs(src, target_store, report, apply=apply)
        _migrate_capabilities(src, target_store.knowledge, report, apply=apply)
    finally:
        src.close()

    logger.info("%s", report.summary())
    return report


def _migrate_frs(
    src: sqlite3.Connection,
    target_store: FRStore,
    report: MigrationReport,
    *,
    apply: bool,
) -> None:
    """Port Tier.DERIVED entries tagged 'fr' from source to target."""
    # researcher tags are stored as JSON array text in KnowledgeStore —
    # use LIKE on the serialized form for pre-filtering, then parse to
    # confirm. Avoids parsing every DERIVED entry just to skip non-FRs.
    rows = src.execute(
        "SELECT * FROM knowledge "
        "WHERE tier = ? AND tags LIKE '%\"fr\"%' "
        "ORDER BY created_at",
        (int(Tier.DERIVED),),
    ).fetchall()

    for row in rows:
        tags = _parse_json(row["tags"], default=[])
        if "fr" not in tags:
            # LIKE false-positive (e.g. the string 'fr' appeared inside
            # another tag); skip.
            continue
        report.frs_found += 1

        fr_id = row["id"]
        metadata = _parse_json(row["metadata"], default={})

        # Translate researcher's tag-encoded status into developer's
        # metadata.fr_status + merge-redirect fields.
        fr_status, merged_into = _extract_fr_status_and_merge(tags)

        # Build the metadata shape FRStore._store writes. Preserve
        # researcher-side extras (like "synergies") under the same key
        # so they survive the migration.
        new_metadata = {
            "fr_status": fr_status,
            "priority": metadata.get("priority", "medium"),
            "concept": metadata.get("concept", ""),
            "classification": metadata.get("classification", "app"),
            "target": metadata.get("target", _target_from_id(fr_id)),
            "backing_papers": list(metadata.get("backing_papers") or []),
            "depends_on": list(metadata.get("depends_on") or []),
            "branch": metadata.get("branch", ""),
            "notes_history": list(metadata.get("notes_history") or []),
            "merged_into": merged_into,
            "merged_from": list(metadata.get("merged_from") or []),
            "merge_role": metadata.get("merge_role", ""),
            "merge_note": metadata.get("merge_note", ""),
        }
        # Preserve anything else the researcher recorded (e.g. synergies)
        # so the migration is lossless. Skip fields we already handled.
        for k, v in metadata.items():
            if k in new_metadata or k in {"fr_status", "status"}:
                continue
            new_metadata[k] = v

        existing = target_store.knowledge.get(fr_id)
        if existing is not None:
            if _fr_content_matches(existing, row, new_metadata):
                report.frs_already_present += 1
            else:
                report.frs_skipped.append({
                    "id": fr_id,
                    "reason": "already present on target with different content — manual resolution needed",
                })
            continue

        if not apply:
            report.frs_migrated += 1
            continue

        entry = KnowledgeEntry(
            id=fr_id,
            tier=Tier.DERIVED,
            title=row["title"],
            content=row["content"],
            source="developer.fr_store",  # match what FRStore writes going forward
            scope="development",
            confidence=row["confidence"] if row["confidence"] is not None else 1.0,
            status=EntryStatus.DISTILLED,
            tags=["fr", f"target:{new_metadata['target']}", new_metadata["classification"]],
            metadata=new_metadata,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
        target_store.knowledge.add(entry)
        report.frs_migrated += 1


def _migrate_capabilities(
    src: sqlite3.Connection,
    target_store: KnowledgeStore,
    report: MigrationReport,
    *,
    apply: bool,
) -> None:
    """Port Tier.DERIVED entries tagged 'capability' from source to target."""
    rows = src.execute(
        "SELECT * FROM knowledge "
        "WHERE tier = ? AND tags LIKE '%\"capability\"%' "
        "ORDER BY created_at",
        (int(Tier.DERIVED),),
    ).fetchall()

    for row in rows:
        tags = _parse_json(row["tags"], default=[])
        if "capability" not in tags:
            continue
        report.capabilities_found += 1

        if target_store.get(row["id"]) is not None:
            report.capabilities_already_present += 1
            continue
        if not apply:
            report.capabilities_migrated += 1
            continue

        entry = KnowledgeEntry(
            id=row["id"],
            tier=Tier.DERIVED,
            title=row["title"],
            content=row["content"],
            source="developer.fr_store.capability",
            scope="development",
            confidence=row["confidence"] if row["confidence"] is not None else 1.0,
            status=EntryStatus.DISTILLED,
            tags=list(tags),
            metadata=_parse_json(row["metadata"], default={}),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
        target_store.add(entry)
        report.capabilities_migrated += 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_json(text: Any, *, default: Any) -> Any:
    if text is None:
        return default
    if isinstance(text, (list, dict)):
        return text
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return default


def _target_from_id(fr_id: str) -> str:
    """Derive the target project name from an FR id like 'fr_<target>_<hex>'.

    Fallback for entries whose metadata didn't explicitly record the target.
    """
    # fr_<target>_<hex>
    parts = fr_id.split("_")
    if len(parts) < 3 or parts[0] != "fr":
        return "unknown"
    return "_".join(parts[1:-1])


def _fr_content_matches(
    existing: KnowledgeEntry, src_row: sqlite3.Row, new_metadata: dict,
) -> bool:
    """Heuristic match: title + description + target all equal.

    Used by the idempotent path to decide whether the target already has
    this FR in a compatible state. Doesn't compare full metadata (some
    fields like notes_history may differ by noise); focuses on the
    identity-ish fields.
    """
    if existing.title != src_row["title"]:
        return False
    if existing.content != src_row["content"]:
        return False
    existing_target = (existing.metadata or {}).get("target")
    if existing_target and existing_target != new_metadata.get("target"):
        return False
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main(argv: Optional[list[str]] = None) -> int:
    """``python -m developer.migrations.fr_data_from_researcher``.

    Requires --source (path to researcher.db) and --developer-config
    (path to developer's config.yaml, so we can build an FRStore
    against the correct developer.db). Defaults to dry-run; pass
    --apply to write.
    """
    import argparse

    from developer.config import Config

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", required=True, help="path to researcher.db")
    parser.add_argument("--developer-config", required=True,
                        help="path to developer's config.yaml")
    parser.add_argument("--apply", action="store_true",
                        help="actually write (default: dry-run)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    config = Config.load(args.developer_config)
    knowledge = KnowledgeStore(str(config.db_path))
    store = FRStore(knowledge=knowledge)

    report = migrate(args.source, store, apply=args.apply)
    print(report.summary())
    if report.frs_skipped:
        print("\nSkipped entries:")
        for s in report.frs_skipped:
            print(f"  {s['id']}: {s['reason']}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv[1:]))
