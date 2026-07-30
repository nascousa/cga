from __future__ import annotations

import json

import pytest

from backend.extensions.models import ExtensionConfigUpdate
from backend.extensions.registry import AZURE_POLICY_EXTENSION_ID, _normalize_repo_path, validate_extension_config
from backend.extensions.service import _default_repo_path, get_extension_config, list_extension_runs, run_project_extension, update_extension_config
from backend.schedules.models import ScheduledTaskCreate
from backend.schedules.service import create_scheduled_task, execute_scheduled_task


async def _seed_project(db, repo_path: str, *, project_id: int = 1) -> None:
    await db.execute(
        "INSERT INTO projects(id, project_name, project_id, repo_path, is_active) VALUES (?, ?, ?, ?, 1)",
        (project_id, "Azure Policy", "AZPOLICY01", repo_path),
    )
    await db.commit()


def _write_policy_fixture(root) -> None:
    policy_root = root / "settings" / "BuiltInPoliciesV2"
    for folder in ("AllEnvironments", "USNat", "USSec"):
        folder_path = policy_root / folder
        folder_path.mkdir(parents=True, exist_ok=True)
        (folder_path / "11111111-1111-1111-1111-111111111111.json").write_text(
            json.dumps(
                {
                    "name": "11111111-1111-1111-1111-111111111111",
                    "properties": {
                        "metadata": {"version": "1.0.0"},
                        "policyRule": {"then": {"effect": "Audit"}},
                    },
                }
            ),
            encoding="utf-8",
        )


def test_windows_repos_path_maps_to_container_mount(tmp_path) -> None:
    repos_mount = tmp_path / "repos"
    repos_mount.mkdir()

    assert _normalize_repo_path("D:\\Repos\\azure-policy", repos_mount) == str(repos_mount / "azure-policy")
    assert _normalize_repo_path("D:/Repos/team/policy", repos_mount) == str(repos_mount / "team" / "policy")
    assert _normalize_repo_path("D:\\Other\\policy", repos_mount) == "D:\\Other\\policy"


def test_default_repo_path_prefers_explicit_project_path(tmp_path) -> None:
    assert _default_repo_path({"repo_path": str(tmp_path), "project_name": "BrowserAgent"}) == str(tmp_path)


def test_default_repo_path_without_project_is_empty() -> None:
    assert _default_repo_path(None) == ""


def test_proxy_config_requires_subscription_scope_and_disables_compliance() -> None:
    config = {
        "azure_monitor_enabled": True,
        "subscription_id": "00000000-0000-0000-0000-000000000001",
        "auth_mode": "proxy",
        "proxy_endpoint": "https://cga-policy-proxy.example",
        "proxy_key_env": "CGA_PROXY_KEY",
        "include_compliance": False,
    }

    validate_extension_config(AZURE_POLICY_EXTENSION_ID, config, require_complete=True)

    with pytest.raises(ValueError, match="include_compliance=false"):
        validate_extension_config(
            AZURE_POLICY_EXTENSION_ID,
            {**config, "include_compliance": True},
            require_complete=True,
        )
    with pytest.raises(ValueError, match="subscription scope only"):
        validate_extension_config(
            AZURE_POLICY_EXTENSION_ID,
            {**config, "subscription_id": "", "management_group_id": "platform"},
            require_complete=True,
        )
    with pytest.raises(ValueError, match="proxy_key"):
        validate_extension_config(
            AZURE_POLICY_EXTENSION_ID,
            {**config, "proxy_key": "must-not-be-persisted"},
            require_complete=True,
        )


@pytest.mark.asyncio
async def test_extension_config_defaults_to_project_repo_path(auth_pg_pool, tmp_path) -> None:
    async with auth_pg_pool.acquire() as db:
        await _seed_project(db, str(tmp_path))

        config = await get_extension_config(db, AZURE_POLICY_EXTENSION_ID, 1)

    assert config.enabled is True
    assert config.project_external_id == "AZPOLICY01"
    assert config.config["repo_path"] == str(tmp_path)
    assert config.config["policy_root"] == "settings/BuiltInPoliciesV2"
    assert config.config["cloud_folders"] == ["AllEnvironments", "USNat", "USSec"]


