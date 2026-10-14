"""Validation for versioned ADC skills (SKILL.md).

A skill declares frontmatter (name, description, version) plus three body sections:
"When to Use" (trigger boundary), "Steps" (ordered actions) and "Validation" (success checks).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*\S)\s*$")
_SECTIONS = {"when to use": "when_to_use", "steps": "steps", "validation": "validation"}


def _split_frontmatter(text: str) -> tuple[dict[str, str] | None, str]:
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        return None, text
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            metadata: dict[str, str] = {}
            for raw in lines[1:index]:
                if ":" not in raw or raw.lstrip().startswith("#"):
                    continue
                key, value = raw.split(":", 1)
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                    value = value[1:-1]
                metadata[key.strip()] = value
            return metadata, "\n".join(lines[index + 1 :])
    return None, text


def _sections(body: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in body.split("\n"):
        heading = re.match(r"^##\s+(.+?)\s*$", line)
        if heading:
            current = _SECTIONS.get(heading.group(1).strip().lower())
            if current:
                sections.setdefault(current, [])
            continue
        if current:
            sections[current].append(line)
    return sections


def parse_skill(text: str) -> dict[str, Any]:
    metadata, body = _split_frontmatter(text)
    sections = _sections(body)

    def _items(key: str) -> list[str]:
        return [match.group(1) for line in sections.get(key, []) if (match := _LIST_ITEM_RE.match(line))]

    return {
        "has_frontmatter": metadata is not None,
        "name": (metadata or {}).get("name", ""),
        "description": (metadata or {}).get("description", ""),
        "version": (metadata or {}).get("version", ""),
        "has_when_to_use": "when_to_use" in sections,
        "when_to_use": " ".join(" ".join(sections.get("when_to_use", [])).split()),
        "has_steps": "steps" in sections,
        "steps": _items("steps"),
        "has_validation": "validation" in sections,
        "validation": _items("validation"),
    }


def validate_skill_text(text: str) -> list[str]:
    skill = parse_skill(text)
    if not skill["has_frontmatter"]:
        return ["frontmatter is required"]
    errors: list[str] = []
    if not skill["name"]:
        errors.append("name is required")
    elif not _NAME_RE.match(skill["name"]):
        errors.append("name must be lowercase kebab-case")
    if not skill["description"]:
        errors.append("description is required")
    if not _SEMVER_RE.match(skill["version"]):
        errors.append("version must be SemVer (MAJOR.MINOR.PATCH)")
    if not skill["when_to_use"]:
        errors.append("'When to Use' section is required")
    if not skill["steps"]:
        errors.append("'Steps' section must list at least one step")
    if not skill["validation"]:
        errors.append("'Validation' section must list at least one check")
    return errors


def validate_skill_file(path: Path) -> list[str]:
    text = Path(path).read_text(encoding="utf-8")
    errors = validate_skill_text(text)
    name = parse_skill(text)["name"]
    folder = Path(path).parent.name
    if name and name != folder:
        errors.append(f"name '{name}' must match folder '{folder}'")
    return errors
