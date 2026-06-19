from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DESKTOP = ROOT / "deploy" / "docker-desktop"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_desktop_launcher_prefers_prebuilt_image_tar_before_building() -> None:
    script = _read(DESKTOP / "start-desktop.ps1")

    assert "cga-desktop-api-image.tar" in script
    assert "Import-PrebuiltImage" in script
    assert "docker image load" in script
    assert "docker ps --format '{{.Ports}}'" in script
    assert "Get-NetTCPConnection" in script
    assert "Invoke-ComposeUp" in script
    assert "--build" in script
    assert "$UseBuild" in script
    assert "Wait-AdminHealth" in script
    assert "$WaitForReady" in script
    assert "Resolve-StartPort" in script
    assert "Requested port $requestedPort is unavailable" in script
    assert "Restore-DesktopLauncherEnv" in script
    assert "OriginalDesktopEnv" in script


def test_release_bundle_builds_prebuilt_api_image_tar() -> None:
    script = _read(DESKTOP / "build-release-bundle.ps1")

    assert "cga-desktop-api-image.tar" in script
    assert "docker build" in script
    assert "docker image save" in script
    assert "$imageName = 'cga-desktop-portable-cga'" in script
    assert "$localImageTag = \"$imageName`:local\"" in script
    assert "SkipImageBuild" in script


def test_portable_builder_copies_src_contents_at_image_expected_path() -> None:
    script = _read(DESKTOP / "build-portable-bundle.ps1")

    assert "Copy-SourceTreeExcludingGenerated" in script
    assert "'target'" in script
    assert "'node_modules'" in script
    assert "'dist'" in script
    assert "src\\scripts\\init_auth_db.py" in script


def test_portable_builder_includes_open_source_notice_files() -> None:
    script = _read(DESKTOP / "build-portable-bundle.ps1")

    for file_name in [
        "LICENSE",
        "NOTICE.md",
        "OPEN_SOURCE.md",
        "THIRD_PARTY_NOTICES.md",
        "DISCLAIMER.md",
        "SECURITY.md",
        "CONTRIBUTING.md",
        "CODE_OF_CONDUCT.md",
    ]:
        assert f"'{file_name}'" in script

    assert "## License And Notices" in script
    assert "Review these files before redistributing" in script
    assert "~~~powershell" in script


def test_desktop_readme_describes_one_click_release_path() -> None:
    readme = _read(DESKTOP / "README.md")

    assert "prebuilt CGA API image" in readme
    assert "Double-click `start-cga-desktop.cmd`" in readme
    assert "fallback" in readme
    assert "## License And Notices" in readme
    assert "THIRD_PARTY_NOTICES.md" in readme
    assert "no customer projects" in readme
    assert "does not import Nate Scott's local projects" in readme


def test_portable_builder_documents_clean_runtime_and_empty_repos_folder() -> None:
    script = _read(DESKTOP / "build-portable-bundle.ps1")

    assert "does not include Nate Scott's local projects" in script
    assert "PostgreSQL data" in script
    assert "FalkorDB graph indexes" in script
    assert "sample/demo" in script
    assert "project data" in script
    assert "does not ship Nate Scott's local project repositories or prebuilt index data" in script