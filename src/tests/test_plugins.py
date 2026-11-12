from __future__ import annotations

import importlib.util
import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest

from backend import runtime_config
from backend.auth.dependencies import get_current_user, require_admin
from backend.plugins import host
from backend.plugins import settings as plugin_settings
from backend.plugins.catalog import CATALOG
from backend.schedules.models import ScheduledTaskCreate
from backend.schedules.service import create_scheduled_task, execute_scheduled_task, get_scheduled_task

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def plugin_config(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_config, "RUNTIME_CONFIG_PATH", tmp_path / "runtime-config.json")
    monkeypatch.delenv("CGA_PLUGINS", raising=False)
    monkeypatch.delenv("CGA_AVAILABLE_PLUGINS", raising=False)
    return runtime_config.RUNTIME_CONFIG_PATH


def select(*plugins: str):
    return host.update_selection({"installed": list(plugins), "enabled": list(plugins)})


def test_catalog_exposes_pricing_independently_of_plugin_activation(plugin_config, monkeypatch):
    select("reports")
    monkeypatch.setitem(CATALOG, "reports", replace(CATALOG["reports"], pricing="paid"))
    host.update_selection({"installed": ["reports"], "enabled": []})
    catalog = {item["id"]: item for item in host.catalog_status()["items"]}
    assert catalog["reports"]["pricing"] == "paid"
    assert catalog["reports"]["installed"] is True
    assert catalog["reports"]["enabled"] is False
    assert catalog["briefing"]["pricing"] == "free"
    select("reports")
    assert host.is_enabled("reports"), "Pricing metadata must not invent an execution paywall"


@pytest.mark.parametrize("plugin_id", CATALOG)
def test_each_plugin_can_be_installed_disabled_and_removed_without_losing_settings(plugin_config, plugin_id):
    runtime_config.update_runtime_config({"query_policy": {"max_response_bytes": 4096}})
    select(plugin_id)
    assert host.is_enabled(plugin_id)
    host.update_selection({"installed": [plugin_id], "enabled": []})
    assert not host.is_enabled(plugin_id)
    assert host.selection()["installed"] == [plugin_id]
    with pytest.raises(host.PluginUnavailableError, match="disabled"):
        with host.execution(plugin_id):
            pytest.fail("A disabled plugin was executed")
    select()
    assert json.loads(plugin_config.read_text())["query_policy"]["max_response_bytes"] == 4096
    assert host.selection() == {"installed": [], "enabled": []}


@pytest.mark.parametrize("value", [
    {"installed": ["audit"], "enabled": []},
    {"installed": ["adc"], "enabled": []},
    {"installed": [], "enabled": ["reports"]},
    {"installed": ["reports", "reports"], "enabled": []},
    {"installed": [], "enabled": [], "unknown": True},
])
def test_invalid_selection_does_not_replace_persisted_selection(plugin_config, value):
    select("reports")
    before = plugin_config.read_bytes()
    with pytest.raises(runtime_config.RuntimeConfigError):
        host.update_selection(value)
    assert plugin_config.read_bytes() == before


def test_active_execution_blocks_uninstall_and_releases_after_failure(plugin_config):
    select("reports")
    with pytest.raises(RuntimeError, match="in-flight failure"):
        with host.execution("reports"):
            with pytest.raises(host.PluginUnavailableError, match="active executions"):
                select()
            raise RuntimeError("in-flight failure")
    select()
    assert not host.is_enabled("reports")


def test_minimal_initial_install_and_bundle_availability_are_explicit(plugin_config, monkeypatch):
    monkeypatch.setenv("CGA_PLUGINS", "reports")
    assert host.selection() == {"installed": ["reports"], "enabled": ["reports"]}
    monkeypatch.setenv("CGA_AVAILABLE_PLUGINS", "reports")
    with pytest.raises(runtime_config.RuntimeConfigError, match="distribution"):
        select("browseragent")
    select("reports")
    monkeypatch.setenv("CGA_PLUGINS", "")
    assert host.is_enabled("reports"), "Saved selection must win over the initial-install default"


