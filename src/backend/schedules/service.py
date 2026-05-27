"""Persistence and execution service for admin scheduled automation."""
from __future__ import annotations

import asyncio
import json
import secrets
import string
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urlparse, urlunparse

import httpx
import structlog

from backend.auth.pgshim import Connection, get_pool
from backend.schedules.models import (
    ScheduledTaskCreate,
    ScheduledTaskList,
    ScheduledTaskOut,
    ScheduledTaskRunOut,
    ScheduledTaskUpdate,
)

log = structlog.get_logger()

TASK_SELECT = """
SELECT
    st.id,
    st.task_id,
    st.name,
    st.description,
    st.task_type,
    st.project_id,
    p.project_name,
    p.project_id AS project_external_id,
    st.agent_id,
    st.target_url,
    st.payload_json,
    st.cadence_minutes,
    st.timeout_seconds,
    st.enabled,
    st.created_at,
    st.updated_at,
    st.next_run_at,
    st.last_run_at,
    st.last_run_status,
    st.last_run_error
FROM scheduled_tasks st
LEFT JOIN projects p ON p.id = st.project_id
"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime | None = None) -> str:
    dt = value or utc_now()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def next_run_iso(cadence_minutes: int, *, from_time: datetime | None = None) -> str:
    base = from_time or utc_now()
    return iso_utc(base + timedelta(minutes=max(1, int(cadence_minutes))))


def parse_iso_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def random_scheduled_task_id(length: int = 8) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


async def create_unique_scheduled_task_id(db: Connection) -> str:
    for _ in range(30):
        candidate = random_scheduled_task_id()
        async with db.execute("SELECT 1 FROM scheduled_tasks WHERE task_id = ?", (candidate,)) as cur:
            if not await cur.fetchone():
                return candidate
    raise RuntimeError("Could not generate a unique scheduled task id")


def _decode_json(value: str | None, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
    if not value:
        return dict(fallback or {})
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else dict(fallback or {})
    except Exception:
        return dict(fallback or {})


def _encode_json(value: dict[str, Any] | None) -> str:
    return json.dumps(value or {}, sort_keys=True, ensure_ascii=True)


def _compact_response_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 6:
        return "[truncated]"
    if isinstance(value, dict):
        compacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            key_l = key_text.lower()
            if key_l in {"dataurl", "data_url"}:
                compacted[key_text] = "[omitted]"
                compacted[f"{key_text}Length"] = len(str(item or ""))
            elif any(marker in key_l for marker in ("password", "token", "secret", "authorization", "api_key")):
                compacted[key_text] = "***"
            else:
                compacted[key_text] = _compact_response_value(item, depth=depth + 1)
        return compacted
    if isinstance(value, list):
        compacted_items = [_compact_response_value(item, depth=depth + 1) for item in value[:25]]
        if len(value) > 25:
            compacted_items.append({"truncatedCount": len(value) - 25})
        return compacted_items
    if isinstance(value, str) and len(value) > 2000:
        return value[:2000] + "... [truncated]"
    return value


def _task_from_row(row) -> ScheduledTaskOut:
    data = dict(row)
    data["payload"] = _decode_json(data.pop("payload_json", "{}"))
    data["enabled"] = bool(data.get("enabled"))
    return ScheduledTaskOut(**data)


def _run_from_row(row) -> ScheduledTaskRunOut:
    data = dict(row)
    data["response"] = _decode_json(data.pop("response_json", "{}"))
    return ScheduledTaskRunOut(**data)


def _as_text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        candidates = value.splitlines()
    elif isinstance(value, list):
        candidates = value
    else:
        return []
    return [str(item).strip() for item in candidates if str(item).strip()]


def _browser_task_spec(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    spec = payload.get("browser_task")
    return spec if isinstance(spec, dict) else None


def _browser_task_artifacts(spec: dict[str, Any]) -> dict[str, bool]:
    raw = spec.get("artifacts") if isinstance(spec.get("artifacts"), dict) else {}
    return {
        "screenshot": bool(raw.get("screenshot", True)),
        "console": bool(raw.get("console", True)),
        "metrics": bool(raw.get("metrics", True)),
        "dom_snapshot": bool(raw.get("dom_snapshot", False)),
    }


def _command_run_suffix(value: str) -> str:
    text = "".join(ch for ch in value if ch.isalnum())
    return text[-16:] or str(int(time.time()))


def _build_browseragent_page_test_payload(task: ScheduledTaskOut, spec: dict[str, Any], command_run_id: str) -> dict[str, Any]:
    page_url = str(spec.get("url") or spec.get("target_url") or "").strip()
    if not page_url:
        raise ValueError("browser_task.url is required")
    if not (page_url.startswith("http://") or page_url.startswith("https://")):
        raise ValueError("browser_task.url must start with http:// or https://")

    command_prefix = f"{task.task_id}-{_command_run_suffix(command_run_id)}"
    commands: list[dict[str, Any]] = [
        {
            "id": f"{command_prefix}-open",
            "action": "newTab",
            "params": {"url": page_url, "active": True},
        }
    ]

    for index, assertion in enumerate(_as_text_list(spec.get("assertions")), start=1):
        commands.append(
            {
                "id": f"{command_prefix}-assert{index}",
                "action": "findText",
                "params": {"text": assertion, "caseSensitive": False},
            }
        )

    artifacts = _browser_task_artifacts(spec)
    if artifacts["console"]:
        commands.append(
            {
                "id": f"{command_prefix}-console",
                "action": "captureConsole",
                "params": {"durationMs": 1000},
            }
        )
    if artifacts["metrics"]:
        commands.append({"id": f"{command_prefix}-metrics", "action": "getPageMetrics", "params": {}})
    if artifacts["dom_snapshot"]:
        commands.append({"id": f"{command_prefix}-dom", "action": "getContent", "params": {}})
    if artifacts["screenshot"]:
        commands.append({"id": f"{command_prefix}-screenshot", "action": "screenshot", "params": {}})

    return {
        "commands": commands,
        "metadata": {
            "schedule_task_id": task.task_id,
            "schedule_db_id": task.id,
            "task_name": task.name,
            "project_id": task.project_external_id,
            "browser_task": spec,
        },
    }


def _resolve_execution_target_url(task: ScheduledTaskOut) -> str:
    if task.task_type == "browseragent_task" and _browser_task_spec(task.payload):
        parsed = urlparse(task.target_url)
        if parsed.path.rstrip("/") == "/command":
            return urlunparse(parsed._replace(path="/command/batch"))
    return task.target_url


def _browseragent_command_url(target_url: str) -> str:
    parsed = urlparse(target_url)
    if parsed.path.rstrip("/") == "/command/batch":
        return urlunparse(parsed._replace(path="/command"))
    return target_url


def _browseragent_result_url(target_url: str, command_id: str) -> str:
    parsed = urlparse(target_url)
    path = parsed.path or ""
    prefix = path.split("/command", 1)[0] if "/command" in path else path.rsplit("/", 1)[0]
    result_path = (prefix.rstrip("/") + "/result/" + quote(command_id, safe="")).replace("//", "/")
    return urlunparse(parsed._replace(path=result_path, query="", fragment=""))


def _queued_browseragent_items(response_payload: dict[str, Any]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    raw_items = response_payload.get("items")
    if isinstance(raw_items, list):
        for item in raw_items:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            items.append({"id": str(item.get("id")), "action": str(item.get("action") or "")})
    elif response_payload.get("id"):
        items.append({"id": str(response_payload.get("id")), "action": str(response_payload.get("action") or "")})
    return items


async def _collect_browseragent_results(
    client: httpx.AsyncClient,
    *,
    target_url: str,
    queued_items: list[dict[str, str]],
    deadline: float,
) -> dict[str, Any] | None:
    if not queued_items:
        return None

    pending = {item["id"]: item for item in queued_items}
    results: list[dict[str, Any]] = []
    while pending and time.perf_counter() < deadline:
        for command_id in list(pending.keys()):
            try:
                response = await client.get(_browseragent_result_url(target_url, command_id))
            except Exception:
                continue
            if response.status_code == 404:
                continue
            try:
                result_payload = response.json() if response.content else {}
            except Exception:
                result_payload = {"success": False, "error": response.text[:1000]}
            if not isinstance(result_payload, dict):
                result_payload = {"success": False, "data": result_payload}
            queued = pending.pop(command_id)
            results.append(
                {
                    "id": command_id,
                    "action": queued.get("action", ""),
                    "success": result_payload.get("success") is not False,
                    "error": str(result_payload.get("error") or "")[:1000],
                    "data": _compact_response_value(result_payload.get("data") or {}),
                }
            )
        if pending:
            await asyncio.sleep(min(0.5, max(0.0, deadline - time.perf_counter())))

    failures = [item for item in results if not item["success"]]
    return {
        "expected_count": len(queued_items),
        "completed_count": len(results),
        "pending_ids": sorted(pending.keys()),
        "failed_count": len(failures),
        "results": results,
    }


async def _wait_for_browseragent_result(
    client: httpx.AsyncClient,
    *,
    target_url: str,
    command_id: str,
    action: str,
    params: dict[str, Any],
    deadline: float,
) -> dict[str, Any]:
    while time.perf_counter() < deadline:
        try:
            response = await client.get(_browseragent_result_url(target_url, command_id))
        except Exception:
            await asyncio.sleep(min(0.5, max(0.0, deadline - time.perf_counter())))
            continue
        if response.status_code == 404:
            await asyncio.sleep(min(0.5, max(0.0, deadline - time.perf_counter())))
            continue
        try:
            result_payload = response.json() if response.content else {}
        except Exception:
            result_payload = {"success": False, "error": response.text[:1000]}
        if not isinstance(result_payload, dict):
            result_payload = {"success": False, "data": result_payload}
        data = result_payload.get("data") if isinstance(result_payload.get("data"), dict) else {}
        success = result_payload.get("success") is not False
        error = str(result_payload.get("error") or "")[:1000]
        if action == "findText" and "-assert" in command_id and int(data.get("count") or 0) < 1:
            success = False
            error = f"Text assertion not found: {params.get('text') or ''}"[:1000]
        return {
            "id": command_id,
            "action": action,
            "success": success,
            "error": error,
            "data": _compact_response_value(data),
        }
    return {
        "id": command_id,
        "action": action,
        "success": False,
        "error": "Timed out waiting for BrowserAgent result",
        "data": {},
    }


async def _execute_browseragent_workflow(
    client: httpx.AsyncClient,
    *,
    task: ScheduledTaskOut,
    target_url: str,
    request_payload: dict[str, Any],
    deadline: float,
) -> dict[str, Any]:
    command_url = _browseragent_command_url(target_url)
    commands = request_payload.get("commands") if isinstance(request_payload.get("commands"), list) else []
    items: list[dict[str, str]] = []
    results: list[dict[str, Any]] = []
    current_tab_id: int | None = None
    status_code: int | None = None

    for raw_command in commands:
        if not isinstance(raw_command, dict) or not raw_command.get("action"):
            continue
        command = dict(raw_command)
        params = dict(command.get("params") or {})
        action = str(command["action"])
        base_command_id = str(command.get("id") or f"{task.task_id}-{len(items) + 1}")
        if current_tab_id is not None and action not in {"newTab", "navigate", "openNewWindow"} and "tabId" not in params:
            params["tabId"] = current_tab_id

        max_attempts = 5 if action == "findText" else 1
        result: dict[str, Any] | None = None
        for attempt in range(1, max_attempts + 1):
            if time.perf_counter() >= deadline:
                break
            command_id = base_command_id if attempt == 1 else f"{base_command_id}-try{attempt}"
            command_body = {"id": command_id, "action": action, "params": params}

            response = await client.post(command_url, json=command_body)
            status_code = response.status_code
            if response.status_code >= 400:
                result = {
                    "id": command_id,
                    "action": action,
                    "success": False,
                    "error": response.text[:1000],
                    "data": {},
                    "attempts": attempt,
                }
                break
            items.append({"id": command_id, "action": action})
            result = await _wait_for_browseragent_result(
                client,
                target_url=target_url,
                command_id=command_id,
                action=action,
                params=params,
                deadline=deadline,
            )
            result["attempts"] = attempt
            if result.get("success") or action != "findText":
                break
            await asyncio.sleep(min(1.0, max(0.0, deadline - time.perf_counter())))
        if result is None:
            result = {
                "id": base_command_id,
                "action": action,
                "success": False,
                "error": "Timed out before BrowserAgent command could complete",
                "data": {},
                "attempts": max_attempts,
            }
        results.append(result)
        tab_id = result.get("data", {}).get("tabId")
        if isinstance(tab_id, int):
            current_tab_id = tab_id
        if action in {"newTab", "navigate"} and result.get("success"):
            await asyncio.sleep(min(1.5, max(0.0, deadline - time.perf_counter())))

    failures = [item for item in results if not item["success"]]
    return {
        "queued": True,
        "count": len(items),
        "items": items,
        "status_code": status_code,
        "browseragent": {
            "execution_mode": "sequential",
            "expected_count": len(commands),
            "completed_count": len(results),
            "pending_ids": [],
            "failed_count": len(failures),
            "results": results,
        },
    }


async def get_scheduled_task(db: Connection, task_id: int) -> ScheduledTaskOut | None:
    async with db.execute(TASK_SELECT + " WHERE st.id = ?", (task_id,)) as cur:
        row = await cur.fetchone()
    return _task_from_row(row) if row else None


async def list_recent_scheduled_task_runs(db: Connection, *, limit: int = 25) -> list[ScheduledTaskRunOut]:
    async with db.execute(
        """
        SELECT str.id, str.schedule_id, st.task_id, str.started_at, str.finished_at,
               str.status, str.status_code, str.duration_ms, str.error, str.response_json
        FROM scheduled_task_runs str
        LEFT JOIN scheduled_tasks st ON st.id = str.schedule_id
        ORDER BY str.started_at DESC, str.id DESC
        LIMIT ?
        """,
        (max(1, min(int(limit), 200)),),
    ) as cur:
        rows = await cur.fetchall()
    return [_run_from_row(row) for row in rows]


async def list_scheduled_tasks(db: Connection, *, include_runs: bool = True) -> ScheduledTaskList:
    async with db.execute(
        TASK_SELECT + " ORDER BY st.enabled DESC, st.next_run_at ASC, st.id ASC"
    ) as cur:
        rows = await cur.fetchall()
    recent_runs = await list_recent_scheduled_task_runs(db) if include_runs else []
    return ScheduledTaskList(items=[_task_from_row(row) for row in rows], recent_runs=recent_runs)


async def create_scheduled_task(db: Connection, body: ScheduledTaskCreate) -> ScheduledTaskOut:
    now = utc_now()
    task_id = await create_unique_scheduled_task_id(db)
    async with db.execute(
        """
        INSERT INTO scheduled_tasks(
            task_id, name, description, task_type, project_id, agent_id, target_url, payload_json,
            cadence_minutes, timeout_seconds, enabled, created_at, updated_at, next_run_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        RETURNING id
        """,
        (
            task_id,
            body.name,
            body.description,
            body.task_type,
            body.project_id,
            body.agent_id,
            body.target_url,
            _encode_json(body.payload),
            body.cadence_minutes,
            body.timeout_seconds,
            1 if body.enabled else 0,
            iso_utc(now),
            iso_utc(now),
            next_run_iso(body.cadence_minutes, from_time=now),
        ),
    ) as cur:
        row = await cur.fetchone()
    await db.commit()
    created = await get_scheduled_task(db, int(row["id"]))
    if created is None:
        raise RuntimeError("scheduled task insert did not return a retrievable row")
    return created


async def update_scheduled_task(db: Connection, task_id: int, body: ScheduledTaskUpdate) -> ScheduledTaskOut:
    current = await get_scheduled_task(db, task_id)
    if current is None:
        raise KeyError("Scheduled task not found")

    changes = body.model_dump(exclude_unset=True)
    if not changes:
        return current

    merged = current.model_dump()
    merged.update(changes)
    candidate = ScheduledTaskCreate(
        name=merged["name"],
        description=merged.get("description") or "",
        task_type=merged["task_type"],
        project_id=merged.get("project_id"),
        agent_id=merged.get("agent_id") or "",
        target_url=merged.get("target_url") or "",
        payload=merged.get("payload") or {},
        cadence_minutes=merged["cadence_minutes"],
        timeout_seconds=merged["timeout_seconds"],
        enabled=merged["enabled"],
    )

    now = utc_now()
    recompute_next = any(key in changes for key in ("cadence_minutes", "enabled")) and candidate.enabled
    next_run_at = next_run_iso(candidate.cadence_minutes, from_time=now) if recompute_next else current.next_run_at
    await db.execute(
        """
        UPDATE scheduled_tasks
        SET name = ?, description = ?, task_type = ?, project_id = ?, agent_id = ?, target_url = ?,
            payload_json = ?, cadence_minutes = ?, timeout_seconds = ?, enabled = ?, updated_at = ?, next_run_at = ?
        WHERE id = ?
        """,
        (
            candidate.name,
            candidate.description,
            candidate.task_type,
            candidate.project_id,
            candidate.agent_id,
            candidate.target_url,
            _encode_json(candidate.payload),
            candidate.cadence_minutes,
            candidate.timeout_seconds,
            1 if candidate.enabled else 0,
            iso_utc(now),
            next_run_at,
            task_id,
        ),
    )
    await db.commit()
    updated = await get_scheduled_task(db, task_id)
    if updated is None:
        raise KeyError("Scheduled task not found")
    return updated


async def delete_scheduled_task(db: Connection, task_id: int) -> None:
    await db.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))
    await db.commit()


async def get_due_scheduled_tasks(
    db: Connection,
    *,
    now_iso: str | None = None,
    limit: int = 10,
) -> list[ScheduledTaskOut]:
    async with db.execute(
        TASK_SELECT
        + " WHERE st.enabled = 1 AND st.next_run_at <= ? ORDER BY st.next_run_at ASC, st.id ASC LIMIT ?",
        (now_iso or iso_utc(), max(1, min(int(limit), 50))),
    ) as cur:
        rows = await cur.fetchall()
    return [_task_from_row(row) for row in rows]


async def record_scheduled_task_run(
    db: Connection,
    task: ScheduledTaskOut,
    *,
    status: str,
    status_code: int | None = None,
    duration_ms: int = 0,
    response: dict[str, Any] | None = None,
    error: str = "",
    started_at: str | None = None,
    finished_at: str | None = None,
) -> ScheduledTaskRunOut:
    started = started_at or iso_utc()
    finished = finished_at or iso_utc()
    async with db.execute(
        """
        INSERT INTO scheduled_task_runs(
            schedule_id, started_at, finished_at, status, status_code, duration_ms, error, response_json
        ) VALUES(?,?,?,?,?,?,?,?)
        RETURNING id, schedule_id, started_at, finished_at, status, status_code, duration_ms, error, response_json
        """,
        (
            task.id,
            started,
            finished,
            status,
            status_code,
            max(0, int(duration_ms)),
            error[:1000],
            _encode_json(response),
        ),
    ) as cur:
        row = await cur.fetchone()
    row_data = dict(row)
    row_data["task_id"] = task.task_id

    await db.execute(
        """
        UPDATE scheduled_tasks
        SET last_run_at = ?, last_run_status = ?, last_run_error = ?, next_run_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            finished,
            status,
            error[:1000],
            next_run_iso(task.cadence_minutes, from_time=parse_iso_utc(finished)),
            finished,
            task.id,
        ),
    )
    await db.commit()
    return _run_from_row(row_data)


