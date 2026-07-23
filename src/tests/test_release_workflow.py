from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_relay_checksums_use_flat_release_asset_names() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    assert "action-gh-release uploads each configured path using its basename" in workflow
    assert "cd relay-dist" in workflow
    assert 'sha256sum cga-relay.exe "cga-relay-${version}-windows-x64.zip"' in workflow
    assert "relay-dist/*" in workflow