@pytest.mark.parametrize("plugin_id,values", [
    ("browseragent", {"target_url": "https://browser.invalid/batch", "timeout_seconds": 91, "screenshot": False}),
    ("workflows", {"agent_url": "", "http_url": "https://workflow.invalid/run", "payload": {"action": "inspect"}}),
    ("briefing", {"project_id": "P1", "refresh_seconds": 0, "batch_size": 9}),
    ("evaluation", {"trace_enabled": False}),
])
def test_plugin_settings_persist_without_replacing_shared_state(plugin_config, plugin_id, values):
    select(plugin_id)
    runtime_config.update_runtime_config({"query_policy": {"max_response_bytes": 4096}})
    saved = plugin_settings.save_settings(plugin_id, values)
    assert all(saved[key] == value for key, value in values.items())
    select()
    select(plugin_id)
    assert plugin_settings.get_settings(plugin_id) == saved
    assert json.loads(plugin_config.read_text())["query_policy"]["max_response_bytes"] == 4096


@pytest.mark.parametrize("plugin_id,values", [
    ("browseragent", {"target_url": "https://user:DO-NOT-ECHO@browser.invalid/run"}),
    ("browseragent", {"target_url": "https://browser.invalid/run?token=DO-NOT-ECHO"}),
    ("browseragent", {"timeout_seconds": 0}),
    ("browseragent", {"target_url": ""}),
    ("browseragent", {"timeout_seconds": "30"}),
    ("workflows", {"payload": {"nested": [{"password": "DO-NOT-ECHO"}]}}),
    ("workflows", {"payload": {"message": "x" * 16385}}),
    ("briefing", {"refresh_seconds": -1}),
    ("briefing", {"batch_size": 26}),
    ("evaluation", {"trace_enabled": "false"}),
])
def test_invalid_plugin_settings_do_not_change_saved_configuration(plugin_config, plugin_id, values):
    select(plugin_id)
    before = plugin_config.read_bytes()
    with pytest.raises(ValueError):
        plugin_settings.save_settings(plugin_id, values)
    assert plugin_config.read_bytes() == before


def test_trace_setting_respects_operator_gate(plugin_config, monkeypatch):
    monkeypatch.setenv("TRACE_ENABLED", "true")
    assert plugin_settings.tracing_enabled()
    plugin_settings.save_settings("evaluation", {"trace_enabled": False})
    assert not plugin_settings.tracing_enabled()
    plugin_settings.save_settings("evaluation", {"trace_enabled": True})
    monkeypatch.setenv("TRACE_ENABLED", "false")
    assert not plugin_settings.tracing_enabled()


@pytest.mark.asyncio
async def test_disabled_installed_monitor_remains_discoverable_for_configuration(plugin_config):
    from backend.extensions.service import list_extensions
    host.update_selection({"installed": ["azure_policy_change_monitor"], "enabled": []})
    assert [item.extension_id for item in (await list_extensions()).items] == ["azure_policy_change_monitor"]
    select()
    assert (await list_extensions()).items == []