@pytest.mark.asyncio
async def test_extension_config_update_persists(auth_pg_pool, tmp_path) -> None:
    async with auth_pg_pool.acquire() as db:
        await _seed_project(db, str(tmp_path))

        saved = await update_extension_config(
            db,
            AZURE_POLICY_EXTENSION_ID,
            1,
            ExtensionConfigUpdate(
                enabled=False,
                config={
                    "repo_path": str(tmp_path),
                    "policy_root": "policy",
                    "cloud_folders": ["AllEnvironments", "USSec"],
                    "baseline_folder": "AllEnvironments",
                },
            ),
        )
        loaded = await get_extension_config(db, AZURE_POLICY_EXTENSION_ID, 1)

    assert saved == loaded
    assert loaded.enabled is False
    assert loaded.config["policy_root"] == "policy"
    assert loaded.config["cloud_folders"] == ["AllEnvironments", "USSec"]


@pytest.mark.asyncio
async def test_extension_config_rejects_inline_secrets(auth_pg_pool, tmp_path) -> None:
    async with auth_pg_pool.acquire() as db:
        await _seed_project(db, str(tmp_path))

        with pytest.raises(ValueError, match="notification_webhook_url"):
            await update_extension_config(
                db,
                AZURE_POLICY_EXTENSION_ID,
                1,
                ExtensionConfigUpdate(
                    enabled=True,
                    config={"notification_webhook_url": "https://alerts.example/secret-hook"},
                ),
            )

        with pytest.raises(ValueError, match="client_secret"):
            await run_project_extension(
                db,
                AZURE_POLICY_EXTENSION_ID,
                config_override={"client_secret": "never-persist-client-secret"},
            )

        with pytest.raises(ValueError, match="model_endpoint"):
            await update_extension_config(
                db,
                AZURE_POLICY_EXTENSION_ID,
                1,
                ExtensionConfigUpdate(
                    enabled=True,
                    config={
                        "model_summary_enabled": True,
                        "model_endpoint": "https://model.example/v1?api_key=never-persist-api-key",
                    },
                ),
            )

        with pytest.raises(ValueError, match="notification channel"):
            await update_extension_config(
                db,
                AZURE_POLICY_EXTENSION_ID,
                1,
                ExtensionConfigUpdate(enabled=True, config={"notifications_enabled": True}),
            )

        with pytest.raises(ValueError, match="valid email"):
            await update_extension_config(
                db,
                AZURE_POLICY_EXTENSION_ID,
                1,
                ExtensionConfigUpdate(
                    enabled=True,
                    config={
                        "notifications_enabled": True,
                        "notification_email_recipients": ["not-an-email"],
                    },
                ),
            )

        with pytest.raises(ValueError, match="activity_subscription_ids"):
            await update_extension_config(
                db,
                AZURE_POLICY_EXTENSION_ID,
                1,
                ExtensionConfigUpdate(
                    enabled=True,
                    config={
                        "azure_monitor_enabled": True,
                        "management_group_id": "platform",
                        "include_activity": True,
                    },
                ),
            )


@pytest.mark.asyncio
async def test_run_project_extension_records_scan_result(auth_pg_pool, tmp_path) -> None:
    _write_policy_fixture(tmp_path)

    async with auth_pg_pool.acquire() as db:
        await _seed_project(db, str(tmp_path))

        run = await run_project_extension(db, AZURE_POLICY_EXTENSION_ID, 1)

    assert run.status == "success", run.model_dump_json(indent=2)
    assert run.severity == "info"
    assert run.project_external_id == "AZPOLICY01"
    assert run.summary["finding_count"] == 0
    assert run.result["findings"] == []


