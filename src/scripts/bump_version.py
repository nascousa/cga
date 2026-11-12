"""Increment CGA's patch version and keep product metadata aligned."""

import argparse
import json
import sys
from datetime import date
from pathlib import Path
import re


VERSION = r"(?P<version>\d+\.\d+\.\d+)"
FIELDS = {
    "src/backend/main.py": (rf'^APP_VERSION = "{VERSION}"', 1),
    "src/cga-relay/Cargo.toml": (rf'^version = "{VERSION}"', 1),
    "src/cga-relay/Cargo.lock": (
        rf'^\[\[package\]\]\r?\nname = "cga-relay"\r?\nversion = "{VERSION}"',
        1,
    ),
    "README.md": (rf"^- \*\*Version:\*\* {VERSION}", 1),
    "src/frontend/index.html": (
        rf'(?:<script src="/(?:adc(?:-versions|-collection)?|maintenance|report-workspace)\.js'
        rf'|<link rel="stylesheet" href="/report-workspace\.css)\?v={VERSION}',
        6,
    ),
}


def version_files(root: Path) -> tuple[str, dict[Path, tuple[str, re.Pattern[str]]]]:
    files = {}
    versions: set[str] = set()
    for relative, (pattern, expected) in FIELDS.items():
        path = root / relative
        text = path.read_bytes().decode("utf-8")
        compiled = re.compile(pattern, re.MULTILINE)
        matches = list(compiled.finditer(text))
        if len(matches) != expected:
            raise ValueError(f"{relative}: expected {expected} version fields, found {len(matches)}")
        versions.update(match["version"] for match in matches)
        files[path] = (text, compiled)
    if len(versions) != 1:
        raise ValueError(f"Version metadata is inconsistent: {', '.join(sorted(versions))}")
    return versions.pop(), files


def bump_version(root: Path) -> str:
    current, files = version_files(root)
    major, minor, patch = (int(part) for part in current.split("."))
    updated = f"{major}.{minor}.{patch + 1}"
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from backend.maintenance.history import history

    notes_root = root / "src/backend/maintenance"
    notes = history(current, notes_root)
    pending = notes["unreleased"]
    if not pending["changes"]:
        raise ValueError("Add a user-facing Change Logs fragment before incrementing a product iteration.")
    if any(item["version"] == updated for item in notes["releases"]):
        raise ValueError("The target Change Logs version already exists; published history cannot be overwritten.")
    replacements = {}
    for path, (text, pattern) in files.items():
        text = pattern.sub(
            lambda match: (
                match[0][:match.start("version") - match.start()]
                + updated
                + match[0][match.end("version") - match.start():]
            ),
            text,
        )
        if path.name == "README.md":
            text, count = re.subn(
                r"(^- \*\*Date:\*\* )\d{4}-\d{2}-\d{2}",
                rf"\g<1>{date.today().isoformat()}",
                text,
                flags=re.MULTILINE,
            )
            if count != 1:
                raise ValueError("README.md: expected one release date")
        replacements[path] = text
    pending.update(version=updated, date=date.today().isoformat())
    replacements[notes_root / "change-logs.json"] = json.dumps(
        [pending, *notes["releases"]], indent=2, ensure_ascii=True,
    ) + "\n"
    # Keep every original fragment as release evidence instead of overwriting it.
    fragments = list((notes_root / "changes").glob("*.json"))
    archive = notes_root / "changes" / "archive" / updated
    if archive.exists():
        raise ValueError("Change Logs archive already exists for the target version.")
    for path, text in replacements.items():
        path.write_bytes(text.encode("utf-8"))
    archive.mkdir(parents=True)
    for path in fragments:
        path.rename(archive / path.name)
    return updated


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check alignment without incrementing")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    try:
        if args.check:
            print(f"CGA version metadata aligned: {version_files(root)[0]}")
        else:
            print(f"CGA patch version incremented: {bump_version(root)}")
    except (OSError, UnicodeError, ValueError) as error:
        parser.exit(1, f"Version update failed: {error}\n")


if __name__ == "__main__":
    main()
