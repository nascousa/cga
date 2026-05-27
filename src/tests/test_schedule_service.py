from __future__ import annotations

import pytest

from backend.schedules.models import ScheduledTaskCreate, ScheduledTaskUpdate
from backend.schedules.service import (
    build_request_payload,
    create_scheduled_task,
    execute_scheduled_task,
    get_due_scheduled_tasks,
    list_scheduled_tasks,
    record_scheduled_task_run,
    update_scheduled_task,
)


async def _seed_project(db, *, project_id: int = 1, project_name: str = "browseragent") -> None:
    await db.execute(
        "INSERT INTO projects(id, project_name, project_id, upstream_url, is_active) VALUES (?, ?, ?, ?, 1)",
        (project_id, project_name, "BA12345678", "http://localhost:9876"),
    )


@pytest.mark.asyncio
async def test_create_scheduled_task_persists_payload_and_due_time(auth_pg_pool) -> None:
    payload = {"action": "ping", "params": {"scope": "smoke"}}

    async with auth_pg_pool.acquire() as db:
        await _seed_project(db)
        task = await create_scheduled_task(
            db,
            ScheduledTaskCreate(
                name="Hourly browser smoke",
                task_type="browseragent_task",
                project_id=1,
                agent_id="browseragent-local",
                target_url="http://localhost:9876/command",
                cadence_minutes=15,
                payload=payload,
            ),
        )
        second = await create_scheduled_task(
            db,
            ScheduledTaskCreate(
                name="Daily browser smoke",
                task_type="browseragent_task",
                project_id=1,
                target_url="http://localhost:9876/command",
                cadence_minutes=1440,
            ),
        )

        tasks = await list_scheduled_tasks(db)
        due = await get_due_scheduled_tasks(db, now_iso="2100-01-01T00:00:00+00:00")

    assert task.id > 0
    assert len(task.task_id) == 8
    assert task.task_id.isalnum()
    assert task.task_id == task.task_id.upper()
    assert len(second.task_id) == 8
    assert second.task_id.isalnum()
    assert second.task_id == second.task_id.upper()
    assert second.task_id != task.task_id
    assert task.enabled is True
    assert task.next_run_at
    assert tasks.items[0].project_name == "browseragent"
    assert {item.task_id for item in tasks.items} == {task.task_id, second.task_id}
    assert tasks.items[0].payload == payload
    assert {item.id for item in due} == {task.id, second.id}


@pytest.mark.asyncio
async def test_disabled_scheduled_task_is_not_due(auth_pg_pool) -> None:
    async with auth_pg_pool.acquire() as db:
        await _seed_project(db)
        task = await create_scheduled_task(
            db,
            ScheduledTaskCreate(
                name="Disabled activation",
                task_type="agent_activation",
                project_id=1,
                agent_id="edge-agent-a",
                target_url="http://localhost:3002/activate",
                cadence_minutes=30,
                enabled=True,
            ),
        )

        updated = await update_scheduled_task(db, task.id, ScheduledTaskUpdate(enabled=False))
        due = await get_due_scheduled_tasks(db, now_iso="2100-01-01T00:00:00+00:00")

    assert updated.enabled is False
    assert due == []


@pytest.mark.asyncio
async def test_record_scheduled_task_run_updates_task_status(auth_pg_pool) -> None:
    async with auth_pg_pool.acquire() as db:
        await _seed_project(db)
        task = await create_scheduled_task(
            db,
            ScheduledTaskCreate(
                name="BrowserAgent ping",
                task_type="browseragent_task",
                project_id=1,
                target_url="http://localhost:9876/command",
                cadence_minutes=10,
            ),
        )

        run = await record_scheduled_task_run(
            db,
            task,
            status="success",
            status_code=202,
            duration_ms=34,
            response={"queued": True},
            started_at="2026-01-01T00:00:00+00:00",
            finished_at="2026-01-01T00:01:00+00:00",
        )
        refreshed = await list_scheduled_tasks(db)

    assert run.id > 0
    assert run.schedule_id == task.id
    assert run.task_id == task.task_id
    assert run.status == "success"
    assert refreshed.items[0].last_run_status == "success"
    assert refreshed.items[0].last_run_at
    assert refreshed.items[0].next_run_at == "2026-01-01T00:11:00+00:00"


