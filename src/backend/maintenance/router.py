from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from backend.auth.dependencies import get_current_user, require_admin
from backend.maintenance import retention, updates
from backend.maintenance.history import history

router = APIRouter()


class PolicySave(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy: retention.RetentionPolicy
    preview_token: str


class Confirmation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    confirm: bool


class InstallRequest(Confirmation):
    version: str
    image: str


@router.get("/admin/audit-retention")
async def audit_retention(_: dict = Depends(require_admin)):
    return await retention.status()


@router.post("/admin/audit-retention/preview")
async def audit_preview(policy: retention.RetentionPolicy, _: dict = Depends(require_admin)):
    return await retention.preview(policy)


@router.put("/admin/audit-retention")
async def audit_policy(payload: PolicySave, _: dict = Depends(require_admin)):
    try:
        return await retention.save_policy(payload.policy, payload.preview_token)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/admin/audit-retention/run")
async def audit_cleanup(payload: Confirmation, _: dict = Depends(require_admin)):
    if not payload.confirm:
        raise HTTPException(400, "Explicit cleanup confirmation is required.")
    return await retention.cleanup()


@router.get("/change-logs")
async def change_logs(request: Request, _: dict = Depends(get_current_user)):
    return history(request.app.version)


@router.get("/updates")
async def update_status(request: Request, _: dict = Depends(get_current_user)):
    return await updates.check(request.app.version)


@router.post("/admin/updates/check")
async def update_check(request: Request, _: dict = Depends(require_admin)):
    return await updates.check(request.app.version, force=True)


@router.post("/admin/updates/install")
async def install_update(request: Request, payload: InstallRequest, user: dict = Depends(require_admin)):
    if not payload.confirm:
        raise HTTPException(400, "Explicit upgrade confirmation is required.")
    try:
        return await updates.request_install(request.app.version, payload.version, payload.image, user["username"])
    except (ValueError, OSError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/admin/updates/cancel")
async def cancel_update(payload: Confirmation, _: dict = Depends(require_admin)):
    if not payload.confirm:
        raise HTTPException(400, "Explicit cancellation confirmation is required.")
    try:
        return updates.cancel_queued()
    except (ValueError, OSError) as exc:
        raise HTTPException(409, str(exc)) from exc
