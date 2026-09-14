"""API bridge for CGA-Relay."""

from __future__ import annotations

import inspect
import asyncio
import ast
import json
import re
import time
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from redis.exceptions import RedisError

from backend.auth import pgshim as aiosqlite
from backend.auth.access import (
    authorized_repo_root,
    registered_project_scope,
    require_project_access,
)
from backend.auth.context import (
    ProjectScope,
    authorized_graph_name,
    bind_project_ref,
    bind_project_scope,
    branch_graph_name,
    is_default_ref,
    require_project_scope,
    validate_project_graph_name,
)
from backend.auth.crystals import require_crystal_suite
from backend.auth.database import get_db, insert_audit_log
from backend.auth.dependencies import get_current_user
from backend.auth.router import _effective_output_rules
from backend.graph.client import GraphGenerationChanged
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


PROMOTION_TIMEOUT_SECONDS = 120.0


def _argument_value(arguments: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in arguments and arguments[name] is not None:
            return arguments[name]
    return None


def _normalize_ref_id(value: Any) -> str:
    return str(value or "").strip()


def _is_default_ref(ref_id: str | None) -> bool:
    return is_default_ref(_normalize_ref_id(ref_id))


def _graph_name_for_project(project_name: str, ref_id: str | None = None) -> str:
    scope = require_project_scope()
    try:
        main_graph_name = validate_project_graph_name(project_name)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if scope.graph_name != main_graph_name:
        raise HTTPException(status_code=403, detail="Project does not match authenticated graph owner")
    if _is_default_ref(ref_id):
        return main_graph_name
    return branch_graph_name(scope.project_id, _normalize_ref_id(ref_id))


def _graph_connection():
    if mcp_server._registry is None:
        raise RuntimeError("MCP server not initialized")
    scope = require_project_scope()
    graph = mcp_server._registry.get(scope.graph_name)
    if graph._db is None:
        raise HTTPException(status_code=503, detail="Graph ownership verification is unavailable")
    return graph._db.connection


def _require_ref_migration(project_name: str, ref_id: str) -> str:
    """Never infer legacy branch ownership from its collision-prone graph name."""
    graph_name = _graph_name_for_project(project_name, ref_id)
    if _is_default_ref(ref_id):
        return graph_name
    component = re.sub(r"[^a-z0-9]+", "_", ref_id.lower()).strip("_")
    if not component:
        return graph_name
    legacy = f"{require_project_scope().graph_name}__ref__{component}"
    connection = _graph_connection()
    if connection.exists(legacy):
        marker = connection.get(f"cga:ref-migration:v2:{graph_name}")
        try:
            acknowledged = json.loads(marker) if marker else None
        except (TypeError, ValueError):
            acknowledged = None
        expected = {
            "version": 2,
            "project_id": require_project_scope().project_id,
            "ref_id": ref_id,
            "legacy_graph_name": legacy,
        }
        if acknowledged != expected:
            raise HTTPException(
                status_code=409,
                detail="Legacy ref graph ownership is ambiguous. An administrator must back up and migrate this ref; see docs/BRANCH-GRAPHS.md.",
            )
    return graph_name


def _ref_arguments(arguments: dict[str, Any]) -> tuple[str, str]:
    ref_id = _normalize_ref_id(_argument_value(arguments, "ref_id", "branch", "git_branch"))
    parent_ref = _normalize_ref_id(
        _argument_value(arguments, "parent_ref", "base_ref", "base_branch")
    )
    return ref_id, parent_ref


def _graph_file_count(graph_name: str) -> int:
    if mcp_server._registry is None:
        raise RuntimeError("MCP server not initialized")
    authorized_graph_name(graph_name)
    rows = mcp_server._registry.get(graph_name).query("MATCH (f:File) RETURN count(f)").result_set
    return int(rows[0][0]) if rows else 0


def _call_in_graph(graph_name: str, function, **kwargs):
    authorized_graph_name(graph_name)
    return function(**kwargs)


def _query_graph_scope(
    project_name: str,
    ref_id: str,
    fallback_ref: str,
) -> tuple[str, str, bool]:
    requested_graph_name = _require_ref_migration(project_name, ref_id)
    graph_name = requested_graph_name
    fallback_graph_used = False
    if not _is_default_ref(ref_id) and fallback_ref:
        with bind_project_ref(ref_id):
            file_count = _graph_file_count(requested_graph_name)
        if file_count == 0:
            fallback_graph_name = _require_ref_migration(project_name, fallback_ref)
            with bind_project_ref(fallback_ref):
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
    delete_requested = arguments.get("delete_ref_graph", False)
    if not isinstance(delete_requested, bool):
        raise HTTPException(status_code=400, detail="delete_ref_graph must be a boolean")
    if mcp_server._registry is None:
        raise RuntimeError("MCP server not initialized")

    root = authorized_repo_root(str(repo_path))
    source_graph_name = _require_ref_migration(project_name, ref_id)
    target_graph_name = _require_ref_migration(project_name, parent_ref)
    if source_graph_name == target_graph_name:
        raise HTTPException(status_code=400, detail="Source and target refs must be different")
    if not _graph_connection().exists(source_graph_name):
        raise HTTPException(status_code=404, detail="Source ref graph does not exist")
    with bind_project_ref(ref_id):
        source_generation = _graph_generation(source_graph_name)

    # Full rebuilding observes deletions even when no File node survives for them.
    with bind_project_ref(parent_ref):
        initial_generation = _graph_generation(target_graph_name)
        try:
            submitted = await mcp_server.index_full(repo_path=str(root), project_name=target_graph_name)
        except ValueError:
            submitted = {"status": "failed", "reason": "full_rebuild_rejected"}
        index_result = submitted
        if submitted.get("status") == "queued" and submitted.get("job_id"):
            try:
                index_result = await asyncio.wait_for(
                    mcp_server.wait_for_index_ready(
                        job_id=str(submitted["job_id"]),
                        timeout_sec=PROMOTION_TIMEOUT_SECONDS,
                        poll_interval_sec=0.25,
                    ),
                    timeout=PROMOTION_TIMEOUT_SECONDS + 1,
                )
            except TimeoutError:
                index_result = {**submitted, "ready": False, "timeout": True}
    eligible = _successful_promotion(index_result, target_graph_name)
    completed = False
    verification_failed = False
    if eligible:
        with bind_project_ref(parent_ref):
            try:
                completed = (
                    bool(_graph_connection().exists(target_graph_name))
                    and _graph_generation(target_graph_name) != initial_generation
                    and _graph_file_count(target_graph_name) > 0
                )
            except (RedisError, RuntimeError) as exc:
                log.error("relay.promotion_verification_failed", graph=target_graph_name, error=str(exc))
                verification_failed = True
    state = str(index_result.get("status", ""))
    if completed:
        outcome, reason = "done", "target_full_rebuild_published"
    elif verification_failed:
        outcome, reason = "failed", "target_publication_verification_failed"
    elif _completed_index_result(index_result, target_graph_name) and _promotion_file_count(index_result) == 0:
        outcome, reason = "noop", "empty_full_rebuild"
    elif eligible or state in {"noop", "skipped"}:
        outcome, reason = "noop", "target_not_published_or_empty"
    elif index_result.get("timeout") or state in {"queued", "processing", "retrying", "not_found"}:
        outcome, reason = "pending", "target_index_not_ready"
    else:
        outcome, reason = "failed", "target_full_rebuild_failed_or_unverified"
    deleted_ref_graph = completed and delete_requested
    if deleted_ref_graph:
        try:
            mcp_server._registry.delete(source_graph_name, expected_generation=source_generation)
        except GraphGenerationChanged:
            deleted_ref_graph = False
            reason = "target_published_source_changed_and_retained"
            log.warning("relay.promotion_source_changed", graph=source_graph_name)
    return {
        "status": outcome,
        "reason": reason,
        "rebuild_mode": "full",
        "source_graph_name": source_graph_name,
        "target_graph_name": target_graph_name,
        "deleted_ref_graph": deleted_ref_graph,
        "submitted_job": submitted,
        "index_result": index_result,
    }


def _successful_promotion(result: dict, target_graph_name: str) -> bool:
    files = _promotion_file_count(result)
    return _completed_index_result(result, target_graph_name) and files is not None and files > 0


def _promotion_file_count(result: dict) -> int | None:
    stats = result.get("stats") if isinstance(result.get("stats"), dict) else {}
    value = result.get("files", result.get("files_indexed", stats.get("files", stats.get("files_indexed"))))
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = int(value)
        except ValueError:
            return None
    return value if isinstance(value, int) and value >= 0 else None


def _graph_generation(graph_name: str) -> str:
    authorized_graph_name(graph_name)
    if mcp_server._registry is None:
        raise RuntimeError("MCP server not initialized")
    value = mcp_server._registry.get(graph_name).cache_generation()
    if not isinstance(value, str) or not value:
        raise HTTPException(status_code=503, detail="Graph publication generation cannot be verified")
    return value


def _completed_index_result(result: dict, target_graph_name: str) -> bool:
    if (
        result.get("status") != "done"
        or result.get("timeout")
        or result.get("error")
        or result.get("ready") is False
    ):
        return False
    if result.get("project_name") != target_graph_name:
        return False
    errors = result.get("errors", result.get("stats", {}).get("errors") if isinstance(result.get("stats"), dict) else None)
    if isinstance(errors, str):
        try:
            errors = ast.literal_eval(errors)
        except (SyntaxError, ValueError):
            return False
    return errors == [] or (type(errors) is int and errors == 0)


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
        "registered_project_scope": state.get("registered_project_scope"),
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
        "SELECT id, project_name, project_id, repo_path FROM projects WHERE project_id = ? AND is_active = 1",
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
        "registered_project_scope": registered_project_scope(dict(row)),
    }


