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
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from developer.config import Config
from developer.pipeline import Pipeline
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
    pipeline = Pipeline.from_config(cfg)

    before = audit_link_integrity(pipeline.frs, pipeline.milestones, project=None)
    print(
        f"[{stamp}] audit: checked_frs={before['checked_frs']} "
        f"checked_milestones={before['checked_milestones']} "
        f"mismatches={len(before['mismatches'])}"
    )

    result = repair_link_integrity(
        pipeline.frs, pipeline.milestones, project=None, dry_run=args.dry_run,
    )
    print(
        f"[{stamp}] repair: repaired={len(result['repaired'])} "
        f"skipped={len(result['skipped'])} total={result['total_mismatches']}"
    )
    for item in result["skipped"]:
        print(f"[{stamp}] skipped: {item}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
