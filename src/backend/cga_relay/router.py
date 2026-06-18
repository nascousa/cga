"""API bridge for CGA-Relay."""

from __future__ import annotations

import inspect
import time
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from backend.auth import pgshim as aiosqlite
from backend.auth.access import require_project_access
from backend.auth.context import _current_project_db_id, _current_project_external_id
from backend.auth.crystals import require_crystal_suite
from backend.auth.database import get_db, insert_audit_log
from backend.auth.dependencies import get_current_user
from backend.graph.registry import _current_project_name
from backend.tools import server as mcp_server

log = structlog.get_logger()
router = APIRouter(prefix="/project/cga-relay", tags=["cga-relay"])
account_router = APIRouter(prefix="/auth/cga-relay", tags=["cga-relay"])


class CgaRelayToolCall(BaseModel):
    tool: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    project_id: str | None = None


class CgaRelaySync(BaseModel):
    agent_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    namespace: str | None = None
    project_tag: str | None = None
    root: str | None = None
    counts: dict[str, Any] = Field(default_factory=dict)
    snapshots: list[dict[str, Any]] = Field(default_factory=list)
    tombstones: list[str] = Field(default_factory=list)


def _project_context(request: Request) -> dict[str, Any]:
    state = request.scope.get("state", {})
    project_id = str(state.get("project_id") or "").strip()
    project_name = str(state.get("project_name") or "").strip()
    if not project_id or not project_name:
        raise HTTPException(status_code=401, detail="Project token required")
    return {
        "project_id": project_id,
        "project_name": project_name,
        "project_db_id": state.get("project_db_id"),
        "project_token_id": state.get("project_token_id"),
        "project_token_type": state.get("project_token_type"),
    }


def _require_project_match(bound_project_id: str, payload_project_id: str | None) -> str:
    cleaned = (payload_project_id or bound_project_id).strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="project_id is required")
    if cleaned != bound_project_id:
        raise HTTPException(status_code=403, detail="project_id must match authenticated project")
    return cleaned