async def _dispatch_with_project_context(
    tool: str,
    arguments: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    scope = context.get("registered_project_scope")
    if not isinstance(scope, ProjectScope):
        raise HTTPException(status_code=403, detail="Registered project scope is required")
    with bind_project_scope(scope):
        result = await dispatch_tool(tool, arguments, context["project_name"])
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
    requested_graph_name = graph_name
    fallback_ref = _normalize_ref_id(args.get("fallback_ref"))
    fallback_graph_used = False
    if tool == "index_full":
        backend_tool = "index_full"
        repo_path = args.get("repo_path") or args.get("project_root") or args.get("root")
        if not repo_path:
            raise HTTPException(status_code=400, detail="repo_path is required")
        authorized_repo_root(str(repo_path))
        _require_ref_migration(project_name, ref_id)
        with bind_project_ref(ref_id):
            result = await mcp_server.index_full(repo_path=str(repo_path), project_name=graph_name)
    elif tool == "index_git_incremental":
        backend_tool = "index_repo_changes"
        repo_path = args.get("repo_path") or args.get("project_root") or args.get("root")
        if not repo_path:
            raise HTTPException(status_code=400, detail="repo_path is required")
        authorized_repo_root(str(repo_path))
        _require_ref_migration(project_name, ref_id)
        with bind_project_ref(ref_id):
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
        root = authorized_repo_root(str(repo_path))
        safe_paths = mcp_server._validated_changed_paths(str(repo_path), root, changed_paths)
        _require_ref_migration(project_name, ref_id)
        with bind_project_ref(ref_id):
            result = await mcp_server.index_incremental(
                repo_path=str(repo_path),
                changed_paths=safe_paths,
                project_name=graph_name,
            )
    elif tool == "index_progress":
        backend_tool = "get_index_job_status"
        job_id = args.get("job_id")
        if not job_id:
            raise HTTPException(status_code=400, detail="job_id is required")
        with bind_project_ref(ref_id):
            result = await mcp_server.get_index_job_status(job_id=str(job_id))
    elif tool in {"query_impact_graph", "get_optimized_context"}:
        backend_tool = "strategy_query"
        query = args.get("query") or args.get("question")
        if not query:
            raise HTTPException(status_code=400, detail="query is required")
        raw_token_budget = args.get("token_budget")
        authorized_repo_root()
        requested_graph_name, graph_name, fallback_graph_used = _query_graph_scope(
            project_name, ref_id, fallback_ref
        )
        with bind_project_ref(fallback_ref if fallback_graph_used else ref_id):
            result = await _maybe_await(_call_in_graph(
                graph_name,
                mcp_server.strategy_query,
                query=str(query),
                graph_top_k=int(args.get("graph_top_k", 8)),
                min_graph_hits=int(args.get("min_graph_hits", 3)),
                token_budget=int(raw_token_budget) if raw_token_budget is not None else None,
                relation_depth=int(args.get("relation_depth", 1)),
                fallback_max_files=int(args.get("fallback_max_files", 3)),
            ))
    elif tool == "fetch_minimal_code":
        backend_tool = "retrieve_context"
        query = args.get("query") or args.get("symbol")
        if not query:
            raise HTTPException(status_code=400, detail="query is required")
        authorized_repo_root()
        requested_graph_name, graph_name, fallback_graph_used = _query_graph_scope(
            project_name, ref_id, fallback_ref
        )
        with bind_project_ref(fallback_ref if fallback_graph_used else ref_id):
            result = await _maybe_await(_call_in_graph(
                graph_name,
                mcp_server.retrieve_context,
                query=str(query),
                limit=int(args.get("limit", 10)),
                task_id=str(args.get("task_id")) if args.get("task_id") else None,
                issue_id=str(args.get("issue_id")) if args.get("issue_id") else None,
                pr_id=str(args.get("pr_id")) if args.get("pr_id") else None,
                activity_id=str(args.get("activity_id")) if args.get("activity_id") else None,
            ))
    elif tool == "promote_ref":
        backend_tool = "index_full"
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
                "requested_graph_name": requested_graph_name,
                "graph_name": graph_name,
                "parent_graph_name": _graph_name_for_project(project_name, parent_ref),
                "fallback_ref": fallback_ref,
                "fallback_graph_used": fallback_graph_used,
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
    result = await _dispatch_with_project_context(payload.tool, payload.arguments, context)
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


@router.get("/output-rules")
async def get_project_output_rules_for_relay(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
) -> dict[str, Any]:
    """Return the server-managed rules that the local relay should materialize."""
    context = _project_context(request)
    rules = await _effective_output_rules(db, int(context["project_db_id"]))
    return rules.model_dump()


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


@account_router.get("/output-rules")
async def get_account_output_rules_for_relay(
    project_id: str,
    _: None = Depends(require_crystal_suite),
    user: dict = Depends(get_current_user),
    db: aiosqlite.Connection = Depends(get_db),
) -> dict[str, Any]:
    context = await _account_project_context(db, project_id, user)
    rules = await _effective_output_rules(db, int(context["project_db_id"]))
    return rules.model_dump()


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
