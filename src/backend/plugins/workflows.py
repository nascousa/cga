"""External agent activation and generic HTTP workflow adapter."""
from __future__ import annotations

import httpx

from backend.schedules.models import ScheduledTaskOut


def build_payload(task: ScheduledTaskOut) -> dict:
    if task.task_type == "agent_activation":
        return {
            "action": "activate_agent",
            "agent_id": task.agent_id,
            "project_id": task.project_external_id,
            "payload": task.payload,
        }
    return dict(task.payload or {})


async def execute(task: ScheduledTaskOut, started_at: str, started_clock: float):
    async with httpx.AsyncClient(timeout=float(task.timeout_seconds)) as client:
        response = await client.post(task.target_url, json=build_payload(task))
    try:
        payload = response.json() if response.content else {}
    except ValueError:
        payload = {"body": response.text[:2000]}
    if not isinstance(payload, dict):
        payload = {"body": payload}
    success = response.status_code < 400
    return (
        "success" if success else "failed", response.status_code, payload,
        "" if success else response.text[:1000],
    )
