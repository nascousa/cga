from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "src" / "scripts"

DESKTOP_DRIVER = r"""
$ErrorActionPreference = 'Stop'
function global:docker {
    @{kind='docker'; args=@($args)} | ConvertTo-Json -Compress |
        Add-Content -LiteralPath $env:CGA_TEST_LOG -Encoding UTF8
    $global:LASTEXITCODE = 0
    if ($args[0] -eq 'inspect') {
        if ($env:CGA_TEST_MODE -eq 'inspect-failure') {
            $global:LASTEXITCODE = 9
            return
        }
        if ($args -contains '{{json .Mounts}}') {
            $directory = if ($env:CGA_TEST_MODE -eq 'unsafe-mount') { '/data' } else { '/var/lib/falkordb/data' }
            @(@{Type='volume'; RW=$true; Destination=$directory}) |
                ConvertTo-Json -Compress
        } else { 'true' }
    } elseif ($args[0] -eq 'exec') {
        'dir'
        if ($env:CGA_TEST_MODE -eq 'wrong-directory') { '/data' } else { '/var/lib/falkordb/data' }
    } elseif ($args -contains 'port') {
        $env:CGA_TEST_BINDING
    } elseif ($args -contains 'ps') {
        'isolated-test-graph'
    } elseif ($args -contains $env:CGA_TEST_FAIL_ACTION) {
        $global:LASTEXITCODE = 7
    }
}
function global:Start-Process {
    param([string]$FilePath)
    @{kind='browser'; url=$FilePath} | ConvertTo-Json -Compress |
        Add-Content -LiteralPath $env:CGA_TEST_LOG -Encoding UTF8
}
& $env:CGA_TEST_SCRIPT -Command $env:CGA_TEST_ACTION -OpenBrowser $true `
    -Detached ($env:CGA_TEST_DETACHED -ne 'false')
exit $LASTEXITCODE
"""

RELAY_DRIVER = r"""
$ErrorActionPreference = 'Stop'
Set-Item -LiteralPath ("Function:\global:" + $env:CGA_TEST_EXE) -Value {
    @{kind='relay'; args=@($args); api=$env:CGA_TEST_API_KEY; account=$env:CGA_TEST_ACCOUNT_TOKEN} |
        ConvertTo-Json -Compress | Add-Content -LiteralPath $env:CGA_TEST_LOG -Encoding UTF8
    $global:LASTEXITCODE = [int]$env:CGA_TEST_RELAY_EXIT
}
& $env:CGA_TEST_SCRIPT
exit $LASTEXITCODE
"""


@contextmanager
def _health_endpoint(responses):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            status, body = responses[0]
            if len(responses) > 1:
                responses.pop(0)
            if status is None:
                self.close_connection = True
                return
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture(scope="session")
def health_port():
    with _health_endpoint([(200, b'{"status":"ok","service":"cga"}')]) as port:
        yield port


@pytest.fixture
def launcher_env(tmp_path: Path, health_port: int) -> tuple[str, Path, dict[str, str]]:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        pytest.skip("PowerShell is needed to execute workspace launcher regressions")
    root = tmp_path / "workspace with spaces"
    scripts = root / "src" / "scripts"
    scripts.mkdir(parents=True)
    for name in ("start-desktop.ps1", "desktop-graph-safety.ps1", "start-relay.ps1"):
        shutil.copyfile(SCRIPTS / name, scripts / name)
    profile = tmp_path / "profile with spaces"
    profile.mkdir()
    env = {
        **os.environ,
        "USERPROFILE": str(profile),
        "CGA_TEST_LOG": str(tmp_path / "calls.jsonl"),
        "CGA_TEST_MODE": "",
        "CGA_TEST_FAIL_ACTION": "never-fail",
        "CGA_TEST_BINDING": f"127.0.0.1:{health_port}",
        "CGA_TEST_API_KEY": "",
        "CGA_TEST_ACCOUNT_TOKEN": "",
        "CGA_TEST_RELAY_EXIT": "0",
        "CGA_DESKTOP_API_PORT": str(health_port),
        "CGA_DESKTOP_FALKORDB_PORT": "19011",
        "CGA_DESKTOP_BROWSER_PORT": "19021",
    }
    return powershell, root, env


