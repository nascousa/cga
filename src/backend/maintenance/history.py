from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).parent


class ChangeNote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1, max_length=500)
    changes: dict[str, list[str]]


def version_tuple(value: str) -> tuple[int, int, int]:
    if not re.fullmatch(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", value):
        raise ValueError("Expected a stable semantic version.")
    return tuple(int(part) for part in value.removeprefix("v").split("."))


def pending_notes(root: Path = ROOT) -> dict:
    merged: dict[str, list[str]] = {}
    summaries = []
    for path in sorted((root / "changes").glob("*.json")):
        note = ChangeNote.model_validate_json(path.read_text(encoding="utf-8"))
        summaries.append(note.summary)
        if not note.changes or any(not entries or any(not entry.strip() for entry in entries)
                                   for entries in note.changes.values()):
            raise ValueError(f"{path.name}: non-empty user-facing changes are required.")
        for category, entries in note.changes.items():
            if category not in {"Added", "Changed", "Fixed", "Security", "Removed", "Deprecated", "Breaking"}:
                raise ValueError(f"{path.name}: unsupported change category {category}.")
            merged.setdefault(category, []).extend(entries)
    return {"version": "Unreleased", "date": None, "summary": "; ".join(summaries), "changes": merged}


def history(current_version: str, root: Path = ROOT) -> dict:
    releases = json.loads((root / "change-logs.json").read_text(encoding="utf-8"))
    seen = set()
    for release in releases:
        version_tuple(release["version"])
        if release["version"] in seen:
            raise ValueError("Duplicate published Change Logs version.")
        seen.add(release["version"])
        ChangeNote.model_validate({key: release[key] for key in ("summary", "changes")})
    releases.sort(key=lambda release: version_tuple(release["version"]), reverse=True)
    return {"current_version": current_version, "releases": releases, "unreleased": pending_notes(root),
            "history_note": "Older local versions are not reconstructed without verified release evidence."}


def validate_change_diff(data: dict, previous: list[dict], changed_paths: list[str]) -> None:
    current = {item["version"]: item for item in data["releases"]}
    previous_map = {item["version"]: item for item in previous}
    for version, release in previous_map.items():
        if current.get(version) != release:
            raise ValueError(f"Published Change Logs {version} must not be overwritten or removed.")
    product_changed = any(
        path.startswith(("src/backend/", "src/frontend/", "src/cga-relay/", "src/scripts/"))
        and "/tests/" not in path for path in changed_paths
    )
    fragment_changed = bool(data["unreleased"]["changes"]) and any(
        re.fullmatch(r"src/backend/maintenance/changes/[^/]+\.json", path) for path in changed_paths
    )
    if product_changed and not fragment_changed and not (current.keys() - previous_map.keys()):
        raise ValueError("Product changes require a changed user-facing fragment or a new frozen Change Logs version.")
