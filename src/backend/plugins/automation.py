"""Optional Automation management application; the scheduler remains core."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from backend.auth.database import get_db
from backend.auth.dependencies import require_admin
from backend.auth.pgshim import Connection
from backend.plugins.host import require_plugin
from backend.schedules.models import (
    ScheduledTaskCreate,
    ScheduledTaskList,
    ScheduledTaskOut,
    ScheduledTaskRunOut,
    ScheduledTaskUpdate,
)
from backend.schedules.service import (
    create_scheduled_task,
    delete_scheduled_task,
    execute_scheduled_task,
    get_scheduled_task,
    list_scheduled_tasks,
    update_scheduled_task,
)

router = APIRouter(
    prefix="/admin/schedules", tags=["schedules"],
    dependencies=[Depends(require_admin), Depends(require_plugin("automation"))],
)


@router.get("", response_model=ScheduledTaskList)
async def list_admin_schedules(
    _: dict = Depends(require_admin),
    db: Connection = Depends(get_db),
) -> ScheduledTaskList:
    return await list_scheduled_tasks(db)


@router.post("", response_model=ScheduledTaskOut, status_code=201)
async def create_admin_schedule(
    body: ScheduledTaskCreate,
    _: dict = Depends(require_admin),
    db: Connection = Depends(get_db),
) -> ScheduledTaskOut:
    try:
        return await create_scheduled_task(db, body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/{schedule_id}", response_model=ScheduledTaskOut)
async def update_admin_schedule(
    schedule_id: int,
    body: ScheduledTaskUpdate,
    _: dict = Depends(require_admin),
    db: Connection = Depends(get_db),
) -> ScheduledTaskOut:
    try:
        return await update_scheduled_task(db, schedule_id, body)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Scheduled task not found") from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/{schedule_id}", status_code=204)
async def delete_admin_schedule(
    schedule_id: int,
    _: dict = Depends(require_admin),
    db: Connection = Depends(get_db),
) -> None:
    await delete_scheduled_task(db, schedule_id)


@router.post("/{schedule_id}/run", response_model=ScheduledTaskRunOut)
async def run_admin_schedule_now(
    schedule_id: int,
    _: dict = Depends(require_admin),
    db: Connection = Depends(get_db),
) -> ScheduledTaskRunOut:
    task = await get_scheduled_task(db, schedule_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Scheduled task not found")
    return await execute_scheduled_task(db, task)