#!/usr/bin/env python3
"""Cold-start import of repository documents into Work Briefing memory.

Dry-run by default: prints the planned summaries and skipped files without writing.
Pass --apply to upsert them (idempotent by `doc:<path>` external ID). The database
DSN comes from the WORKBRIEFING_POSTGRES_DSN environment variable.

Usage:
    python src/scripts/memory_cold_start.py --project-id <id> [--root .] [--apply]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.workbriefing.cold_start import apply_cold_start, build_cold_start_plan  # noqa: E402
from backend.workbriefing.service import WorkBriefingService  # noqa: E402
from backend.workbriefing.store import PgVectorActivityStore  # noqa: E402


async def _apply(plan) -> dict[str, int]:
    store = PgVectorActivityStore()
    try:
        return await apply_cold_start(WorkBriefingService(store=store), plan)
    finally:
        await store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cold-start import of documents into Work Briefing memory.")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--root", default=".")
    parser.add_argument("--apply", action="store_true", help="write to the database (default: dry run)")
    args = parser.parse_args(argv)

    plan = build_cold_start_plan(Path(args.root), project_id=args.project_id)
    report: dict = {
        "dry_run": not args.apply,
        "planned": [{"external_id": p["external_id"], "title": p["title"]} for p in plan.payloads],
        "skipped": plan.skipped,
    }
    if args.apply:
        report["operations"] = asyncio.run(_apply(plan))
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