def _run(
    powershell: str, env: dict[str, str], driver: str
) -> tuple[subprocess.CompletedProcess[str], list[dict]]:
    result = subprocess.run(
        [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", driver],
        env=env, capture_output=True, text=True, timeout=30, check=False,
    )
    log = Path(env["CGA_TEST_LOG"])
    events = [json.loads(line) for line in log.read_text(encoding="utf-8-sig").splitlines()] if log.exists() else []
    return result, events


def _desktop(
    launcher_env: tuple[str, Path, dict[str, str]], recovered: bool, action: str
) -> tuple[subprocess.CompletedProcess[str], list[dict], Path]:
    powershell, root, env = launcher_env
    compose = root / "docker-compose.desktop.yml"
    if recovered:
        compose = Path(env["USERPROFILE"]) / ".nasco" / "docker" / "main" / "cga" / "compose.json"
        compose.parent.mkdir(parents=True)
        compose.write_text("{}", encoding="utf-8")
    env["CGA_TEST_SCRIPT"] = str(root / "src" / "scripts" / "start-desktop.ps1")
    env["CGA_TEST_ACTION"] = action
    result, events = _run(powershell, env, DESKTOP_DRIVER)
    return result, events, compose


@pytest.mark.parametrize("action", ["start", "stop", "logs", "open"])
def test_root_launchers_use_shared_deployment_selection(action: str) -> None:
    launcher = (ROOT / f"{action}-cga-desktop.cmd").read_text(encoding="utf-8")
    assert r".\src\scripts\start-desktop.ps1" in launcher
    assert any(f"start-desktop.ps1{quote} {action}" in launcher for quote in ("'", '"'))
    assert "docker compose" not in launcher


@pytest.mark.skipif(os.name != "nt", reason="Windows command wrappers")
@pytest.mark.parametrize("action", ["start", "stop", "logs", "open"])
@pytest.mark.parametrize("exit_code", [0, 7, None])
def test_root_wrappers_execute_powershell_arguments_and_preserve_exit_code(
    launcher_env, action: str, exit_code: int | None
) -> None:
    _, root, env = launcher_env
    launcher = root / f"{action}-cga-desktop.cmd"
    shutil.copyfile(ROOT / launcher.name, launcher)
    (root / "src" / "scripts" / "start-desktop.ps1").write_text(
        r"""
param([string]$Command, [bool]$Detached=$true, [bool]$OpenBrowser=$false)
@{action=$Command; detached=$Detached; browser=$OpenBrowser} | ConvertTo-Json -Compress |
    Add-Content -LiteralPath $env:CGA_TEST_LOG -Encoding UTF8
if ($env:CGA_TEST_RELAY_EXIT -eq 'throw') {
    $global:LASTEXITCODE = 0
    throw 'Test launcher refusal'
}
exit ([int]$env:CGA_TEST_RELAY_EXIT)
""",
        encoding="utf-8",
    )
    env["CGA_TEST_RELAY_EXIT"] = str(exit_code) if exit_code is not None else "throw"
    result = subprocess.run(
        [os.environ["COMSPEC"], "/d", "/c", str(launcher)],
        env=env, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == (exit_code if exit_code is not None else 1), result.stdout + result.stderr
    event = json.loads(Path(env["CGA_TEST_LOG"]).read_text(encoding="utf-8-sig"))
    assert event["action"] == action
    assert event["browser"] == (action == "start")
    assert event["detached"] == (action != "logs")


@pytest.mark.parametrize("recovered", [False, True])
@pytest.mark.parametrize("action", ["start", "restart", "stop", "logs", "open", "status", "config"])
def test_desktop_actions_target_same_deployment(launcher_env, recovered: bool, action: str) -> None:
    result, events, compose = _desktop(launcher_env, recovered, action)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = [event["args"] for event in events if event["kind"] == "docker"]
    assert calls
    assert all(call[2] == str(compose) for call in calls if call[0] == "compose")
    if action in ("start", "restart"):
        up_index = next(i for i, call in enumerate(calls) if "up" in call)
        assert any(call[0] == "inspect" for call in calls[:up_index])
        assert any(call[0] == "exec" and "CONFIG" in call for call in calls[:up_index])
        assert ("--no-build" in calls[up_index]) == recovered
        assert ("--build" in calls[up_index]) != recovered
        assert ("--wait" in calls[up_index]) == recovered
    if action in ("start", "restart", "open"):
        urls = [event["url"] for event in events if event["kind"] == "browser"]
        env = launcher_env[2]
        expected = (
            f"http://{env['CGA_TEST_BINDING']}/admin"
            if recovered else f"http://localhost:{env['CGA_DESKTOP_API_PORT']}/admin"
        )
        assert urls == [expected]
    if action == "restart":
        assert [call[-2:] for call in calls if "restart" in call] == [["restart", "cga"]]


@pytest.mark.parametrize(
    ("status", "body", "healthy"),
    [
        (200, b'{"status":"ok","service":"cga"}', True),
        (200, b'{"status":"ok","service":"unrelated"}', False),
        (200, b'{"status":"failed","service":"cga"}', False),
        (200, b'{}', False),
        (200, b'not-json', False),
        (503, b'{"status":"ok","service":"cga"}', False),
        (302, b'{}', False),
        (None, b'', False),
    ],
)
def test_host_health_requires_real_cga_response(launcher_env, status, body, healthy):
    powershell, _, env = launcher_env
    script = str(SCRIPTS / "start-desktop.ps1").replace("'", "''")
    with _health_endpoint([(status, body)]) as port:
        driver = f"""
$ErrorActionPreference = 'Stop'
$ast = [System.Management.Automation.Language.Parser]::ParseFile('{script}', [ref]$null, [ref]$null)
$function = $ast.Find({{param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq 'Wait-CgaDesktopHealth'
}}, $true)
Invoke-Expression $function.Extent.Text
try {{
    Wait-CgaDesktopHealth -AdminUrl 'http://127.0.0.1:{port}/admin' -TimeoutSeconds 0
    Write-Output 'HOST_HEALTH_OK'
}} catch {{
    Write-Error $_ -ErrorAction Continue
    exit 1
}}
"""
        result, _ = _run(powershell, env, driver)
    assert (result.returncode == 0) == healthy, result.stdout + result.stderr
    if healthy:
        assert "HOST_HEALTH_OK" in result.stdout
    else:
        assert "CGA host HTTP check failed" in result.stderr


def test_status_rejects_container_only_health(launcher_env):
    with _health_endpoint([(503, b'{}')]) as port:
        launcher_env[2]["CGA_TEST_BINDING"] = f"127.0.0.1:{port}"
        result, events, _ = _desktop(launcher_env, True, "status")
    assert result.returncode != 0
    assert "CGA host HTTP check failed" in result.stderr
    assert not any(event["kind"] == "browser" for event in events)


@pytest.mark.parametrize("recovered", [False, True])
def test_foreground_start_does_not_probe_after_compose_exits(launcher_env, recovered):
    env = launcher_env[2]
    env["CGA_TEST_DETACHED"] = "false"
    env["CGA_TEST_BINDING"] = "127.0.0.1:1"
    env["CGA_DESKTOP_API_PORT"] = "1"
    result, events, _ = _desktop(launcher_env, recovered, "start")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not any(event["kind"] == "browser" for event in events)


@pytest.mark.parametrize("recovered", [False, True])
@pytest.mark.parametrize("action", ["start", "restart"])
@pytest.mark.parametrize("failure", ["unsafe-mount", "wrong-directory", "inspect-failure"])
def test_desktop_refuses_recreation_when_graph_safety_fails(
    launcher_env, recovered: bool, action: str, failure: str
) -> None:
    launcher_env[2]["CGA_TEST_MODE"] = failure
    result, events, _ = _desktop(launcher_env, recovered, action)
    assert result.returncode != 0
    assert "FalkorDB" in result.stderr
    assert not any("up" in event.get("args", []) for event in events)
    assert not any(event["kind"] == "browser" for event in events)


@pytest.mark.parametrize("recovered", [False, True])
@pytest.mark.parametrize("action", ["start", "stop", "logs"])
def test_desktop_preserves_real_powershell_failure_exit_code(launcher_env, recovered: bool, action: str) -> None:
    launcher_env[2]["CGA_TEST_FAIL_ACTION"] = "up" if action == "start" else action
    result, events, compose = _desktop(launcher_env, recovered, action)
    assert result.returncode == 7, result.stdout + result.stderr
    assert "exit code 7" in result.stderr
    assert not any(event["kind"] == "browser" for event in events)
    assert all(
        event["args"][2] == str(compose)
        for event in events if event["kind"] == "docker" and event["args"][0] == "compose"
    )


@pytest.mark.parametrize(
    ("binding", "url"),
    [
        ("0.0.0.0:19100", "http://localhost:19100/admin"),
        ("[::]:19101", "http://localhost:19101/admin"),
        ("[::1]:19102", "http://[::1]:19102/admin"),
        ("10.0.0.1:19103", "http://10.0.0.1:19103/admin"),
    ],
)
def test_recovered_admin_uses_published_binding(launcher_env, binding: str, url: str) -> None:
    launcher_env[2]["CGA_TEST_BINDING"] = binding
    result, events, _ = _desktop(launcher_env, True, "open")
    assert result.returncode == 0, result.stdout + result.stderr
    assert [event["url"] for event in events if event["kind"] == "browser"] == [url]


def _relay(launcher_env, *, env_text: str | None, inherited: str = "", exit_code: int = 0):
    powershell, root, env = launcher_env
    config = Path(env["USERPROFILE"]) / ".cga" / "relay.env"
    config.parent.mkdir()
    config.write_text("API_KEY_ENV=CGA_TEST_API_KEY\nACCOUNT_TOKEN_ENV=CGA_TEST_ACCOUNT_TOKEN\n", encoding="utf-8")
    executable = config.parent / "bin" / "cga-relay.exe"
    executable.parent.mkdir()
    executable.touch()
    if env_text is not None:
        (root / ".env").write_text(env_text, encoding="utf-8")
    env.update(
        CGA_TEST_SCRIPT=str(root / "src" / "scripts" / "start-relay.ps1"),
        CGA_TEST_EXE=str(executable),
        CGA_TEST_API_KEY=inherited,
        CGA_TEST_RELAY_EXIT=str(exit_code),
    )
    return _run(powershell, env, RELAY_DRIVER)


@pytest.mark.parametrize("env_text", [None, "UNRELATED=value\n", "CGA_TEST_API_KEY=\n"])
def test_relay_allows_stored_account_session_without_checkout_token(launcher_env, env_text: str | None) -> None:
    result, events = _relay(launcher_env, env_text=env_text)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(events) == 1
    assert events[0]["args"][0:2] == ["tray", "--config"]
    assert not events[0]["api"]
    assert not events[0]["account"]


@pytest.mark.parametrize("inherited", ["", "inherited-test-credential"])
def test_relay_uses_configured_variable_names_without_overwriting_environment(launcher_env, inherited: str) -> None:
    result, events = _relay(
        launcher_env,
        env_text='CGA_TEST_API_KEY="checkout-test-credential"\nCGA_TEST_ACCOUNT_TOKEN=account-test-credential\n',
        inherited=inherited,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert events[0]["api"] == (inherited or "checkout-test-credential")
    assert events[0]["account"] == "account-test-credential"
    assert "test-credential" not in result.stdout + result.stderr


def test_relay_failure_is_not_reported_as_success(launcher_env) -> None:
    result, _ = _relay(launcher_env, env_text=None, exit_code=7)
    assert result.returncode == 7
    assert "exited with code 7" in result.stderr
