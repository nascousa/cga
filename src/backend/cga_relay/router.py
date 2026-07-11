"""API bridge for CGA-Relay."""

from __future__ import annotations

import inspect
import re
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


_DEFAULT_REFS = {"", "main", "master", "default"}


def _argument_value(arguments: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in arguments and arguments[name] is not None:
            return arguments[name]
    return None


def _normalize_ref_id(value: Any) -> str:
    return str(value or "").strip()


def _is_default_ref(ref_id: str | None) -> bool:
    return _normalize_ref_id(ref_id).lower() in _DEFAULT_REFS


def _graph_name_component(ref_id: str) -> str:
    component = re.sub(r"[^a-z0-9]+", "_", ref_id.strip().lower()).strip("_")
    if not component:
        raise HTTPException(status_code=400, detail="ref_id must contain letters or numbers")
    return component


def _graph_name_for_project(project_name: str, ref_id: str | None = None) -> str:
    main_graph_name = project_name.strip().lower()
    if _is_default_ref(ref_id):
        return main_graph_name
    return f"{main_graph_name}__ref__{_graph_name_component(_normalize_ref_id(ref_id))}"


def _ref_arguments(arguments: dict[str, Any]) -> tuple[str, str]:
    ref_id = _normalize_ref_id(_argument_value(arguments, "ref_id", "branch", "git_branch"))
    parent_ref = _normalize_ref_id(
        _argument_value(arguments, "parent_ref", "base_ref", "base_branch")
    )
    return ref_id, parent_ref


def _graph_file_count(graph_name: str) -> int:
    if mcp_server._registry is None:
        raise RuntimeError("MCP server not initialized")
    token = _current_project_name.set(graph_name)
    try:
        rows = mcp_server._registry.current().query("MATCH (f:File) RETURN count(f)").result_set
    finally:
        _current_project_name.reset(token)
    return int(rows[0][0]) if rows else 0


def _call_in_graph(graph_name: str, function, **kwargs):
    token = _current_project_name.set(graph_name)
    try:
        return function(**kwargs)
    finally:
        _current_project_name.reset(token)


def _query_graph_scope(
    project_name: str,
    ref_id: str,
    fallback_ref: str,
) -> tuple[str, str, bool]:
    requested_graph_name = _graph_name_for_project(project_name, ref_id)
    graph_name = requested_graph_name
    fallback_graph_used = False
    if not _is_default_ref(ref_id) and fallback_ref and _graph_file_count(requested_graph_name) == 0:
        fallback_graph_name = _graph_name_for_project(project_name, fallback_ref)
        if _graph_file_count(fallback_graph_name) > 0:
            graph_name = fallback_graph_name
            fallback_graph_used = True
    return requested_graph_name, graph_name, fallback_graph_used


async def _promote_ref(arguments: dict[str, Any], project_name: str) -> dict[str, Any]:
    ref_id, parent_ref = _ref_arguments(arguments)
    if not ref_id or _is_default_ref(ref_id):
        raise HTTPException(status_code=400, detail="a non-default ref_id is required")
    repo_path = _argument_value(arguments, "repo_path", "project_root", "root")
    if not repo_path:
        raise HTTPException(status_code=400, detail="repo_path is required")
    if mcp_server._registry is None:
        raise RuntimeError("MCP server not initialized")

    source_graph_name = _graph_name_for_project(project_name, ref_id)
    target_graph_name = _graph_name_for_project(project_name, parent_ref)
    rows = mcp_server._registry.get(source_graph_name).query(
        "MATCH (f:File) RETURN f.path ORDER BY f.path"
    ).result_set
    promoted_files = [str(row[0]) for row in rows if row and row[0]]
    index_result = await mcp_server.index_incremental(
        repo_path=str(repo_path),
        changed_paths=promoted_files,
        project_name=target_graph_name,
    )
    deleted_ref_graph = bool(arguments.get("delete_ref_graph", False))
    if deleted_ref_graph:
        mcp_server._registry.delete(source_graph_name)
    return {
        "promoted_files": promoted_files,
        "source_graph_name": source_graph_name,
        "target_graph_name": target_graph_name,
        "deleted_ref_graph": deleted_ref_graph,
        "index_result": index_result,
    }


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
    ref_id, parent_ref = _ref_arguments(args)
    graph_name = _graph_name_for_project(project_name, ref_id)
    if tool == "index_git_incremental":
        backend_tool = "index_repo_changes"
        repo_path = args.get("repo_path") or args.get("project_root") or args.get("root")
        if not repo_path:
            raise HTTPException(status_code=400, detail="repo_path is required")
        result = await mcp_server.index_repo_changes(
            repo_path=str(repo_path),
            include_untracked=bool(args.get("include_untracked", True)),
            auto_full_on_destructive=bool(args.get("auto_full_on_destructive", False)),
            project_name=graph_name,
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
            project_name=graph_name,
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
        fallback_ref = _normalize_ref_id(args.get("fallback_ref"))
        requested_graph_name, graph_name, fallback_graph_used = _query_graph_scope(
            project_name, ref_id, fallback_ref
        )
        result = _call_in_graph(
            graph_name,
            mcp_server.strategy_query,
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
        fallback_ref = _normalize_ref_id(args.get("fallback_ref"))
        requested_graph_name, graph_name, fallback_graph_used = _query_graph_scope(
            project_name, ref_id, fallback_ref
        )
        result = _call_in_graph(
            graph_name,
            mcp_server.retrieve_context,
            query=str(query),
            limit=int(args.get("limit", 10)),
            task_id=str(args.get("task_id")) if args.get("task_id") else None,
            issue_id=str(args.get("issue_id")) if args.get("issue_id") else None,
            pr_id=str(args.get("pr_id")) if args.get("pr_id") else None,
            activity_id=str(args.get("activity_id")) if args.get("activity_id") else None,
        )
    elif tool == "promote_ref":
        backend_tool = "index_incremental"
        result = await _promote_ref(args, project_name)
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

    response = {
        "ok": True,
        "tool": tool,
        "backend_tool": backend_tool,
        "result": await _maybe_await(result),
    }
    if ref_id or parent_ref or args.get("fallback_ref"):
        response.update(
            {
                "ref_id": ref_id,
                "parent_ref": parent_ref,
                "requested_graph_name": locals().get("requested_graph_name", graph_name),
                "graph_name": graph_name,
                "parent_graph_name": _graph_name_for_project(project_name, parent_ref),
                "fallback_ref": _normalize_ref_id(args.get("fallback_ref")),
                "fallback_graph_used": locals().get("fallback_graph_used", False),
            }
        )
    return response


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
