"""Shared project-scoped ADC operations for REST, MCP and Desktop Relay."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Literal

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import Field, field_validator

from backend.adc import service

OPERATIONS = ("adc_catalog", "adc_release", "adc_current", "adc_history",
              "adc_diff", "adc_document", "adc_bundle")


class RemoteQuery(service.StrictModel):
    operation: Literal["adc_catalog", "adc_release", "adc_current", "adc_history",
                       "adc_diff", "adc_document", "adc_bundle"]
    revision: int | None = Field(default=None, ge=1)
    release_id: int | None = Field(default=None, ge=1)
    path: str | None = None
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=100)

    @field_validator("path")
    @classmethod
    def safe_path(cls, value):
        return service.document_path(value) if value is not None else None


async def query(conn, project_id: int, request: RemoteQuery):
    result = await _query(conn, project_id, request)
    # Leave headroom for the authenticated relay envelope within its 8 MiB limit.
    if len(json.dumps(jsonable_encoder(result), ensure_ascii=False).encode()) > 7 * 1024 * 1024:
        raise HTTPException(413, "ADC response exceeds 7 MiB; use adc_document, pagination, or /api/project/adc/download")
    return result


async def _query(conn, project_id: int, request: RemoteQuery):
    project = await conn.fetchrow(
        "SELECT id,project_id,project_name FROM projects WHERE id=$1 AND is_active=1", project_id)
    if project is None:
        raise HTTPException(404, "Active project not found")
    if request.operation == "adc_catalog":
        releases = await service.list_releases(conn)
        return {"latest": releases[0] if releases else None, "total": len(releases),
                "releases": releases[request.offset:request.offset + request.limit]}
    if request.operation == "adc_release":
        if request.release_id is None:
            raise HTTPException(422, "release_id is required")
        return await service.read_release(conn, request.release_id)
    if request.operation == "adc_history":
        rows = await conn.fetch("""SELECT p.revision,p.release_id,r.version,p.reason,p.actor,p.created_at,p.restored_from
            FROM adc_project_revisions p JOIN adc_releases r ON r.id=p.release_id
            WHERE project_id=$1 ORDER BY revision DESC OFFSET $2 LIMIT $3""",
            project_id, request.offset, request.limit)
        total = await conn.fetchval("SELECT count(*) FROM adc_project_revisions WHERE project_id=$1", project_id)
        return {"project_id": project["project_id"], "total": total, "history": [dict(r) for r in rows]}
    state = await service.project_revision(conn, project_id, request.revision)
    state["project_external_id"] = project["project_id"]
    if request.operation == "adc_current":
        return state
    if state["release"] is None:
        raise HTTPException(409, "Project has no approved ADC baseline; ask an administrator to adopt a release")
    if request.operation == "adc_document":
        if request.path is None:
            raise HTTPException(422, "path is required")
        content = state["documents"].get(request.path)
        if content is None:
            raise HTTPException(404, "Document is absent or exempted in this project revision")
        return {"project_id": project["project_id"], "revision": state["revision"],
                "release_id": state["release_id"], "path": request.path, "content": content,
                "sha256": hashlib.sha256(content.encode()).hexdigest(), "evaluated_at": state["evaluated_at"]}
    if request.operation == "adc_diff":
        import difflib
        if request.release_id is None:
            raise HTTPException(422, "release_id is required")
        target = await service.read_release(conn, request.release_id)
        before, after = state["release"]["documents"], target["documents"]
        overlay_paths = {c["path"] for c in state["changes"]}
        differences = []
        for path in sorted(before.keys() | after.keys()):
            if before.get(path) != after.get(path):
                differences.append({
                    "path": path, "requires_project_review": path in overlay_paths,
                    "diff": "".join(difflib.unified_diff(
                        before.get(path, "").splitlines(True), after.get(path, "").splitlines(True),
                        fromfile=f'{state["release"]["version"]}/{path}', tofile=f'{target["version"]}/{path}')),
                })
        return {"from_release": state["release"]["id"], "to_release": target["id"],
                "project_revision": state["revision"], "differences": differences}
    files = [{"path": path, "content": content, "sha256": hashlib.sha256(content.encode()).hexdigest()}
             for path, content in sorted(state["documents"].items())]
    metadata = {k: v for k, v in state.items() if k != "documents"}
    metadata["release"] = {k: v for k, v in metadata["release"].items() if k != "documents"}
    metadata["files"] = [{"path": f["path"], "sha256": f["sha256"]} for f in files]
    lock = json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2, default=str)
    files.append({"path": ".adc/adc-lock.json", "content": lock,
                  "sha256": hashlib.sha256(lock.encode()).hexdigest()})
    # Inactive historical exports are inspectable, but only current snapshots are
    # eligible for automatic installation by the local relay.
    return {"schema": 1, "project_id": project["project_id"], "revision": state["revision"],
            "release_id": state["release_id"], "version": state["release"]["version"],
            "historical": request.revision is not None, "files": files,
            "generated_at": datetime.now(timezone.utc)}