@pytest.mark.asyncio
async def test_plugin_settings_authorization_disabled_editing_and_safe_errors(plugin_config, auth_pg_pool):
    from fastapi import FastAPI
    from backend.plugins.router import router
    app = FastAPI()
    app.include_router(router, prefix="/api")
    select("browseragent", "notifications", "automation")
    host.update_selection({"installed": ["browseragent", "notifications", "automation"], "enabled": []})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/admin/plugins/browseragent/settings")).status_code == 401
        app.dependency_overrides[get_current_user] = lambda: {"id": 2, "role": "developer"}
        assert (await client.put("/api/admin/plugins/browseragent/settings", json={})).status_code == 403
        app.dependency_overrides[require_admin] = lambda: {"id": 1, "role": "admin"}
        result = await client.put("/api/admin/plugins/browseragent/settings", json={"timeout_seconds": 81})
        assert result.status_code == 200
        assert result.json()["enabled"] is False
        assert result.json()["values"]["timeout_seconds"] == 81
        rejected = await client.put("/api/admin/plugins/browseragent/settings",
                                    json={"target_url": "https://user:DO-NOT-ECHO@browser.invalid"})
        assert rejected.status_code == 400
        assert "DO-NOT-ECHO" not in rejected.text
        assert (await client.get("/api/admin/plugins/workflows/settings")).status_code == 409
        assert (await client.get("/api/admin/plugins/audit/settings")).status_code == 404
        smtp = await client.put("/api/admin/plugins/notifications/settings",
                               json={"host": "smtp.example.invalid", "from_name": "CGA"})
        assert smtp.status_code == 200
        assert runtime_config.get_runtime_config()["smtp"]["host"] == "smtp.example.invalid"
        assert "password" not in smtp.json()["values"]
        assert (await client.put("/api/admin/plugins/notifications/settings",
                                 json={"password": "DO-NOT-ECHO"})).status_code == 400
        class Worker:
            _task = asyncio.current_task()
        app.state.scheduled_task_worker = Worker()
        status = await client.get("/api/admin/plugins/automation/settings")
        assert status.json()["status"]["scheduler_running"] is True
        app.state.scheduled_task_worker = None
        assert (await client.get("/api/admin/plugins/automation/settings")).json()["status"]["scheduler_running"] is False


@pytest.mark.asyncio
async def test_reports_remain_independent_of_briefing_and_audit_stays_core(plugin_config, auth_pg_pool, monkeypatch):
    import backend.main as main
    select("reports")
    source = {"activities": [], "total_events": 0}
    service = type("ActivityService", (), {"get_briefing": AsyncMock(return_value=source)})()
    monkeypatch.setattr(main, "_work_briefing_service", service)
    def admin():
        return {"id": 1, "role": "admin"}
    main.app.dependency_overrides[require_admin] = admin
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            assert (await client.get("/api/admin/reports/source")).json() == source
            assert (await client.get("/api/admin/work-briefing")).status_code == 409
            assert (await client.get("/plugins/reports.js")).status_code == 200
            assert (await client.get("/plugins/briefing.js")).status_code == 409
            assert (await client.get("/plugins//reports.js")).status_code == 404
            host.update_selection({"installed": ["reports"], "enabled": []})
            assert (await client.get("/plugins/reports.js")).status_code == 200
            assert (await client.get("/api/admin/reports/source")).status_code == 409
            select()
            assert (await client.get("/api/admin/reports/source")).status_code == 409
            assert (await client.get("/plugins/reports.js")).status_code == 409
            assert (await client.get("/plugins//reports.js")).status_code == 404
            assert (await client.get("/api/auth/audit")).status_code == 200
            assert (await client.get("/health")).status_code == 200
            assert (await client.get("/api/admin/plugins")).json()["core"][:2] == ["adc", "audit"]
    finally:
        main.app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_plugin_selection_requires_real_admin_permission(plugin_config, auth_pg_pool):
    import backend.main as main
    main.app.dependency_overrides[get_current_user] = lambda: {"id": 2, "role": "developer"}
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            for method in ("GET", "PUT"):
                response = await client.request(method, "/api/admin/plugins", json={"installed": [], "enabled": []})
                assert response.status_code == 403
            main.app.dependency_overrides.clear()
            assert (await client.get("/api/admin/plugins")).status_code == 401
    finally:
        main.app.dependency_overrides.clear()
    assert not plugin_config.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("task_type,plugin_id,payload", [
    ("browseragent_task", "browseragent", {"id": "browser-command"}),
    ("agent_activation", "workflows", {"action": "inspect"}),
    ("http_request", "workflows", {"action": "inspect"}),
    ("extension_task", "azure_policy_change_monitor", {"extension_id": "azure_policy_change_monitor"}),
])
async def test_uninstalled_adapter_blocks_execution_but_retains_task_and_run_history(
    plugin_config, auth_pg_pool, monkeypatch, task_type, plugin_id, payload,
):
    select(plugin_id)
    async with auth_pg_pool.acquire() as db:
        task = await create_scheduled_task(db, ScheduledTaskCreate(
            name="Plugin isolation task", task_type=task_type, payload=payload,
            target_url="http://adapter.invalid/command",
        ))
        select()
        network = AsyncMock(side_effect=AssertionError("Uninstalled adapter contacted the network"))
        monkeypatch.setattr(httpx.AsyncClient, "post", network)
        run = await execute_scheduled_task(db, task)
        assert run.status == "failed"
        assert run.status_code == 409
        assert plugin_id in run.error
        retained = await get_scheduled_task(db, task.id)
        assert retained.id == task.id
        assert retained.payload == payload
        assert retained.last_run_status == "failed"
        select(plugin_id)
        assert (await get_scheduled_task(db, task.id)).id == task.id