@pytest.mark.asyncio
async def test_run_platform_extension_records_scan_result(auth_pg_pool, tmp_path) -> None:
    _write_policy_fixture(tmp_path)

    async with auth_pg_pool.acquire() as db:
        saved = await update_extension_config(
            db,
            AZURE_POLICY_EXTENSION_ID,
            None,
            ExtensionConfigUpdate(
                enabled=True,
                config={
                    "repo_path": str(tmp_path),
                    "policy_root": "settings/BuiltInPoliciesV2",
                    "cloud_folders": ["AllEnvironments", "USNat", "USSec"],
                    "baseline_folder": "AllEnvironments",
                },
            ),
        )

        run = await run_project_extension(db, AZURE_POLICY_EXTENSION_ID)
        runs = await list_extension_runs(db, AZURE_POLICY_EXTENSION_ID)

    assert saved.project_id is None
    assert run.status == "success", run.model_dump_json(indent=2)
    assert run.project_id is None
    assert run.summary["finding_count"] == 0
    assert runs == [run]


@pytest.mark.asyncio
async def test_execute_extension_schedule_task_runs_internal_extension(auth_pg_pool, tmp_path) -> None:
    _write_policy_fixture(tmp_path)

    async with auth_pg_pool.acquire() as db:
        await _seed_project(db, str(tmp_path))
        task = await create_scheduled_task(
            db,
            ScheduledTaskCreate(
                name="Daily policy scan",
                task_type="extension_task",
                project_id=1,
                cadence_minutes=1440,
                timeout_seconds=300,
                target_url="",
                payload={"extension_id": AZURE_POLICY_EXTENSION_ID, "config_override": {}},
            ),
        )

        run = await execute_scheduled_task(db, task)

    assert run.status == "success"
    assert run.status_code == 200
    assert run.response["extension_id"] == AZURE_POLICY_EXTENSION_ID
    assert run.response["summary"]["finding_count"] == 0


@pytest.mark.asyncio
async def test_execute_platform_extension_schedule_task_runs_internal_extension(auth_pg_pool, tmp_path) -> None:
    _write_policy_fixture(tmp_path)

    async with auth_pg_pool.acquire() as db:
        task = await create_scheduled_task(
            db,
            ScheduledTaskCreate(
                name="Daily platform policy scan",
                task_type="extension_task",
                project_id=None,
                cadence_minutes=1440,
                timeout_seconds=300,
                target_url="",
                payload={
                    "extension_id": AZURE_POLICY_EXTENSION_ID,
                    "config_override": {"repo_path": str(tmp_path)},
                },
            ),
        )

        run = await execute_scheduled_task(db, task)

    assert run.status == "success", run.model_dump_json(indent=2)
    assert run.status_code == 200
    assert run.response["extension_id"] == AZURE_POLICY_EXTENSION_ID
    assert run.response["project_id"] is None
    assert run.response["summary"]["finding_count"] == 0


@pytest.mark.asyncio
async def test_live_monitor_persists_scoped_snapshots_and_loads_baseline(auth_pg_pool, monkeypatch) -> None:
    scope = "/subscriptions/00000000-0000-0000-0000-000000000001"
    captured_snapshots = [
        {"schema_version": 1, "scope": scope, "captured_at": "2026-07-14T08:00:00Z", "assignments": {}},
        {"schema_version": 1, "scope": scope, "captured_at": "2026-07-14T09:00:00Z", "assignments": {}},
    ]
    previous_snapshots = []

    def fake_run_extension(extension_id, config, *, previous_snapshot=None):
        previous_snapshots.append(previous_snapshot)
        snapshot = captured_snapshots[len(previous_snapshots) - 1]
        return {
            "extension_id": extension_id,
            "status": "completed",
            "severity": "info",
            "summary": {"finding_count": 0, "severity_counts": {}},
            "findings": [],
            "_snapshot": snapshot,
            "_snapshot_scope": scope,
        }

    monkeypatch.setattr("backend.extensions.service.run_extension", fake_run_extension)

    async with auth_pg_pool.acquire() as db:
        await update_extension_config(
            db,
            AZURE_POLICY_EXTENSION_ID,
            None,
            ExtensionConfigUpdate(
                enabled=True,
                config={
                    "repo_scan_enabled": False,
                    "azure_monitor_enabled": True,
                    "subscription_id": "00000000-0000-0000-0000-000000000001",
                    "snapshot_retention_count": 30,
                },
            ),
        )
        first = await run_project_extension(db, AZURE_POLICY_EXTENSION_ID)
        second = await run_project_extension(db, AZURE_POLICY_EXTENSION_ID)
        async with db.execute(
            "SELECT scope_key, snapshot_json FROM extension_snapshots ORDER BY captured_at, id"
        ) as cur:
            rows = await cur.fetchall()

    assert previous_snapshots == [None, captured_snapshots[0]]
    assert len(rows) == 2
    assert rows[0]["scope_key"] == scope
    assert json.loads(rows[1]["snapshot_json"]) == captured_snapshots[1]
    assert "_snapshot" not in first.result
    assert "_snapshot" not in second.result