@pytest.mark.asyncio
async def test_browseragent_page_test_payload_builds_batch_commands(auth_pg_pool) -> None:
    async with auth_pg_pool.acquire() as db:
        await _seed_project(db)
        task = await create_scheduled_task(
            db,
            ScheduledTaskCreate(
                name="Schedule page smoke",
                task_type="browseragent_task",
                project_id=1,
                target_url="http://localhost:9876/command/batch",
                cadence_minutes=30,
                payload={
                    "browser_task": {
                        "kind": "page_test",
                        "url": "http://localhost:18001/admin/schedule",
                        "goal": "Verify the schedule page is usable.",
                        "assertions": ["Schedule", "Scheduled tasks"],
                        "artifacts": {
                            "screenshot": True,
                            "console": True,
                            "metrics": True,
                            "dom_snapshot": False,
                        },
                    }
                },
            ),
        )

    payload = build_request_payload(task, command_run_id="RUN1")
    commands = payload["commands"]

    assert [command["action"] for command in commands] == [
        "newTab",
        "findText",
        "findText",
        "captureConsole",
        "getPageMetrics",
        "screenshot",
    ]
    assert commands[0]["id"] == f"{task.task_id}-RUN1-open"
    assert commands[0]["params"] == {"url": "http://localhost:18001/admin/schedule", "active": True}
    assert commands[1]["params"] == {"text": "Schedule", "caseSensitive": False}
    assert payload["metadata"]["schedule_task_id"] == task.task_id
    assert payload["metadata"]["browser_task"]["goal"] == "Verify the schedule page is usable."


@pytest.mark.asyncio
async def test_execute_browseragent_workflow_waits_for_results_and_compacts_artifacts(auth_pg_pool, monkeypatch) -> None:
    posted_payloads = []

    class FakeResponse:
        def __init__(self, status_code: int, payload: dict) -> None:
            self.status_code = status_code
            self._payload = payload
            self.text = ""
            self.content = b"{}"

        def json(self) -> dict:
            return self._payload

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, url: str, json: dict) -> FakeResponse:
            posted_payloads.append((url, json))
            return FakeResponse(202, {"queued": True, "id": json["id"], "position": 1})

        async def get(self, url: str) -> FakeResponse:
            command_id = url.rsplit("/", 1)[-1]
            data = {"tabId": 123}
            if "-assert" in command_id:
                data["count"] = 1
                data["matches"] = [{"context": "Schedule"}]
            if command_id.endswith("-screenshot"):
                data["dataUrl"] = "data:image/png;base64," + ("a" * 2048)
            return FakeResponse(200, {"id": command_id, "success": True, "data": data})

    monkeypatch.setattr("backend.schedules.service.httpx.AsyncClient", FakeAsyncClient)

    async with auth_pg_pool.acquire() as db:
        await _seed_project(db)
        task = await create_scheduled_task(
            db,
            ScheduledTaskCreate(
                name="BrowserAgent scheduled smoke",
                task_type="browseragent_task",
                project_id=1,
                target_url="http://localhost:9876/command/batch",
                cadence_minutes=30,
                timeout_seconds=5,
                payload={
                    "browser_task": {
                        "kind": "page_test",
                        "url": "http://localhost:18001/admin/schedule",
                        "assertions": ["Schedule"],
                        "artifacts": {"screenshot": True, "console": False, "metrics": False},
                    }
                },
            ),
        )

        run = await execute_scheduled_task(db, task)

    assert posted_payloads[0][0] == "http://localhost:9876/command"
    assert [payload["action"] for _, payload in posted_payloads] == ["newTab", "findText", "screenshot"]
    assert "tabId" not in posted_payloads[0][1]["params"]
    assert posted_payloads[1][1]["params"]["tabId"] == 123
    assert run.status == "success"
    assert run.status_code == 202
    browseragent = run.response["browseragent"]
    assert browseragent["execution_mode"] == "sequential"
    assert browseragent["expected_count"] == len(posted_payloads)
    assert browseragent["completed_count"] == browseragent["expected_count"]
    screenshot_result = next(item for item in browseragent["results"] if item["id"].endswith("-screenshot"))
    assert screenshot_result["data"]["dataUrl"] == "[omitted]"
    assert screenshot_result["data"]["dataUrlLength"] > 2048