@pytest.mark.asyncio
async def test_scheduler_runs_selected_adapter_without_automation_application(plugin_config, auth_pg_pool, monkeypatch):
    select("workflows")
    monkeypatch.setattr(httpx.AsyncClient, "post", AsyncMock(return_value=httpx.Response(200, json={"ran": True})))
    async with auth_pg_pool.acquire() as db:
        task = await create_scheduled_task(db, ScheduledTaskCreate(
            name="Independent workflow", task_type="http_request", target_url="http://workflow.invalid/run",
        ))
        run = await execute_scheduled_task(db, task)
    assert run.status == "success"
    assert run.response == {"ran": True}
    assert not host.is_enabled("automation")


@pytest.mark.asyncio
async def test_plugin_task_listing_filters_before_limit_and_ignores_unknown_extensions(plugin_config, auth_pg_pool):
    from backend.plugins.router import list_plugin_tasks
    select("workflows", "azure_policy_change_monitor")
    async with auth_pg_pool.acquire() as db:
        for name in ("First", "Latest"):
            task = await create_scheduled_task(db, ScheduledTaskCreate(
                name=name, task_type="http_request", target_url="http://workflow.invalid/run",
            ))
        foreign = await create_scheduled_task(db, ScheduledTaskCreate(
            name="Historical extension", task_type="extension_task",
            payload={"extension_id": "azure_policy_change_monitor"},
        ))
        await db.execute("UPDATE scheduled_tasks SET payload_json = ? WHERE id = ?",
                         ('{"extension_id":"retired_extension"}', foreign.id))
        tasks = await list_plugin_tasks("workflows", limit=1, db=db)
        assert [item.id for item in tasks] == [task.id]
        assert await list_plugin_tasks("azure_policy_change_monitor", limit=1, db=db) == []


@pytest.mark.asyncio
async def test_unknown_extension_keeps_not_found_semantics_without_plugins(plugin_config, auth_pg_pool):
    from backend.extensions.service import ExtensionNotFoundError, run_project_extension
    select()
    async with auth_pg_pool.acquire() as db:
        with pytest.raises(ExtensionNotFoundError):
            await run_project_extension(db, "missing_extension")


@pytest.mark.asyncio
async def test_mcp_hides_uninstalled_tools_but_keeps_graph_memory_and_adc(plugin_config):
    from backend.tools.server import mcp
    select()
    disabled = {tool.name for tool in await mcp.list_tools()}
    assert "query_graph" in disabled
    assert "workassist_get_memory_context" in disabled
    assert any("adc" in name for name in disabled)
    assert "run_eval" not in disabled
    assert "workassist_get_activity_briefing" not in disabled
    select("evaluation", "briefing")
    enabled = {tool.name for tool in await mcp.list_tools()}
    assert {"run_eval", "benchmark_token_efficiency", "workassist_get_activity_briefing"} <= enabled


def test_disabled_notifications_do_not_load_or_send_delivery(plugin_config, monkeypatch):
    from backend.extensions.registry import deliver_extension_notifications
    select("azure_policy_change_monitor")
    with pytest.raises(host.PluginUnavailableError, match="notifications"):
        deliver_extension_notifications("azure_policy_change_monitor", {}, {}, smtp_config={})


