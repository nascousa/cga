"""Validate bundled history and optionally require frozen release notes."""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.maintenance.history import history, validate_change_diff  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", action="store_true")
    parser.add_argument("--base", default=os.getenv("CGA_CHANGELOG_BASE", ""))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    source = (root / "src/backend/main.py").read_text(encoding="utf-8")
    version = re.search(r'^APP_VERSION = "([^"]+)"', source, re.MULTILINE).group(1)
    data = history(version)
    if args.base and set(args.base) != {"0"}:
        if not re.fullmatch(r"[0-9a-f]{40,64}", args.base):
            parser.error("Expected a complete base commit hash.")
        changed = subprocess.run(
            ["git", "diff", "--name-only", f"{args.base}...HEAD"], cwd=root,
            check=True, text=True, capture_output=True,
        ).stdout.splitlines()
        exists = subprocess.run(
            ["git", "ls-tree", "--name-only", args.base, "src/backend/maintenance/change-logs.json"],
            cwd=root, check=True, text=True, capture_output=True,
        ).stdout.strip()
        old = subprocess.run(
            ["git", "show", f"{args.base}:src/backend/maintenance/change-logs.json"], cwd=root,
            check=True, text=True, encoding="utf-8", capture_output=True,
        ).stdout if exists else "[]"
        validate_change_diff(data, json.loads(old), changed)
    if not any(item["version"] == version for item in data["releases"]):
        parser.exit(1, "Current version has no bundled Change Logs entry.\n")
    if args.release and data["unreleased"]["changes"]:
        parser.exit(1, "Unreleased change fragments remain. Increment the completed iteration before publishing.\n")
    print(f"Change Logs validated for {version}")


if __name__ == "__main__":
    main()
