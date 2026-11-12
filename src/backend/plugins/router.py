"""Authenticated first-party plugin installation and activation."""
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, ValidationError

from backend import runtime_config
from backend.auth.dependencies import require_admin
from backend.auth.database import get_db
from backend.auth.pgshim import Connection
from backend.plugins.host import PluginUnavailableError, catalog_status, execution, task_plugin, update_selection
from backend.plugins import host, settings
from backend.schedules.models import ScheduledTaskCreate, ScheduledTaskOut

router = APIRouter(prefix="/admin/plugins", tags=["plugins"], dependencies=[Depends(require_admin)])


class PluginSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    installed: list[str]
    enabled: list[str]


@router.get("")
async def list_plugins() -> dict:
    return catalog_status()


@router.put("")
async def select_plugins(body: PluginSelection) -> dict:
    try:
        return update_selection(body.model_dump())
    except PluginUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except runtime_config.RuntimeConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def installed_plugin(plugin_id: str) -> None:
    if plugin_id not in host.CATALOG:
        raise HTTPException(status_code=404, detail="Unknown plugin")
    if plugin_id not in host.selection()["installed"]:
        raise HTTPException(status_code=409, detail="Install the plugin before configuring it")


@router.get("/{plugin_id}/settings")
async def plugin_settings(plugin_id: str, request: Request) -> dict:
    installed_plugin(plugin_id)
    response: dict = {"id": plugin_id, "enabled": host.is_enabled(plugin_id), "scope": "runtime"}
    if plugin_id in settings.MODELS:
        response["values"] = settings.get_settings(plugin_id)
    elif plugin_id == "notifications":
        response["values"] = runtime_config.get_runtime_config()["smtp"]
    else:
        response["values"] = {}
    if plugin_id == "automation":
        worker = getattr(request.app.state, "scheduled_task_worker", None)
        task = worker._task if worker else None
        response["status"] = {"scheduler_running": bool(task and not task.done())}
    if plugin_id == "evaluation":
        response["status"] = {
            "tracing_allowed": settings.tracing_enabled(),
            "worker_running": bool(getattr(getattr(request.app.state, "trace_evaluator", None), "_running", False)),
            "restart_note": "Enabling background tracing after startup requires an API restart; benchmarks do not.",
        }
    if plugin_id == "reports":
        response["scope"] = "browser"
    return response


@router.put("/{plugin_id}/settings")
async def update_plugin_settings(plugin_id: str, body: dict, request: Request) -> dict:
    installed_plugin(plugin_id)
    try:
        if plugin_id in settings.MODELS:
            settings.save_settings(plugin_id, body)
        elif plugin_id == "notifications":
            allowed = {"enabled", "host", "port", "security", "username", "from_email", "from_name"}
            if set(body) - allowed:
                raise ValueError("Unknown SMTP fields; use CGA_SMTP_PASSWORD in the environment for credentials")
            runtime_config.update_runtime_config({"smtp": body})
        else:
            raise HTTPException(status_code=400, detail="Use this plugin's existing configuration editor")
    except ValidationError as exc:
        errors = exc.errors(include_input=False, include_context=False, include_url=False)
        detail = "; ".join(f"{'.'.join(map(str, error['loc']))}: {error['msg']}" for error in errors)
        raise HTTPException(status_code=400, detail=detail) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await plugin_settings(plugin_id, request)


@router.post("/{plugin_id}/tasks", response_model=ScheduledTaskOut, status_code=201)
async def create_plugin_task(
    plugin_id: str, body: ScheduledTaskCreate, db: Connection = Depends(get_db),
) -> ScheduledTaskOut:
    if plugin_id not in {"browseragent", "workflows", "azure_policy_change_monitor"}:
        raise HTTPException(status_code=404, detail="Plugin does not provide scheduled tasks")
    try:
        if task_plugin(body.task_type, body.payload) != plugin_id:
            raise HTTPException(status_code=400, detail="Task type does not belong to this plugin")
        with execution(plugin_id):
            from backend.schedules.service import create_scheduled_task
            return await create_scheduled_task(db, body)
    except PluginUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/{plugin_id}/tasks", response_model=list[ScheduledTaskOut])
async def list_plugin_tasks(
    plugin_id: str, limit: int = Query(default=100, ge=1, le=500),
    db: Connection = Depends(get_db),
) -> list[ScheduledTaskOut]:
    if plugin_id not in {"browseragent", "workflows", "azure_policy_change_monitor"}:
        raise HTTPException(status_code=404, detail="Plugin does not provide scheduled tasks")
    from backend.schedules.service import TASK_SELECT, _task_from_row
    params: tuple[str, ...]
    if plugin_id == "workflows":
        where, params = "st.task_type IN (?, ?)", ("agent_activation", "http_request")
    elif plugin_id == "browseragent":
        where, params = "st.task_type = ?", ("browseragent_task",)
    else:
        where = "st.task_type = ? AND st.payload_json::jsonb ->> 'extension_id' = ?"
        params = ("extension_task", plugin_id)
    async with db.execute(
        TASK_SELECT + " WHERE " + where + " ORDER BY st.id DESC LIMIT ?",
        (*params, limit),
    ) as cursor:
        rows = await cursor.fetchall()
    return [_task_from_row(row) for row in rows]
