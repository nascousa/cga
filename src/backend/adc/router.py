from __future__ import annotations

import difflib

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import Field, field_validator

from backend.adc import service
from backend.adc.remote import RemoteQuery, query
from backend.auth.access import require_project_access
from backend.auth.database import get_db
from backend.auth.dependencies import get_current_user, require_admin
from backend.auth.pgshim import Connection

router = APIRouter(tags=["ADC governance"])


async def project_reader(project_id: int, user: dict = Depends(get_current_user), db: Connection = Depends(get_db)):
    if not await db.raw.fetchval("SELECT 1 FROM projects WHERE id=$1 AND is_active=1", project_id):
        raise HTTPException(404, "Active project not found")
    await require_project_access(db, user, project_id)
    return user


@router.get("/adc/releases")
async def releases(_: dict = Depends(get_current_user), db: Connection = Depends(get_db)):
    return await service.list_releases(db.raw)


@router.get("/adc/releases/{release_id}")
async def release(release_id: int, _: dict = Depends(get_current_user), db: Connection = Depends(get_db)):
    return await service.read_release(db.raw, release_id)


@router.post("/adc/releases", status_code=201)
async def publish(body: service.ReleaseCreate, user: dict = Depends(require_admin), db: Connection = Depends(get_db)):
    return await service.publish(db.raw, body, user["username"])


@router.get("/adc/diff")
async def diff(from_release: int, to_release: int, _: dict = Depends(get_current_user), db: Connection = Depends(get_db)):
    old = await service.read_release(db.raw, from_release)
    new = await service.read_release(db.raw, to_release)
    result = []
    for path in sorted(old["documents"].keys() | new["documents"].keys()):
        a, b = old["documents"].get(path), new["documents"].get(path)
        if a != b:
            result.append({"path": path, "kind": "added" if a is None else "removed" if b is None else "modified",
                           "diff": "".join(difflib.unified_diff((a or "").splitlines(True), (b or "").splitlines(True),
                                                              fromfile=f'{old["version"]}/{path}', tofile=f'{new["version"]}/{path}'))})
    return result


@router.get("/adc/projects/{project_id}")
async def state(project_id: int, revision: int | None = Query(None, ge=1),
                _: dict = Depends(project_reader), db: Connection = Depends(get_db)):
    return await service.project_revision(db.raw, project_id, revision)


@router.get("/adc/projects/{project_id}/history")
async def history(project_id: int, _: dict = Depends(project_reader), db: Connection = Depends(get_db)):
    rows = await db.raw.fetch("""SELECT p.revision,p.release_id,r.version,p.reason,p.actor,p.created_at,p.restored_from
        FROM adc_project_revisions p JOIN adc_releases r ON r.id=p.release_id
        WHERE project_id=$1 ORDER BY revision DESC""", project_id)
    return [dict(row) for row in rows]


@router.post("/adc/projects/{project_id}/revisions", status_code=201)
async def update(project_id: int, body: service.ProjectUpdate, user: dict = Depends(require_admin),
                 db: Connection = Depends(get_db)):
    return await service.save_revision(db.raw, project_id, body, user["username"])


class Restore(service.StrictModel):
    revision: int = Field(ge=1)
    expected_revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=4000)
    _reason = field_validator("reason")(service.ReleaseCreate.nonblank.__func__)


@router.post("/adc/projects/{project_id}/restore", status_code=201)
async def restore(project_id: int, body: Restore, user: dict = Depends(require_admin), db: Connection = Depends(get_db)):
    old = await service.project_revision(db.raw, project_id, body.revision)
    current = await service.project_revision(db.raw, project_id)
    changes = [service.Change.model_validate({k: v for k, v in c.items() if k != "active"}) for c in old["changes"]]
    update_body = service.ProjectUpdate(
        release_id=old["release"]["id"], expected_revision=body.expected_revision, reason=body.reason,
        changes=changes, reviewed_paths=list({c.path for c in changes} | {c["path"] for c in current["changes"]}),
    )
    return await service.save_revision(db.raw, project_id, update_body, user["username"], restored_from=body.revision)


def download(state):
    if not state["release"]:
        raise HTTPException(409, "Bind an ADC release to this project before downloading")
    return Response(service.archive(state), media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="adc-project-{state["project_id"]}-r{state["revision"]}.zip"',
        "Cache-Control": "no-store",
    })


@router.get("/adc/projects/{project_id}/download")
async def project_download(project_id: int, revision: int | None = Query(None, ge=1),
                           _: dict = Depends(project_reader), db: Connection = Depends(get_db)):
    return download(await service.project_revision(db.raw, project_id, revision))


@router.get("/project/adc")
async def agent_state(request: Request, db: Connection = Depends(get_db)):
    project_id = getattr(request.state, "project_db_id", None)
    if project_id is None:
        raise HTTPException(401, "A project-scoped token is required")
    return await service.project_revision(db.raw, project_id)


@router.get("/project/adc/download")
async def agent_download(request: Request, db: Connection = Depends(get_db)):
    return download(await agent_state(request, db))


@router.post("/project/adc/query")
async def agent_query(body: RemoteQuery, request: Request, db: Connection = Depends(get_db)):
    project_id = getattr(request.state, "project_db_id", None)
    if project_id is None:
        raise HTTPException(401, "A project-scoped token is required")
    return await query(db.raw, project_id, body)
