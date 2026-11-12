"""BrowserAgent task adapter, loaded only when selected."""
from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import quote, urlparse, urlunparse

import httpx

from backend.schedules.models import ScheduledTaskOut
from backend.schedules.service import _compact_response_value

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
    raw = spec.get("artifacts")
    if not isinstance(raw, dict):
        raw = {}
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
        data = result_payload.get("data")
        if not isinstance(data, dict):
            data = {}
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
    commands = request_payload.get("commands")
    if not isinstance(commands, list):
        commands = []
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


async def execute(task: ScheduledTaskOut, started_at: str, started_clock: float):
    from backend.schedules.service import build_request_payload
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
    return status, status_code, response_payload, error