def build_request_payload(task: ScheduledTaskOut, *, command_run_id: str | None = None) -> dict[str, Any]:
    if task.task_type == "agent_activation":
        return {
            "action": "activate_agent",
            "agent_id": task.agent_id,
            "project_id": task.project_external_id,
            "payload": task.payload,
        }
    if task.task_type == "browseragent_task":
        spec = _browser_task_spec(task.payload)
        if spec:
            return _build_browseragent_page_test_payload(task, spec, command_run_id or iso_utc())
    return dict(task.payload or {})


async def execute_scheduled_task(db: Connection, task: ScheduledTaskOut) -> ScheduledTaskRunOut:
    started_at = iso_utc()
    started_clock = time.perf_counter()
    if not task.target_url:
        return await record_scheduled_task_run(
            db,
            task,
            status="failed",
            duration_ms=0,
            error="target_url is required",
            started_at=started_at,
        )

    try:
        request_payload = build_request_payload(task, command_run_id=started_at)
        target_url = _resolve_execution_target_url(task)
        deadline = started_clock + float(task.timeout_seconds)
        async with httpx.AsyncClient(timeout=float(task.timeout_seconds)) as client:
            if task.task_type == "browseragent_task" and _browser_task_spec(task.payload):
                response_payload = await _execute_browseragent_workflow(
                    client,
                    task=task,
                    target_url=target_url,
                    request_payload=request_payload,
                    deadline=deadline,
                )
                response = None
            else:
                response = await client.post(target_url, json=request_payload)
        duration_ms = int((time.perf_counter() - started_clock) * 1000)
        if response is not None:
            try:
                response_payload = response.json() if response.content else {}
                if not isinstance(response_payload, dict):
                    response_payload = {"body": response_payload}
            except Exception:
                response_payload = {"body": response.text[:2000]}
        status_code = response_payload.get("status_code") if response is None else response.status_code
        status = "success" if status_code is not None and int(status_code) < 400 else "failed"
        error = "" if status == "success" else (response.text[:1000] if response is not None else "BrowserAgent command failed")
        if status == "success" and task.task_type == "browseragent_task" and not _browser_task_spec(task.payload):
            async with httpx.AsyncClient(timeout=float(task.timeout_seconds)) as client:
                browseragent_results = await _collect_browseragent_results(
                    client,
                    target_url=target_url,
                    queued_items=_queued_browseragent_items(response_payload),
                    deadline=deadline,
                )
            if browseragent_results:
                duration_ms = int((time.perf_counter() - started_clock) * 1000)
                response_payload["browseragent"] = browseragent_results
                if browseragent_results["pending_ids"]:
                    status = "failed"
                    error = "Timed out waiting for BrowserAgent results: " + ", ".join(browseragent_results["pending_ids"][:5])
                elif browseragent_results["failed_count"]:
                    status = "failed"
                    failed = [item for item in browseragent_results["results"] if not item["success"]]
                    error = failed[0].get("error") or "One or more BrowserAgent commands failed"
        if task.task_type == "browseragent_task" and _browser_task_spec(task.payload):
            browseragent_results = response_payload.get("browseragent") if isinstance(response_payload, dict) else None
            if browseragent_results:
                if browseragent_results["pending_ids"]:
                    status = "failed"
                    error = "Timed out waiting for BrowserAgent results: " + ", ".join(browseragent_results["pending_ids"][:5])
                elif browseragent_results["failed_count"]:
                    status = "failed"
                    failed = [item for item in browseragent_results["results"] if not item["success"]]
                    error = failed[0].get("error") or "One or more BrowserAgent commands failed"
        return await record_scheduled_task_run(
            db,
            task,
            status=status,
            status_code=status_code,
            duration_ms=duration_ms,
            response=response_payload,
            error=error,
            started_at=started_at,
            finished_at=iso_utc(),
        )
    except Exception as exc:
        duration_ms = int((time.perf_counter() - started_clock) * 1000)
        return await record_scheduled_task_run(
            db,
            task,
            status="failed",
            duration_ms=duration_ms,
            error=str(exc),
            started_at=started_at,
            finished_at=iso_utc(),
        )


class ScheduledTaskWorker:
    def __init__(self, *, poll_seconds: float = 30.0, batch_size: int = 10) -> None:
        self.poll_seconds = max(5.0, float(poll_seconds))
        self.batch_size = max(1, int(batch_size))
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="cga-scheduled-task-worker")

    async def stop(self) -> None:
        self._stop.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def run_due_once(self) -> int:
        pool = get_pool()
        async with pool.acquire() as db:
            due = await get_due_scheduled_tasks(db, limit=self.batch_size)
            for task in due:
                await execute_scheduled_task(db, task)
            return len(due)

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                count = await self.run_due_once()
                if count:
                    log.info("schedules.ran_due_tasks", count=count)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("schedules.worker_error", error=str(exc))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
            except asyncio.TimeoutError:
                pass
