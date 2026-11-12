from datetime import date
import json
from pathlib import Path

import pytest

from scripts.bump_version import bump_version, version_files


def version_fixture(root: Path) -> None:
    files = {
        "src/backend/main.py": 'APP_VERSION = "1.30.128"\r\nOTHER = "unchanged"\r\n',
        "src/cga-relay/Cargo.toml": '[package]\nname = "cga-relay"\nversion = "1.30.128"\n',
        "src/cga-relay/Cargo.lock": (
            'version = 4\n\n[[package]]\nname = "cga-relay"\nversion = "1.30.128"\n'
        ),
        "README.md": "- **Version:** 1.30.128\n- **Date:** 2026-01-01 (status)\n",
        "src/frontend/index.html": (
            '<script src="/adc.js?v=1.30.128-local"></script>\n'
            '<script src="/adc-versions.js?v=1.30.128-local"></script>\n'
            '<script src="/adc-collection.js?v=1.30.128-local"></script>\n'
            '<script src="/maintenance.js?v=1.30.128"></script>\n'
            '<script src="/report-workspace.js?v=1.30.128-report.1"></script>\n'
            '<link rel="stylesheet" href="/report-workspace.css?v=1.30.128-report.1">\n'
        ),
        "src/backend/maintenance/change-logs.json": "[]",
        "src/backend/maintenance/changes/test.json": json.dumps({
            "summary": "Test change", "changes": {"Fixed": ["Correct behavior"]},
        }),
    }
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode())


def test_patch_increment_aligns_metadata_and_preserves_other_content(tmp_path: Path) -> None:
    version_fixture(tmp_path)
    assert bump_version(tmp_path) == "1.30.129"
    assert version_files(tmp_path)[0] == "1.30.129"
    assert (tmp_path / "src/backend/main.py").read_bytes() == (
        b'APP_VERSION = "1.30.129"\r\nOTHER = "unchanged"\r\n'
    )
    assert f"- **Date:** {date.today().isoformat()} (status)" in (
        tmp_path / "README.md"
    ).read_text()
    assert 'adc.js?v=1.30.129-local' in (tmp_path / "src/frontend/index.html").read_text()
    assert (tmp_path / "src/backend/maintenance/changes/archive/1.30.129/test.json").exists()
    (tmp_path / "src/backend/maintenance/changes/next.json").write_text(json.dumps({
        "summary": "Next change", "changes": {"Added": ["Next behavior"]},
    }))
    assert bump_version(tmp_path) == "1.30.130"


@pytest.mark.parametrize("broken_file", ["src/cga-relay/Cargo.lock", "README.md"])
def test_invalid_metadata_is_rejected_before_writing(tmp_path: Path, broken_file: str) -> None:
    version_fixture(tmp_path)
    path = tmp_path / broken_file
    text = path.read_text()
    path.write_text(
        text.replace("1.30.128", "1.30.127")
        if broken_file.endswith("lock")
        else text.replace("- **Date:**", "- Updated:")
    )
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    with pytest.raises(ValueError):
        bump_version(tmp_path)
    assert {path: path.read_bytes() for path in before} == before
