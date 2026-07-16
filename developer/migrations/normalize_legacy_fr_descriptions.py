"""Tier 1 mechanical normalization of legacy embedded-JSON FR descriptions.

Part of fr_developer_68b4db12's Tier 1 slice: a batch of early-ingestion
FRs stored an entire JSON blob (``{"target": ..., "title": ...,
"description": ..., "priority": ..., "backing_papers": [...]}``) as
their ``description`` field instead of just the description text —
first observed on ``fr_developer_28a11ce2``, confirmed at scale via a
production scan (94/581 FRs affected as of 2026-07-13).

This is a thin reporting/orchestration layer over
:meth:`developer.fr_store.FRStore.normalize_legacy_description`, which
does the actual detection + repair (and is itself idempotent and safe
to call on terminal FRs). This module's job is just: enumerate every
FR across every project, call the store method, and produce a summary
report — the same dry-run-by-default shape as
:mod:`developer.migrations.fr_data_from_researcher` and
:mod:`developer.link_integrity`.

Tier 2 (LLM-assisted extraction for descriptions that *aren't* clean
JSON but are still unstructured legacy prose) and Tier 3 (flagging
records that can't be mechanically or LLM-normalized) are explicitly
out of scope here — both remain blocked on cross-repo work
(fr_store_c1ade8c7, fr_researcher_d813ad52, fr_librarian_07e4022c).
Malformed ids containing literal spaces (3 records, store-wide) are
also out of scope: renaming an id touches every cross-reference
(depends_on, merged_into, linked_bugs, ...) and is a separate,
higher-risk repair than a pure description-content fix.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from developer.fr_store import FRStore, _parse_legacy_description_blob


logger = logging.getLogger(__name__)


@dataclass
class LegacyDescriptionReport:
    """Summary of a normalization pass."""

    dry_run: bool
    checked: int = 0
    matched: int = 0
    normalized: int = 0
    already_normalized: int = 0
    ids: list[str] = field(default_factory=list)

    def summary(self) -> str:
        mode = "DRY-RUN" if self.dry_run else "APPLY"
        return (
            f"{mode} legacy FR description normalization: "
            f"checked={self.checked} matched={self.matched} "
            f"normalized={self.normalized} "
            f"already_normalized={self.already_normalized}"
        )


def normalize_legacy_fr_descriptions(
    frs: FRStore, *, apply: bool = False,
) -> LegacyDescriptionReport:
    """Scan every FR (all projects, all statuses) and normalize legacy
    embedded-JSON descriptions.

    ``apply=False`` (default) is a dry run: detects matches via the
    same parser the store method uses, but never calls
    :meth:`FRStore.normalize_legacy_description`, so nothing is
    written. ``apply=True`` performs the repair.

    Idempotent either way — re-running after ``apply=True`` reports
    the same records under ``already_normalized`` rather than
    re-matching them.
    """
    report = LegacyDescriptionReport(dry_run=not apply)
    all_frs = frs.list(project=None, include_all=True, sort=False)
    for fr in all_frs:
        report.checked += 1
        if fr.raw_description:
            report.already_normalized += 1
            continue
        if _parse_legacy_description_blob(fr.description, fr.title) is None:
            continue
        report.matched += 1
        report.ids.append(fr.id)
        if apply:
            frs.normalize_legacy_description(fr.id)
            report.normalized += 1

    logger.info("%s", report.summary())
    return report


def _main(argv: list[str] | None = None) -> int:
    import argparse

    from khonliang.knowledge.store import KnowledgeStore
    from librarian_lib import SelfCatalog

    from developer.config import Config

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Path to developer config.yaml")
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually write the normalization (default: dry-run report only)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    cfg = Config.load(args.config)
    knowledge = KnowledgeStore(str(cfg.db_path))
    catalog = None
    if args.apply:
        catalog = SelfCatalog(
            db_path=cfg.catalog_db_path, source="developer", owner_agent="developer-primary",
        )
    frs = FRStore(knowledge=knowledge, catalog=catalog)

    report = normalize_legacy_fr_descriptions(frs, apply=args.apply)
    print(report.summary())
    if report.matched:
        for fr_id in report.ids:
            print(f"  {fr_id}")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main())
