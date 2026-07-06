from __future__ import annotations

import json

import pytest

from backend.extensions.models import ExtensionConfigUpdate
from backend.extensions.registry import AZURE_POLICY_EXTENSION_ID, _normalize_repo_path
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
