from __future__ import annotations

from pathlib import Path

from backend.adc.skills import parse_skill, validate_skill_file, validate_skill_text

VALID_SKILL = """---
name: release-check
description: "Use when: preparing a CGA release"
version: 1.2.0
---

# Release Check

## When to Use

Before tagging a CGA release.

## Steps

1. Run the focused test suites.
2. Confirm version metadata matches.

## Validation

- All focused tests pass.
- README, backend and Relay versions are identical.
"""


def test_parse_skill_reads_metadata_triggers_steps_and_validation() -> None:
    skill = parse_skill(VALID_SKILL)

    assert skill["name"] == "release-check"
    assert skill["description"] == "Use when: preparing a CGA release"
    assert skill["version"] == "1.2.0"
    assert skill["when_to_use"] == "Before tagging a CGA release."
    assert skill["steps"] == ["Run the focused test suites.", "Confirm version metadata matches."]
    assert skill["validation"] == [
        "All focused tests pass.",
        "README, backend and Relay versions are identical.",
    ]
    assert validate_skill_text(VALID_SKILL) == []


def test_validate_skill_reports_missing_fields_and_sections() -> None:
    errors = validate_skill_text("---\nname: Bad Name\nversion: v1\n---\n\n## Steps\n\n")

    assert "name must be lowercase kebab-case" in errors
    assert "description is required" in errors
    assert "version must be SemVer (MAJOR.MINOR.PATCH)" in errors
    assert "'When to Use' section is required" in errors
    assert "'Steps' section must list at least one step" in errors
    assert "'Validation' section must list at least one check" in errors


def test_validate_skill_reports_missing_frontmatter_for_empty_file() -> None:
    assert validate_skill_text("") == ["frontmatter is required"]


def test_validate_skill_file_requires_name_to_match_folder(tmp_path: Path) -> None:
    skill_dir = tmp_path / "other-name"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(VALID_SKILL, encoding="utf-8")

    assert validate_skill_file(skill_file) == ["name 'release-check' must match folder 'other-name'"]