async def _account_project_context(
    db: aiosqlite.Connection,
    project_id: str | None,
    user: dict,
) -> dict[str, Any]:
    cleaned = (project_id or "").strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="project_id is required")
    async with db.execute(
        "SELECT id, project_name, project_id FROM projects WHERE project_id = ? AND is_active = 1",
        (cleaned,),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Project not found")
    await require_project_access(db, user, int(row["id"]))
    return {
        "project_id": str(row["project_id"]),
        "project_name": str(row["project_name"]),
        "project_db_id": int(row["id"]),
    }


async def _dispatch_with_project_context(
    tool: str,
    arguments: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    project_name_var = _current_project_name.set(context["project_name"].strip().lower())
    project_id_var = _current_project_external_id.set(context["project_id"])
    project_db_var = _current_project_db_id.set(int(context["project_db_id"]))
    try:
        result = await dispatch_tool(tool, arguments, context["project_name"])
    finally:
        _current_project_db_id.reset(project_db_var)
        _current_project_external_id.reset(project_id_var)
        _current_project_name.reset(project_name_var)
    result["project_id"] = context["project_id"]
    return result


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def dispatch_tool(tool: str, arguments: dict[str, Any], project_name: str) -> dict[str, Any]:
    """Dispatch a CGA-Relay tool call into existing CGA MCP tool functions."""
    args = dict(arguments or {})
    if tool == "index_git_incremental":
        backend_tool = "index_repo_changes"
        repo_path = args.get("repo_path") or args.get("project_root") or args.get("root")
        if not repo_path:
            raise HTTPException(status_code=400, detail="repo_path is required")
        result = await mcp_server.index_repo_changes(
            repo_path=str(repo_path),
            include_untracked=bool(args.get("include_untracked", True)),
            auto_full_on_destructive=bool(args.get("auto_full_on_destructive", False)),
            project_name=project_name,
        )
    elif tool == "index_incremental":
        backend_tool = "index_incremental"
        repo_path = args.get("repo_path") or args.get("project_root") or args.get("root")
        changed_paths = args.get("changed_paths") or args.get("paths") or []
        if not repo_path:
            raise HTTPException(status_code=400, detail="repo_path is required")
        if not isinstance(changed_paths, list):
            raise HTTPException(status_code=400, detail="changed_paths must be a list")
        result = await mcp_server.index_incremental(
            repo_path=str(repo_path),
            changed_paths=[str(path) for path in changed_paths],
            project_name=project_name,
        )
    elif tool == "index_progress":
        backend_tool = "get_index_job_status"
        job_id = args.get("job_id")
        if not job_id:
            raise HTTPException(status_code=400, detail="job_id is required")
        result = await mcp_server.get_index_job_status(job_id=str(job_id))
    elif tool in {"query_impact_graph", "get_optimized_context"}:
        backend_tool = "strategy_query"
        query = args.get("query") or args.get("question")
        if not query:
            raise HTTPException(status_code=400, detail="query is required")
        raw_token_budget = args.get("token_budget")
        result = mcp_server.strategy_query(
            query=str(query),
            graph_top_k=int(args.get("graph_top_k", 8)),
            min_graph_hits=int(args.get("min_graph_hits", 3)),
            token_budget=int(raw_token_budget) if raw_token_budget is not None else None,
            relation_depth=int(args.get("relation_depth", 1)),
            fallback_max_files=int(args.get("fallback_max_files", 3)),
        )
    elif tool == "fetch_minimal_code":
        backend_tool = "retrieve_context"
        query = args.get("query") or args.get("symbol")
        if not query:
            raise HTTPException(status_code=400, detail="query is required")
        result = mcp_server.retrieve_context(
            query=str(query),
            limit=int(args.get("limit", 10)),
            task_id=str(args.get("task_id")) if args.get("task_id") else None,
            issue_id=str(args.get("issue_id")) if args.get("issue_id") else None,
            pr_id=str(args.get("pr_id")) if args.get("pr_id") else None,
            activity_id=str(args.get("activity_id")) if args.get("activity_id") else None,
        )
    elif tool == "health_check":
        backend_tool = "health_check"
        result = {"status": "ok", "service": "cga-relay-bridge"}
    elif tool == "getstarted":
        backend_tool = "getstarted"
        result = {
            "status": "ok",
            "message": "Use cga-relay over stdio with a machine-local config file.",
        }
    else:
        raise HTTPException(status_code=400, detail=f"unknown CGA-Relay tool: {tool}")

    return {
        "ok": True,
        "tool": tool,
        "backend_tool": backend_tool,
        "result": await _maybe_await(result),
    }


def sync_summary(payload: CgaRelaySync) -> dict[str, Any]:
    """Return a metadata-only sync summary; never include snapshot contents."""
    return {
        "agent_id": payload.agent_id,
        "project_id": payload.project_id,
        "namespace": payload.namespace,
        "project_tag": payload.project_tag,
        "root": payload.root,
        "counts": payload.counts,
        "snapshot_count": len(payload.snapshots),
        "tombstone_count": len(payload.tombstones),
    }


@router.post("/mcp-tool")
async def call_cga_relay_tool(payload: CgaRelayToolCall, request: Request) -> dict[str, Any]:
    context = _project_context(request)
    project_id = _require_project_match(context["project_id"], payload.project_id)
    result = await dispatch_tool(payload.tool, payload.arguments, context["project_name"])
    result["project_id"] = project_id
    return result


@router.post("/sync")
async def receive_cga_relay_sync(payload: CgaRelaySync, request: Request) -> dict[str, Any]:
    started = time.perf_counter()
    context = _project_context(request)
    project_id = _require_project_match(context["project_id"], payload.project_id)
    if len(payload.snapshots) > 500:
        raise HTTPException(status_code=413, detail="too many snapshots in one sync request")

    summary = sync_summary(payload)
    try:
        await insert_audit_log(
            scope="project",
            method="POST",
            path="/api/project/cga-relay/sync",
            status_code=202,
            duration_ms=int((time.perf_counter() - started) * 1000),
            actor_type="project_token",
            project_id=context.get("project_db_id"),
            project_name=context.get("project_name"),
            token_id=context.get("project_token_id"),
            details={
                "agent_id": payload.agent_id,
                "project_id": project_id,
                "namespace": payload.namespace,
                "project_tag": payload.project_tag,
                "counts": payload.counts,
                "snapshot_count": len(payload.snapshots),
                "tombstone_count": len(payload.tombstones),
            },
        )
    except Exception as exc:  # pragma: no cover - audit storage is environment-dependent
        log.warning("cga_relay.sync.audit_failed", error=str(exc), project_id=project_id)

    return {
        "accepted": True,
        **summary,
    }


@account_router.post("/mcp-tool")
async def call_account_cga_relay_tool(
    payload: CgaRelayToolCall,
    _: None = Depends(require_crystal_suite),
    user: dict = Depends(get_current_user),
    db: aiosqlite.Connection = Depends(get_db),
) -> dict[str, Any]:
    project_id = payload.project_id or str(payload.arguments.get("project_id") or "")
    context = await _account_project_context(db, project_id, user)
    result = await _dispatch_with_project_context(payload.tool, payload.arguments, context)
    result["actor_type"] = "account"
    return result


@account_router.post("/sync")
async def receive_account_cga_relay_sync(
    payload: CgaRelaySync,
    _: None = Depends(require_crystal_suite),
    user: dict = Depends(get_current_user),
    db: aiosqlite.Connection = Depends(get_db),
) -> dict[str, Any]:
    started = time.perf_counter()
    context = await _account_project_context(db, payload.project_id, user)
    if len(payload.snapshots) > 500:
        raise HTTPException(status_code=413, detail="too many snapshots in one sync request")

    summary = sync_summary(payload)
    try:
        await insert_audit_log(
            scope="account",
            method="POST",
            path="/api/auth/cga-relay/sync",
            status_code=202,
            duration_ms=int((time.perf_counter() - started) * 1000),
            actor_type="user_token",
            actor_id=int(user["id"]),
            actor_name=str(user.get("username") or ""),
            project_id=context.get("project_db_id"),
            project_name=context.get("project_name"),
            details={
                "agent_id": payload.agent_id,
                "project_id": context["project_id"],
                "namespace": payload.namespace,
                "project_tag": payload.project_tag,
                "counts": payload.counts,
                "snapshot_count": len(payload.snapshots),
                "tombstone_count": len(payload.tombstones),
            },
        )
    except Exception as exc:  # pragma: no cover - audit storage is environment-dependent
        log.warning("cga_relay.account_sync.audit_failed", error=str(exc), project_id=context["project_id"])

    return {
        "accepted": True,
        **summary,
    }
