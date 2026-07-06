"""Admin API for CGA extensions."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.auth.database import get_db
from backend.auth.dependencies import require_admin
from backend.auth.pgshim import Connection
from backend.extensions.models import (
    ExtensionConfigOut,
    ExtensionConfigUpdate,
    ExtensionList,
    ExtensionProjectOverview,
    ExtensionRunOut,
    ExtensionRunRequest,
)
from backend.extensions.service import (
    ExtensionNotFoundError,
    ExtensionProjectNotFoundError,
    get_extension_config,
    get_extension_overview,
    get_extension_project_overview,
    list_extension_runs,
    list_extensions,
    run_project_extension,
    update_extension_config,
)

router = APIRouter(prefix="/admin/extensions", tags=["extensions"])


def _not_found(exc: Exception) -> HTTPException:
    if isinstance(exc, ExtensionProjectNotFoundError):
        return HTTPException(status_code=404, detail="Project not found")
    return HTTPException(status_code=404, detail="Extension not found")


@router.get("", response_model=ExtensionList)
async def list_admin_extensions(
    _: dict = Depends(require_admin),
) -> ExtensionList:
    return await list_extensions()


@router.get("/{extension_id}/projects/{project_id}", response_model=ExtensionProjectOverview)
async def get_admin_extension_project_overview(
    extension_id: str,
    project_id: int,
    _: dict = Depends(require_admin),
    db: Connection = Depends(get_db),
) -> ExtensionProjectOverview:
    try:
        return await get_extension_project_overview(db, extension_id, project_id)
    except (ExtensionNotFoundError, ExtensionProjectNotFoundError) as exc:
        raise _not_found(exc) from exc


@router.get("/{extension_id}", response_model=ExtensionProjectOverview)
async def get_admin_extension_overview(
    extension_id: str,
    _: dict = Depends(require_admin),
    db: Connection = Depends(get_db),
) -> ExtensionProjectOverview:
    try:
        return await get_extension_overview(db, extension_id)
    except ExtensionNotFoundError as exc:
        raise _not_found(exc) from exc


@router.get("/{extension_id}/config", response_model=ExtensionConfigOut)
async def get_admin_platform_extension_config(
    extension_id: str,
    _: dict = Depends(require_admin),
    db: Connection = Depends(get_db),
) -> ExtensionConfigOut:
    try:
        return await get_extension_config(db, extension_id)
    except ExtensionNotFoundError as exc:
        raise _not_found(exc) from exc


@router.put("/{extension_id}/config", response_model=ExtensionConfigOut)
async def update_admin_platform_extension_config(
    extension_id: str,
    body: ExtensionConfigUpdate,
    _: dict = Depends(require_admin),
    db: Connection = Depends(get_db),
) -> ExtensionConfigOut:
    try:
        return await update_extension_config(db, extension_id, None, body)
    except ExtensionNotFoundError as exc:
        raise _not_found(exc) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{extension_id}/run", response_model=ExtensionRunOut)
async def run_admin_platform_extension(
    extension_id: str,
    body: ExtensionRunRequest | None = None,
    _: dict = Depends(require_admin),
    db: Connection = Depends(get_db),
) -> ExtensionRunOut:
    try:
        return await run_project_extension(db, extension_id, config_override=(body or ExtensionRunRequest()).config_override)
    except ExtensionNotFoundError as exc:
        raise _not_found(exc) from exc


@router.get("/{extension_id}/runs", response_model=list[ExtensionRunOut])
async def list_admin_platform_extension_runs(
    extension_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    _: dict = Depends(require_admin),
    db: Connection = Depends(get_db),
) -> list[ExtensionRunOut]:
    try:
        return await list_extension_runs(db, extension_id, limit=limit)
    except ExtensionNotFoundError as exc:
        raise _not_found(exc) from exc


@router.get("/{extension_id}/projects/{project_id}/config", response_model=ExtensionConfigOut)
async def get_admin_extension_config(
    extension_id: str,
    project_id: int,
    _: dict = Depends(require_admin),
    db: Connection = Depends(get_db),
) -> ExtensionConfigOut:
    try:
        return await get_extension_config(db, extension_id, project_id)
    except (ExtensionNotFoundError, ExtensionProjectNotFoundError) as exc:
        raise _not_found(exc) from exc


@router.put("/{extension_id}/projects/{project_id}/config", response_model=ExtensionConfigOut)
async def update_admin_extension_config(
    extension_id: str,
    project_id: int,
    body: ExtensionConfigUpdate,
    _: dict = Depends(require_admin),
    db: Connection = Depends(get_db),
) -> ExtensionConfigOut:
    try:
        return await update_extension_config(db, extension_id, project_id, body)
    except (ExtensionNotFoundError, ExtensionProjectNotFoundError) as exc:
        raise _not_found(exc) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{extension_id}/projects/{project_id}/run", response_model=ExtensionRunOut)
async def run_admin_extension(
    extension_id: str,
    project_id: int,
    body: ExtensionRunRequest | None = None,
    _: dict = Depends(require_admin),
    db: Connection = Depends(get_db),
) -> ExtensionRunOut:
    try:
        return await run_project_extension(db, extension_id, project_id, config_override=(body or ExtensionRunRequest()).config_override)
    except (ExtensionNotFoundError, ExtensionProjectNotFoundError) as exc:
        raise _not_found(exc) from exc


@router.get("/{extension_id}/projects/{project_id}/runs", response_model=list[ExtensionRunOut])
async def list_admin_extension_runs(
    extension_id: str,
    project_id: int,
    limit: int = Query(default=20, ge=1, le=100),
    _: dict = Depends(require_admin),
    db: Connection = Depends(get_db),
) -> list[ExtensionRunOut]:
    try:
        return await list_extension_runs(db, extension_id, project_id, limit=limit)
    except (ExtensionNotFoundError, ExtensionProjectNotFoundError) as exc:
        raise _not_found(exc) from exc
