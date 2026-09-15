from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")
pytestmark = pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is not installed")


@pytest.mark.parametrize(
    ("mount", "directory", "success"),
    [
        ("/data", "/var/lib/falkordb/data", False),
        ("/var/lib/falkordb/data", "/tmp", False),
        ("/var/lib/falkordb/data", "/var/lib/falkordb/data", True),
    ],
)
def test_migration_guard_uses_existing_container_metadata(mount, directory, success):
    helper = str(ROOT / "src" / "scripts" / "desktop-graph-safety.ps1").replace("'", "''")
    command = f"""
$ErrorActionPreference = 'Stop'
. '{helper}'
function docker {{
    $global:LASTEXITCODE = 0
    if ($args[0] -eq 'compose') {{ return 'isolated-fixture' }}
    if ($args[0] -eq 'inspect' -and $args[2] -like '*Mounts*') {{
        return '[{{"Type":"volume","Destination":"{mount}","RW":true}}]'
    }}
    if ($args[0] -eq 'inspect') {{ return 'true' }}
    if ($args[0] -eq 'exec') {{ return @('dir', '{directory}') }}
    throw 'Unexpected command; tests must never call Docker'
}}
try {{
    Assert-CgaGraphPersistence -ComposeFile 'fixture.yml'
    Write-Output 'PERSISTENCE_OK'
}} catch {{
    Write-Output $_.Exception.Message
    exit 1
}}
"""
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert (result.returncode == 0) == success, result.stdout + result.stderr
    if success:
        assert "PERSISTENCE_OK" in result.stdout
