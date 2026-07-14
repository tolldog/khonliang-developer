#!/usr/bin/env python3
"""Recurring FR<->spec<->milestone<->PR reverse-link reconciliation
(fr_developer_cfe3001c).

Runs audit_link_integrity + repair_link_integrity against a live
developer deployment's own database, across every project. Intended
to be invoked periodically (cron) with the deployment venv's
interpreter so `developer.*` imports resolve to the installed package,
not this checkout.

Usage:
    /opt/khonliang/agents/developer/.venv/bin/python3 \\
        scripts/link_integrity_sweep.py --config /opt/khonliang/etc/developer/config.yaml

Logs a compact summary to stdout (captured by cron into a logfile via
redirection); a non-zero exit code signals a run that hit an
unexpected exception (not: a mismatch was found — mismatches/skips
found are expected steady-state signal, not a failure).

Constructs FRStore/MilestoneStore directly rather than going through
Pipeline.from_config (Codex review): the full pipeline also
constructs BugStore/DogfoodStore, which seed curated rows on first
construction against a fresh/partially-initialized DB — a real write
side effect that would fire even under --dry-run, contradicting the
flag's whole purpose. This script only needs the two stores link
integrity actually touches.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from khonliang.knowledge.store import KnowledgeStore
from librarian_lib import SelfCatalog

from developer.config import Config
from developer.fr_store import FRStore
from developer.milestone_store import MilestoneStore
from developer.link_integrity import audit_link_integrity, repair_link_integrity


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Path to developer config.yaml")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report only; don't write repairs (default: repair is applied)",
    )
    args = parser.parse_args()

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[{stamp}] link_integrity_sweep starting (dry_run={args.dry_run})")

    cfg = Config.load(args.config)
    # Fail fast rather than let KnowledgeStore silently create a fresh,
    # empty DB on a wrong/unmounted db_path (Codex round 3) — that would
    # audit 0 FRs/milestones and exit 0, a false "all clear" for exactly
    # the misconfiguration this cron job needs to surface, not hide.
    if not Path(cfg.db_path).exists():
        print(f"[{stamp}] ERROR: configured db_path does not exist: {cfg.db_path}")
        return 1
    knowledge = KnowledgeStore(str(cfg.db_path))
    # Catalog is a write-only sidecar (FRStore/MilestoneStore._store's
    # catalog.upsert) — audit and dry-run repair never touch it, so a
    # report-only sweep shouldn't depend on the sidecar being present
    # or writable (Codex round 2). Only construct it when repairs will
    # actually be written.
    catalog = None
    if not args.dry_run:
        catalog = SelfCatalog(
            db_path=cfg.catalog_db_path, source="developer", owner_agent="developer-primary",
        )
    frs = FRStore(knowledge=knowledge, catalog=catalog)
    milestones = MilestoneStore(knowledge=knowledge, catalog=catalog)

    before = audit_link_integrity(frs, milestones, project=None)
    print(
        f"[{stamp}] audit: checked_frs={before['checked_frs']} "
        f"checked_milestones={before['checked_milestones']} "
        f"mismatches={len(before['mismatches'])}"
    )

    result = repair_link_integrity(frs, milestones, project=None, dry_run=args.dry_run)
    print(
        f"[{stamp}] repair: repaired={len(result['repaired'])} "
        f"skipped={len(result['skipped'])} total={result['total_mismatches']}"
    )
    for item in result["skipped"]:
        print(f"[{stamp}] skipped: {item}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