@pytest.mark.asyncio
@pytest.mark.parametrize("pause_source", ["plugin", "settings"])
async def test_evaluator_resumes_claimed_batch_after_plugin_is_disabled(plugin_config, monkeypatch, pause_source):
    from backend.eval.trace_eval import TraceEvaluator
    select("evaluation")
    evaluator = TraceEvaluator("redis://unused", None)
    evaluator._running = True
    processed = []

    async def read(*args, **kwargs):
        if pause_source == "plugin":
            select()
        else:
            plugin_settings.save_settings("evaluation", {"trace_enabled": False})
        return [("stream", [("1", {}), ("2", {})])]

    async def resume(_delay):
        select("evaluation")
        plugin_settings.save_settings("evaluation", {"trace_enabled": True})

    async def evaluate(message_id, _data):
        processed.append(message_id)
        if len(processed) == 2:
            evaluator._running = False

    evaluator._client = AsyncMock()
    evaluator._client.xreadgroup.side_effect = read
    monkeypatch.setattr("backend.eval.trace_eval.asyncio.sleep", resume)
    monkeypatch.setattr(evaluator, "_evaluate_trace", evaluate)
    await asyncio.wait_for(evaluator._loop(), timeout=1)
    assert processed == ["1", "2"]
    evaluator._client.xreadgroup.assert_awaited_once()


@pytest.mark.parametrize("plugins", ["", *CATALOG])
def test_selected_distribution_imports_without_omitted_implementations(tmp_path, plugins):
    bundle = tmp_path / "bundle"
    for relative in ("src/backend", "src/frontend", "extensions/azure-policy-monitor/src"):
        shutil.copytree(ROOT / relative, bundle / relative, ignore=shutil.ignore_patterns("__pycache__", "report-vendor"))
    # Generated bundles are untracked; this test exercises distribution selection, not the asset build.
    report_assets = bundle / "src/frontend/report-vendor"
    report_assets.mkdir()
    report_marker = report_assets / "packaging-test.txt"
    report_marker.write_text("Synthetic report asset", encoding="utf-8")
    spec = importlib.util.spec_from_file_location("package_plugins", ROOT / "src/scripts/package-plugins.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    selected = module.package_plugins(bundle, plugins)
    assert report_marker.exists() == ("reports" in selected)
    for plugin_id, plugin in CATALOG.items():
        for resource in plugin.resources:
            assert (bundle / resource).exists() == (plugin_id in selected)
    env = {**os.environ, "PYTHONPATH": str(bundle / "src"), "CGA_RUNTIME_CONFIG_PATH": str(bundle / "runtime-config.json")}
    env.pop("CGA_PLUGINS", None)
    env.pop("CGA_AVAILABLE_PLUGINS", None)
    result = subprocess.run(
        [sys.executable, "-c", """
import asyncio, json, sys
from fastapi.testclient import TestClient
from backend.main import app
from backend.plugins.host import selection
from backend.tools.server import mcp
assert TestClient(app).get('/health').status_code == 200
selected = json.loads(sys.argv[1])
assert selection()['enabled'] == selected
assert not any(name.startswith('backend.perf.') for name in sys.modules)
if 'browseragent' in selected:
    from backend.schedules.models import ScheduledTaskOut
    from backend.schedules.service import build_request_payload
    task = ScheduledTaskOut(id=1, task_id='TASK', name='Browser', task_type='browseragent_task',
        payload={'id':'command'}, cadence_minutes=1, timeout_seconds=5, enabled=True,
        created_at='', updated_at='', next_run_at='')
    assert build_request_payload(task) == {'id':'command'}
    assert 'backend.plugins.workflows' not in sys.modules
tools = {tool.name for tool in asyncio.run(mcp.list_tools())}
assert 'query_graph' in tools
assert ('run_eval' in tools) == ('evaluation' in selected)
""", json.dumps(selected)],
        cwd=bundle, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