@pytest.mark.asyncio
async def test_notification_failure_does_not_fail_run_or_snapshot(auth_pg_pool, monkeypatch) -> None:
    scope = "/subscriptions/00000000-0000-0000-0000-000000000001"
    snapshot = {
        "schema_version": 1,
        "scope": scope,
        "captured_at": "2026-07-14T10:00:00Z",
        "assignments": {},
    }

    def fake_run_extension(extension_id, config, *, previous_snapshot=None):
        return {
            "extension_id": extension_id,
            "status": "completed",
            "severity": "critical",
            "summary": {"finding_count": 1, "severity_counts": {"critical": 1}},
            "findings": [{"check": "assignment_drift", "severity": "critical", "message": "Changed."}],
            "_snapshot": snapshot,
            "_snapshot_scope": scope,
        }

    def fail_notifications(extension_id, result, config, *, smtp_config):
        raise RuntimeError("webhook URL must never be persisted in this failure")

    monkeypatch.setattr("backend.extensions.service.run_extension", fake_run_extension)
    monkeypatch.setattr(
        "backend.extensions.service.deliver_extension_notifications",
        fail_notifications,
        raising=False,
    )

    async with auth_pg_pool.acquire() as db:
        await update_extension_config(
            db,
            AZURE_POLICY_EXTENSION_ID,
            None,
            ExtensionConfigUpdate(
                enabled=True,
                config={
                    "repo_scan_enabled": False,
                    "azure_monitor_enabled": True,
                    "subscription_id": "00000000-0000-0000-0000-000000000001",
                    "notifications_enabled": True,
                    "notification_webhook_env": "POLICY_WEBHOOK_URL",
                },
            ),
        )
        run = await run_project_extension(db, AZURE_POLICY_EXTENSION_ID)
        async with db.execute("SELECT COUNT(*) AS count FROM extension_snapshots") as cur:
            row = await cur.fetchone()

    assert run.status == "success"
    assert run.result["outputs"]["notifications"] == {
        "status": "failed",
        "channels": {},
        "error_type": "RuntimeError",
    }
    assert "webhook" not in json.dumps(run.result["outputs"]["notifications"], sort_keys=True)
    assert row["count"] == 1


@pytest.mark.asyncio
async def test_extension_failure_persists_only_error_type(auth_pg_pool, tmp_path, monkeypatch) -> None:
    _write_policy_fixture(tmp_path)

    def fail_run(*args, **kwargs):
        raise RuntimeError("access token never-persist-this-token")

    monkeypatch.setattr("backend.extensions.service.run_extension", fail_run)

    async with auth_pg_pool.acquire() as db:
        await _seed_project(db, str(tmp_path))
        run = await run_project_extension(db, AZURE_POLICY_EXTENSION_ID, 1)

    assert run.status == "failed"
    assert run.summary == {"error_type": "RuntimeError"}
    assert run.result["findings"][0]["evidence"] == {"error_type": "RuntimeError"}
    assert "never-persist" not in run.model_dump_json()
