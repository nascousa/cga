#!/usr/bin/env python3
"""Validate versioned ADC skills (SKILL.md) under a directory.

Usage:
    python src/scripts/validate_skills.py [root]

Exits with status 1 when any skill fails validation.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.adc.skills import validate_skill_file  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", nargs="?", default=".adc", help="directory to scan (default: .adc)")
    args = parser.parse_args(argv)

    root = Path(args.root)
    results = {path.as_posix(): validate_skill_file(path) for path in sorted(root.rglob("SKILL.md"))}
    print(json.dumps({"skills": len(results), "results": results}, indent=2))
    return 1 if any(results.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
