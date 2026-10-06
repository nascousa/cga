from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest


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
    assert "Release output already exists" in script
    assert "Remove-Item -Path $versionedFolder" not in script
    assert "Remove-Item -Path $zipPath" not in script


def test_launchers_check_persistence_before_recreating_containers() -> None:
    for path in (DESKTOP / "start-desktop.ps1", ROOT / "src" / "scripts" / "start-desktop.ps1"):
        script = _read(path)
        assert "Assert-CgaGraphPersistence -ComposeFile $composeFile" in script
        assert "desktop-graph-safety.ps1" in script
        assert "Invoke-Compose @('down')" not in script
        assert "Invoke-Compose @('stop')" in script


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


def _portable_compose() -> str:
    script = _read(DESKTOP / "build-portable-bundle.ps1")
    return script.split('$portableCompose = @"', 1)[1].split('"@', 1)[0].replace("$D", "$")


COMPOSE_PATHS = [
    ROOT / "docker-compose.yml",
    ROOT / "docker-compose.desktop.yml",
    ROOT / "docker-compose.release.yml",
    DESKTOP / "docker-compose.yml",
]


@pytest.mark.parametrize("path", COMPOSE_PATHS + [None], ids=lambda path: str(path or "portable"))
def test_compose_graph_volume_matches_explicit_persistence_directory(path: Path | None) -> None:
    compose = _read(path) if path else _portable_compose()
    graph_services = re.findall(
        r"^  (falkordb(?:-dev)?):\n(.*?)(?=^  \w|\Z)", compose, flags=re.MULTILINE | re.DOTALL
    )
    assert graph_services
    for _, block in graph_services:
        assert re.search(r"falkordb(?:_dev)?_data:/var/lib/falkordb/data", block)
        assert "falkordb_data:/data" not in block
        assert "REDIS_ARGS=" in block
        assert "--dir /var/lib/falkordb/data" in block
        assert "--dbfilename dump.rdb" in block
        assert "--save 60 1" in block
        assert "--stop-writes-on-bgsave-error yes" in block


@pytest.mark.parametrize("path", COMPOSE_PATHS + [None], ids=lambda path: str(path or "portable"))
def test_published_ports_default_to_explicit_loopback_bind(path: Path | None) -> None:
    compose = _read(path) if path else _portable_compose()
    port_blocks = re.findall(r"^    ports:\n((?:      .*\n)+)", compose, flags=re.MULTILINE)
    assert port_blocks
    for block in port_blocks:
        for line in block.strip().splitlines():
            assert re.search(r"\$\{CGA_(?:API|DB)_BIND_ADDRESS:-127\.0\.0\.1\}:", line), line


@pytest.mark.parametrize("path", COMPOSE_PATHS + [None], ids=lambda path: str(path or "portable"))
def test_backup_sidecar_uses_internal_graph_service_and_correct_volume(path: Path | None) -> None:
    compose = _read(path) if path else _portable_compose()
    backups = re.findall(
        r"^  backup(?:-dev)?:\n(.*?)(?=^  \w|^volumes:|\Z)",
        compose,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert backups
    for block in backups:
        assert re.search(r"FALKORDB_HOST=(?:cga-falkordb-dev|falkordb)", block)
        assert "FALKORDB_PORT=6379" in block
        assert "FALKORDB_SERVER_DATA_DIR=/var/lib/falkordb/data" in block
        assert "FALKORDB_DATA_DIR=/falkordb-data" in block
        assert "postgres:16-alpine" in block


def test_portable_generator_preserves_runtime_config_and_refuses_destructive_overwrite() -> None:
    script = _read(DESKTOP / "build-portable-bundle.ps1")
    compose = _portable_compose()
    assert "runtime_data:/app/data" in compose
    assert re.search(r"^  runtime_data:\s*$", compose, flags=re.MULTILINE)
    assert "Remove-Item -Path $portableRoot -Recurse -Force" not in script
    assert re.search(r"if \(Test-Path \$portableRoot\).*?throw", script, flags=re.DOTALL)
    assert "RUNTIME-OPERATIONS.md" in script
    assert "runtime-operations.md" in script


def test_portable_generator_refuses_existing_installation_without_deleting_data(tmp_path: Path) -> None:
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if not powershell:
        pytest.skip("An existing PowerShell is needed to execute the portable generator guard")
    output = tmp_path / "installed bundle"
    output.mkdir()
    sentinel = output / "runtime-state.txt"
    sentinel.write_text("keep installed user data", encoding="utf-8")

    result = subprocess.run(
        [
            powershell, "-NoProfile", "-File",
            str(DESKTOP / "build-portable-bundle.ps1"), "-OutputFolder", str(output),
        ],
        capture_output=True, text=True, timeout=30, check=False,
    )

    assert result.returncode != 0
    assert "Output folder already exists" in result.stderr
    assert sentinel.read_text(encoding="utf-8") == "keep installed user data"
    assert list(output.iterdir()) == [sentinel]