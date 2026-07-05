"""CGA (Context Graph Agent) MCP Server – exposes indexing and retrieval tools.

Phase 1 tools
-------------
index_full / index_incremental / index_repo_changes  - enqueue jobs via MQ
find_symbol / find_callers / find_callees / retrieve_context  - graph reads

Phase 2 additions
-----------------
find_call_graph(qualified_name) - direct call graph traversal
get_stats()                     - symbol / file / edge counts

Phase 3 additions
-----------------
run_eval()      - P@5 + latency benchmark report
clear_cache()   - invalidate Redis query cache

Read tools are cache-aware (check → serve or populate).
All read tool calls are trace-recorded for hallucination-proxy scoring.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import structlog
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from backend import runtime_config
from backend.auth.context import _current_project_external_id
from backend.agent.query_strategy import run_cg_first_strategy
from backend.graph.registry import GraphRegistry
from backend.graph import schema as S
from backend.indexer.pipeline import _normalize_repo_path, _resolve_repo_root
from backend.perf.context_quality import benchmark_context_quality as run_context_quality_benchmark
from backend.perf.token_efficiency import benchmark_token_efficiency as run_token_efficiency_benchmark
from backend.tools.producer import MCPProducer
from backend.queue.streams import JobConsumer
from backend.workbriefing.service import WorkActivityValidationError, WorkBriefingService

log = structlog.get_logger()

def _csv_env(name: str) -> list[str]:
    return [part.strip() for part in os.getenv(name, "").split(",") if part.strip()]


mcp = FastMCP(
    "cga-mcp-server",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*"] + _csv_env("CGA_MCP_ALLOWED_HOSTS"),
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*"] + _csv_env("CGA_MCP_ALLOWED_ORIGINS"),
    ),
)

_registry: GraphRegistry | None = None
_producer: MCPProducer | None = None
_consumer: JobConsumer | None = None
_cache = None           # QueryCache | None  – set via init()
_recorder = None        # TraceRecorder | None  – set via init()
_work_briefing_service: WorkBriefingService | None = None
_repo_root = Path(os.getenv("CONTEXTGRAPH_REPO_ROOT", ".")).resolve()


class _GraphProxy:
    """Module-level proxy that routes graph calls to the current project's GraphClient.

    All existing ``_graph.query(...)`` calls and ``if not _graph:`` guards work
    unchanged – the proxy dispatches to the correct per-project graph at call time
    using the ``_current_project_name`` ContextVar set by ProjectTokenMiddleware.
    """

    def query(self, cypher: str, params: dict | None = None):
        if _registry is None:
            raise RuntimeError("MCP server not initialized")
        return _registry.current().query(cypher, params)

    def __bool__(self) -> bool:  # enables ``if not _graph:`` checks
        return _registry is not None


_graph = _GraphProxy()
def _resolve_project_name(project_name: str | None = None) -> str:
    from backend.graph.registry import _current_project_name

    if project_name:
        return project_name.strip().lower()
    return _current_project_name.get()


async def _collect_git_changed_paths(repo_path: str, include_untracked: bool = True) -> dict[str, list[str]]:
    """Discover changed paths from the local git worktree.

    Returns:
        {
            "changed_paths": [...],       # files safe for incremental indexing
            "destructive_paths": [...],   # deleted / renamed-away paths requiring full reindex
        }

    We classify deletes and renames as destructive because the current incremental
    pipeline does not remove stale symbols for files that disappeared from disk.
    """
    resolved_repo_path = _normalize_repo_path(repo_path)
    untracked_flag = "--untracked-files=all" if include_untracked else "--untracked-files=no"
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", resolved_repo_path, "status", "--porcelain=v1", untracked_flag,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    if proc.returncode != 0:
        msg = (stderr_bytes or stdout_bytes or b"git status failed").decode().strip()
        raise RuntimeError(msg)

    changed_paths: list[str] = []
    destructive_paths: list[str] = []
    seen_changed: set[str] = set()
    seen_destructive: set[str] = set()

    for raw_line in stdout_bytes.decode().splitlines():
        line = raw_line.rstrip()
        if len(line) < 3:
            continue
        status = line[:2]
        payload = line[3:].strip()
        staged = status[0]
        unstaged = status[1]

        if "->" in payload:
            old_path, new_path = [part.strip() for part in payload.split("->", 1)]
            if old_path not in seen_destructive:
                destructive_paths.append(old_path)
                seen_destructive.add(old_path)
            if new_path not in seen_changed:
                changed_paths.append(new_path)
                seen_changed.add(new_path)
            continue

        path = payload
        destructive = any(flag == "D" for flag in (staged, unstaged))
        if destructive:
            if path not in seen_destructive:
                destructive_paths.append(path)
                seen_destructive.add(path)
            continue

        if path not in seen_changed:
            changed_paths.append(path)
            seen_changed.add(path)

    return {
        "changed_paths": changed_paths,
        "destructive_paths": destructive_paths,
    }


def init(
    registry: GraphRegistry,
    producer: MCPProducer,
    cache=None,
    recorder=None,
    consumer=None,
    work_briefing_service: WorkBriefingService | None = None,
) -> None:
    """Wire dependencies after app startup."""
    global _registry, _producer, _cache, _recorder, _consumer, _work_briefing_service
    _registry = registry
    _producer = producer
    _cache = cache
    _recorder = recorder
    _consumer = consumer
    _work_briefing_service = work_briefing_service


def set_consumer(consumer) -> None:
    """Set the consumer after initialization (useful when consumer is created after init)."""
    global _consumer
    _consumer = consumer


def _require_work_briefing_service() -> WorkBriefingService:
    if _work_briefing_service is None:
        raise RuntimeError("Work briefing service not initialized")
    return _work_briefing_service


def _resolve_project_external_id(project_id: str | None = None) -> str:
    bound_project_id = _current_project_external_id.get().strip()
    if project_id is not None:
        cleaned = project_id.strip()
        if not cleaned:
            raise WorkActivityValidationError("project_id is required")
        if bound_project_id and cleaned != bound_project_id:
            raise WorkActivityValidationError("project_id must match the authenticated MCP project")
        return cleaned
    if bound_project_id:
        return bound_project_id
    raise WorkActivityValidationError("project_id is required")


# ─────────────────────────────────────────────────────────────────────────────
# Queue status enrichment helpers (for MCP tool responses)
# ─────────────────────────────────────────────────────────────────────────────

async def _enrich_job_response(job_id: str, base_response: dict) -> dict:
    """Enrich job response with queue_position and eta_seconds.
    
    Queries the current queue snapshot to compute:
    - queue_position: 0-based position in pending queue (None if done/failed/processing)
    - eta_seconds: estimated seconds until completion (None if not queued)
    - created_at: ISO timestamp when job was created
    - updated_at: ISO timestamp when job status last changed
    """
    if not _consumer:
        return base_response  # No consumer available; return base response
    
    try:
        # Get current queue snapshot for avg duration and pending jobs list
        snapshot = await _consumer.get_queue_snapshot()
        pending_jobs = snapshot.get("pending_jobs", [])
        avg_duration_sec = snapshot.get("avg_duration_sec", 30)
        
        # Find queue position for this job_id
        queue_position = None
        eta_seconds = None
        created_at = None
        updated_at = None
        
        for idx, job in enumerate(pending_jobs):
            if job.get("job_id") == job_id:
                queue_position = idx
                # ETA = (queue_position + 1) * avg_duration + current_processing_time
                # For simplicity, estimate as (queue_position + 1) * avg_duration
                if queue_position is not None:
                    eta_seconds = (queue_position + 1) * avg_duration_sec
                created_at = job.get("created_at")
                updated_at = job.get("updated_at")
                break
        
        # Enrich base response
        enriched = {**base_response}
        if queue_position is not None:
            enriched["queue_position"] = queue_position
        if eta_seconds is not None:
            enriched["eta_seconds"] = eta_seconds
        if created_at:
            enriched["created_at"] = created_at
        if updated_at:
            enriched["updated_at"] = updated_at
        
        return enriched
    except Exception as e:
        log.warning("enrich_job_response.failed", error=str(e))
        return base_response  # Return base response on any error


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _cached_read(tool: str, args: dict, fetch_fn, trace_args: dict | None = None):
    """Check cache → call fetch_fn → store → record trace → return."""
    if _cache:
        hit = _cache.get(tool, args)
        if hit is not None:
            return hit
    t0 = time.perf_counter()
    result = fetch_fn()
    latency_ms = (time.perf_counter() - t0) * 1000.0
    if _cache:
        _cache.set(tool, args, result)
    if _recorder:
        _recorder.record(tool, trace_args or args, result, latency_ms)
    return result


def _with_correlation_args(
    args: dict,
    *,
    task_id: str | None = None,
    issue_id: str | None = None,
    pr_id: str | None = None,
    activity_id: str | None = None,
) -> dict:
    correlated = dict(args)
    for key, value in {
        "task_id": task_id,
        "issue_id": issue_id,
        "pr_id": pr_id,
        "activity_id": activity_id,
    }.items():
        if isinstance(value, str) and value.strip():
            correlated[key] = value.strip()
    return correlated


def _read_symbol_snippet(file_path: str, line_start: int, line_end: int, context_lines: int = 2, max_chars: int = 900) -> str:
    path = Path(file_path)
    if not path.is_absolute():
        path = (_repo_root / file_path).resolve()
    if not path.exists() or not path.is_file():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    if not lines:
        return ""

    start_idx = max(0, int(line_start) - 1 - context_lines)
    end_idx = min(len(lines), int(line_end) + context_lines)
    snippet = "\n".join(lines[start_idx:end_idx]).strip()
    if len(snippet) > max_chars:
        return snippet[: max_chars - 3] + "..."
    return snippet


def _summarize_symbol(symbol_type: str, qualified_name: str, file_path: str, line_start: int, line_end: int) -> str:
    location = f"{file_path}:{line_start}-{line_end}"
    return f"{symbol_type} {qualified_name} at {location}"


def _short_variable_name(qualified_name: str) -> str:
    return qualified_name.split(":")[-1] if ":" in qualified_name else qualified_name


def _fetch_relation_summary(qualified_name: str, limit: int = 3) -> dict:
    if not _graph:
        return {"callers": [], "callees": [], "callers_count": 0, "callees_count": 0}

    callers_result = _graph.query(
        S.QUERY_FIND_CALLERS,
        {"qualified_name": qualified_name, "limit": limit},
    )
    callees_result = _graph.query(
        S.QUERY_FIND_CALLEES,
        {"qualified_name": qualified_name, "limit": limit},
    )
    callers = [row[0] for row in callers_result.result_set]
    callees = [row[0] for row in callees_result.result_set]
    return {
        "callers": callers,
        "callees": callees,
        "callers_count": len(callers),
        "callees_count": len(callees),
    }


# ---------------------------------------------------------------------------
# Write tools (async – go through MQ)
# ---------------------------------------------------------------------------

@mcp.tool()
async def index_full(repo_path: str, project_name: str | None = None) -> dict:
    """Enqueue a full index job for the given repository path."""
    if not _producer:
        raise RuntimeError("MCP server not initialized")
    try:
        _resolve_repo_root(repo_path)
    except FileNotFoundError:
        log.warning(
            "index_full.repo_unavailable",
            repo_path=repo_path,
            fallback_mode="cga_relay_or_mount_required",
        )
        return {
            "status": "relay_required",
            "mode": "relay",
            "reason": "repo_path_unavailable",
            "changed_count": 0,
            "destructive_count": 0,
            "repo_path": repo_path,
            "message": "The CGA indexer cannot read this repository path. Mount the checkout into the API/indexer runtime or run a local cga-relay configured for this project.",
        }
    resolved_project_name = _resolve_project_name(project_name)
    submitted = await _producer.submit_full_index(repo_path, project_name=resolved_project_name)
    # Invalidate cache so stale reads are avoided after re-index
    if _cache:
        _cache.invalidate_all()
    base_response = {
        "status": "queued",
        "stream_id": submitted["stream_id"],
        "job_id": submitted["job_id"],
        "repo_path": repo_path,
    }
    # Enrich with queue position and ETA
    return await _enrich_job_response(submitted["job_id"], base_response)


@mcp.tool()
async def index_incremental(repo_path: str, changed_paths: list[str], project_name: str | None = None) -> dict:
    """Enqueue an incremental index job for a list of changed files."""
    if not _producer:
        raise RuntimeError("MCP server not initialized")
    resolved_project_name = _resolve_project_name(project_name)
    submitted = await _producer.submit_incremental_index(
        repo_path, changed_paths, project_name=resolved_project_name
    )
    if _cache:
        _cache.invalidate_all()
    base_response = {
        "status": "queued",
        "stream_id": submitted["stream_id"],
        "job_id": submitted["job_id"],
        "changed_count": len(changed_paths),
    }
    # Enrich with queue position and ETA
    return await _enrich_job_response(submitted["job_id"], base_response)


@mcp.tool()
async def index_repo_changes(
    repo_path: str,
    include_untracked: bool = True,
    auto_full_on_destructive: bool = False,
    project_name: str | None = None,
) -> dict:
    """Index current git worktree changes for a repository.

    This is a convenience wrapper for the common agent workflow of:
    1) discover changed files from git
     2) queue incremental indexing for current file additions/modifications
     3) include deleted/renamed-away paths in the incremental job so the native
         pipeline can remove stale file-local graph data.

     `auto_full_on_destructive=True` remains available as a conservative fallback.
    """
    if not _producer:
        raise RuntimeError("MCP server not initialized")
    resolved_project_name = _resolve_project_name(project_name)

    try:
        discovered = await _collect_git_changed_paths(repo_path, include_untracked=include_untracked)
    except FileNotFoundError:
        log.warning(
            "index_repo_changes.git_unavailable",
            repo_path=repo_path,
            fallback_mode="cga_relay_required",
        )
        return {
            "status": "relay_required",
            "mode": "relay",
            "reason": "git_unavailable",
            "changed_count": 0,
            "destructive_count": 0,
            "repo_path": repo_path,
            "message": "Use cga-relay index_git_incremental so git changes are collected on the developer machine.",
        }
    except RuntimeError as exc:
        log.warning(
            "index_repo_changes.git_status_failed",
            repo_path=repo_path,
            error=str(exc),
            fallback_mode="cga_relay_required",
        )
        return {
            "status": "relay_required",
            "mode": "relay",
            "reason": "git_status_failed",
            "changed_count": 0,
            "destructive_count": 0,
            "repo_path": repo_path,
            "message": "Use cga-relay index_git_incremental so git changes are collected on the developer machine.",
        }

    changed_paths = discovered["changed_paths"]
    destructive_paths = discovered["destructive_paths"]
    incremental_paths: list[str] = []
    seen_paths: set[str] = set()
    for path in destructive_paths + changed_paths:
        if path not in seen_paths:
            incremental_paths.append(path)
            seen_paths.add(path)
    if not incremental_paths:
        return {
            "status": "noop",
            "mode": "none",
            "changed_count": 0,
            "destructive_count": 0,
            "repo_path": repo_path,
        }

    if destructive_paths and auto_full_on_destructive:
        submitted = await _producer.submit_full_index(repo_path, project_name=resolved_project_name)
        if _cache:
            _cache.invalidate_all()
        base_response = {
            "status": "queued",
            "mode": "full",
            "reason": "destructive_git_change",
            "stream_id": submitted["stream_id"],
            "job_id": submitted["job_id"],
            "changed_count": len(changed_paths),
            "destructive_count": len(destructive_paths),
            "repo_path": repo_path,
        }
        # Enrich with queue position and ETA
        return await _enrich_job_response(submitted["job_id"], base_response)

    submitted = await _producer.submit_incremental_index(
        repo_path,
        incremental_paths,
        project_name=resolved_project_name,
    )
    if _cache:
        _cache.invalidate_all()
    base_response = {
        "status": "queued",
        "mode": "incremental",
        "stream_id": submitted["stream_id"],
        "job_id": submitted["job_id"],
        "changed_count": len(incremental_paths),
        "destructive_count": len(destructive_paths),
        "repo_path": repo_path,
    }
    # Enrich with queue position and ETA
    return await _enrich_job_response(submitted["job_id"], base_response)


@mcp.tool()
async def get_index_job_status(job_id: str) -> dict:
    """Get status for an indexing job id, including queue position and ETA if queued."""
    if not _producer:
        raise RuntimeError("MCP server not initialized")
    status = await _producer.get_job_status(job_id)
    if status is None:
        return {"job_id": job_id, "status": "not_found"}
    # Enrich with queue position and ETA
    return await _enrich_job_response(job_id, status)


@mcp.tool()
async def wait_for_index_ready(
    job_id: str,
    timeout_sec: float = 120.0,
    poll_interval_sec: float = 1.0,
) -> dict:
    """Wait until an indexing job reaches terminal state (done or failed)."""
    if not _producer:
        raise RuntimeError("MCP server not initialized")
    return await _producer.wait_for_job_status(
        job_id,
        timeout_sec=timeout_sec,
        poll_interval_sec=poll_interval_sec,
    )


# ---------------------------------------------------------------------------
# Read tools (sync – direct graph query, cache-aware)
# ---------------------------------------------------------------------------

@mcp.tool()
def find_symbol(
    name: str,
    limit: int = 20,
    task_id: str | None = None,
    issue_id: str | None = None,
    pr_id: str | None = None,
    activity_id: str | None = None,
) -> list[dict]:
    """Find symbols by name or qualified name fragment."""
    if not _graph:
        raise RuntimeError("MCP server not initialized")

    def _fetch():
        result = _graph.query(S.QUERY_FIND_SYMBOL, {"name": name, "limit": limit})
        return [
            {
                "qualified_name": row[0],
                "symbol_type": row[1],
                "file_path": row[2],
                "line_start": row[3],
                "line_end": row[4],
            }
            for row in result.result_set
        ]

    args = {"name": name, "limit": limit}
    return _cached_read(
        "find_symbol",
        args,
        _fetch,
        trace_args=_with_correlation_args(
            args,
            task_id=task_id,
            issue_id=issue_id,
            pr_id=pr_id,
            activity_id=activity_id,
        ),
    )


@mcp.tool()
def find_callers(qualified_name: str, limit: int = 20) -> list[dict]:
    """Return all symbols that call the given qualified name."""
    if not _graph:
        raise RuntimeError("MCP server not initialized")

    def _fetch():
        result = _graph.query(
            S.QUERY_FIND_CALLERS, {"qualified_name": qualified_name, "limit": limit}
        )
        return [
            {"caller": row[0], "file_path": row[1], "line_start": row[2]}
            for row in result.result_set
        ]

    return _cached_read(
        "find_callers", {"qualified_name": qualified_name, "limit": limit}, _fetch
    )


@mcp.tool()
def find_callees(qualified_name: str, limit: int = 20) -> list[dict]:
    """Return all symbols called by the given qualified name."""
    if not _graph:
        raise RuntimeError("MCP server not initialized")

    def _fetch():
        result = _graph.query(
            S.QUERY_FIND_CALLEES, {"qualified_name": qualified_name, "limit": limit}
        )
        return [
            {"callee": row[0], "file_path": row[1], "line_start": row[2]}
            for row in result.result_set
        ]

    return _cached_read(
        "find_callees", {"qualified_name": qualified_name, "limit": limit}, _fetch
    )


@mcp.tool()
def find_variable(name: str, limit: int = 20) -> list[dict]:
    """Find variables by simple name or qualified name fragment."""
    if not _graph:
        raise RuntimeError("MCP server not initialized")

    def _fetch():
        result = _graph.query(S.QUERY_FIND_VARIABLE, {"name": name, "limit": limit})
        return [
            {
                "qualified_name": row[0],
                "scope_qname": row[1],
                "file_path": row[2],
                "line_number": row[3],
                "role": row[4],
            }
            for row in result.result_set
        ]

    return _cached_read("find_variable", {"name": name, "limit": limit}, _fetch)


@mcp.tool()
def get_variable_flows(scope_qname: str, limit: int = 50) -> list[dict]:
    """Get variable-to-variable flows inside a symbol scope."""
    if not _graph:
        raise RuntimeError("MCP server not initialized")

    def _fetch():
        result = _graph.query(S.QUERY_VARIABLE_FLOWS_FOR_SCOPE, {"scope_qname": scope_qname, "limit": limit})
        return [
            {
                "source": row[0],
                "target": row[1],
                "flow_type": row[2],
                "line_number": row[3],
            }
            for row in result.result_set
        ]

    return _cached_read("get_variable_flows", {"scope_qname": scope_qname, "limit": limit}, _fetch)


@mcp.tool()
def trace_variable_lineage(qualified_name: str) -> dict:
    """Get one-hop upstream and downstream lineage for a variable."""
    if not _graph:
        raise RuntimeError("MCP server not initialized")

    def _fetch():
        result = _graph.query(S.QUERY_VARIABLE_LINEAGE, {"qualified_name": qualified_name})
        if not result.result_set:
            return {"qualified_name": qualified_name, "upstream": [], "downstream": []}
        row = result.result_set[0]
        upstream = [item for item in (row[0] or []) if item]
        downstream = [item for item in (row[1] or []) if item]
        return {
            "qualified_name": qualified_name,
            "upstream": upstream,
            "downstream": downstream,
        }

    return _cached_read("trace_variable_lineage", {"qualified_name": qualified_name}, _fetch)


@mcp.tool()
def analyze_return_influence(scope_qname: str, limit: int = 20) -> dict:
    """Analyze which parameters influence a function or method return value."""
    if not _graph:
        raise RuntimeError("MCP server not initialized")

    def _fetch():
        result = _graph.query(S.QUERY_RETURN_INFLUENCE, {"scope_qname": scope_qname, "limit": limit})
        influences: list[dict] = []
        seen_parameters: set[str] = set()
        for row in result.result_set:
            parameter = row[0]
            flow_path = [item for item in (row[1] or []) if item]
            influences.append(
                {
                    "parameter": parameter,
                    "path": flow_path,
                    "path_length": max(0, len(flow_path) - 1),
                }
            )
            seen_parameters.add(parameter)
        return {
            "scope_qname": scope_qname,
            "influenced_by_parameters": sorted(seen_parameters),
            "paths": influences,
        }

    return _cached_read("analyze_return_influence", {"scope_qname": scope_qname, "limit": limit}, _fetch)


@mcp.tool()
def analyze_scope_variables(scope_qname: str, limit: int = 20) -> dict:
    """Analyze unused parameters and key intermediary variables inside a symbol scope."""
    if not _graph:
        raise RuntimeError("MCP server not initialized")

    def _fetch():
        result = _graph.query(S.QUERY_SCOPE_VARIABLE_METRICS, {"scope_qname": scope_qname})
        unused_parameters: list[str] = []
        key_intermediates: list[dict] = []
        all_variables: list[dict] = []
        for row in result.result_set:
            variable = {
                "qualified_name": row[0],
                "name": row[1],
                "role": row[2],
                "incoming_count": row[3],
                "outgoing_count": row[4],
            }
            all_variables.append(variable)
            if variable["role"] == "parameter" and variable["outgoing_count"] == 0:
                unused_parameters.append(variable["qualified_name"])
            if variable["role"] == "local" and variable["incoming_count"] > 0 and variable["outgoing_count"] > 0:
                key_intermediates.append(
                    {
                        **variable,
                        "importance_score": variable["incoming_count"] + variable["outgoing_count"],
                    }
                )
        key_intermediates.sort(key=lambda item: (-item["importance_score"], item["qualified_name"]))
        return {
            "scope_qname": scope_qname,
            "unused_parameters": unused_parameters[:limit],
            "key_intermediates": key_intermediates[:limit],
            "variables": all_variables[: max(limit, 20)],
        }

    return _cached_read("analyze_scope_variables", {"scope_qname": scope_qname, "limit": limit}, _fetch)


@mcp.tool()
def explain_data_flow(scope_qname: str, limit: int = 20) -> dict:
    """Explain how data moves through a function or method for agent consumption."""
    if not _graph:
        raise RuntimeError("MCP server not initialized")

    def _fetch():
        flow_result = _graph.query(S.QUERY_VARIABLE_FLOWS_FOR_SCOPE, {"scope_qname": scope_qname, "limit": limit})
        metrics_result = _graph.query(S.QUERY_SCOPE_VARIABLE_METRICS, {"scope_qname": scope_qname})
        return_result = _graph.query(S.QUERY_RETURN_INFLUENCE, {"scope_qname": scope_qname, "limit": limit})

        flows = [
            {
                "source": row[0],
                "target": row[1],
                "flow_type": row[2],
                "line_number": row[3],
            }
            for row in flow_result.result_set
        ]
        variables = [
            {
                "qualified_name": row[0],
                "name": row[1],
                "role": row[2],
                "incoming_count": row[3],
                "outgoing_count": row[4],
            }
            for row in metrics_result.result_set
        ]
        return_paths = [
            {
                "parameter": row[0],
                "path": [item for item in (row[1] or []) if item],
            }
            for row in return_result.result_set
        ]

        unused_parameters = [item["qualified_name"] for item in variables if item["role"] == "parameter" and item["outgoing_count"] == 0]
        key_intermediates = [
            item["qualified_name"]
            for item in sorted(
                (var for var in variables if var["role"] == "local" and var["incoming_count"] > 0 and var["outgoing_count"] > 0),
                key=lambda value: (-(value["incoming_count"] + value["outgoing_count"]), value["qualified_name"]),
            )[:limit]
        ]
        summary: list[str] = []
        scope_name = scope_qname.split(".")[-1]
        parameter_names = sorted({_short_variable_name(item["qualified_name"]) for item in variables if item["role"] == "parameter"})
        influenced_parameters = sorted({_short_variable_name(item["parameter"]) for item in return_paths})
        unused_parameter_names = [_short_variable_name(item) for item in unused_parameters[:limit]]
        key_intermediate_names = [_short_variable_name(item) for item in key_intermediates[:limit]]

        if parameter_names:
            summary.append(f"{scope_name} inputs include: {', '.join(parameter_names)}.")
        if return_paths:
            params = ", ".join(influenced_parameters)
            summary.append(f"Return value is influenced by these inputs: {params}.")
        else:
            summary.append("No clear path from inputs to the return value was found yet.")
        if unused_parameter_names:
            summary.append("Unused parameters: " + ", ".join(unused_parameter_names) + ".")
        if key_intermediate_names:
            summary.append("Key intermediary variables: " + ", ".join(key_intermediate_names) + ".")
        if flows:
            preview = "; ".join(
                f"{_short_variable_name(item['source'])} -> {_short_variable_name(item['target'])} ({item['flow_type']})"
                for item in flows[: min(5, len(flows))]
            )
            summary.append("Key flow preview: " + preview + ".")

        narrative = " ".join(summary)

        return {
            "scope_qname": scope_qname,
            "summary": summary,
            "narrative": narrative,
            "flows": flows,
            "return_influence": return_paths,
            "unused_parameters": unused_parameters,
            "key_intermediates": key_intermediates,
        }

    return _cached_read("explain_data_flow", {"scope_qname": scope_qname, "limit": limit}, _fetch)


@mcp.tool()
def retrieve_context(
    query: str,
    limit: int = 10,
    task_id: str | None = None,
    issue_id: str | None = None,
    pr_id: str | None = None,
    activity_id: str | None = None,
) -> list[dict]:
    """Retrieve relevant code context for an agent query string."""
    if not _graph:
        raise RuntimeError("MCP server not initialized")

    def _fetch():
        result = _graph.query(S.QUERY_RETRIEVE_CONTEXT, {"query": query, "limit": limit})
        items: list[dict] = []
        for row in result.result_set:
            qualified_name = row[0]
            symbol_type = row[1]
            file_path = row[2]
            line_start = row[3]
            line_end = row[4]
            snippet = _read_symbol_snippet(file_path, line_start, line_end)
            relations = _fetch_relation_summary(qualified_name)
            items.append(
                {
                    "qualified_name": qualified_name,
                    "symbol_type": symbol_type,
                    "file_path": file_path,
                    "line_start": line_start,
                    "line_end": line_end,
                    "summary": _summarize_symbol(symbol_type, qualified_name, file_path, line_start, line_end),
                    "snippet": snippet,
                    **relations,
                }
            )
        return items

    args = {"query": query, "limit": limit}
    return _cached_read(
        "retrieve_context",
        args,
        _fetch,
        trace_args=_with_correlation_args(
            args,
            task_id=task_id,
            issue_id=issue_id,
            pr_id=pr_id,
            activity_id=activity_id,
        ),
    )


# ---------------------------------------------------------------------------
# Phase 2: call graph traversal + stats
# ---------------------------------------------------------------------------

@mcp.tool()
def find_call_graph(
    qualified_name: str,
    depth: int = 2,
    task_id: str | None = None,
    issue_id: str | None = None,
    pr_id: str | None = None,
    activity_id: str | None = None,
) -> dict:
    """Return the full call graph (callers + callees) up to *depth* hops."""
    if not _graph:
        raise RuntimeError("MCP server not initialized")

    def _fetch():
        callers_result = _graph.query(
            S.QUERY_FIND_CALLERS, {"qualified_name": qualified_name, "limit": 50}
        )
        callees_result = _graph.query(
            S.QUERY_FIND_CALLEES, {"qualified_name": qualified_name, "limit": 50}
        )
        return {
            "symbol": qualified_name,
            "callers": [row[0] for row in callers_result.result_set],
            "callees": [row[0] for row in callees_result.result_set],
        }

    args = {"qualified_name": qualified_name, "depth": depth}
    return _cached_read(
        "find_call_graph",
        args,
        _fetch,
        trace_args=_with_correlation_args(
            args,
            task_id=task_id,
            issue_id=issue_id,
            pr_id=pr_id,
            activity_id=activity_id,
        ),
    )


@mcp.tool()
def get_stats() -> dict:
    """Return graph statistics for files, symbols, variables, and relationship counts."""
    if not _graph:
        raise RuntimeError("MCP server not initialized")

    def _fetch():
        sym = _graph.query("MATCH (s:Symbol) RETURN count(s)").result_set
        fil = _graph.query("MATCH (f:File) RETURN count(f)").result_set
        var = _graph.query("MATCH (v:Variable) RETURN count(v)").result_set
        calls = _graph.query("MATCH ()-[c:CALLS]->() RETURN count(c)").result_set
        flows = _graph.query("MATCH ()-[f:FLOWS_TO]->() RETURN count(f)").result_set
        return {
            "symbols": sym[0][0] if sym else 0,
            "files": fil[0][0] if fil else 0,
            "variables": var[0][0] if var else 0,
            "call_edges": calls[0][0] if calls else 0,
            "variable_flow_edges": flows[0][0] if flows else 0,
        }

    return _cached_read("get_stats", {}, _fetch)


# ---------------------------------------------------------------------------
# Phase 3: eval + cache management
# ---------------------------------------------------------------------------

@mcp.tool()
def run_eval() -> dict:
    """Run the P@5 + latency benchmark and return the report."""
    if not _graph:
        raise RuntimeError("MCP server not initialized")
    from backend.eval.runner import EvalRunner
    runner = EvalRunner(_registry.current())
    return runner.run().as_dict()


@mcp.tool()
def clear_cache() -> dict:
    """Invalidate all cached query results."""
    if not _cache:
        return {"status": "no_cache_configured", "deleted": 0}
    deleted = _cache.invalidate_all()
    log.info("cache.cleared", keys_deleted=deleted)
    return {"status": "ok", "deleted": deleted}


@mcp.tool()
def benchmark_token_efficiency(
    query: str = "",
    baseline_text: str = "",
    cg_text: str = "",
    baseline_snippets: list[str] | None = None,
    cg_snippets: list[str] | None = None,
    baseline_file_paths: list[str] | None = None,
    cg_file_paths: list[str] | None = None,
    notes: str = "",
) -> dict:
    """Estimate token savings from CG/MCP reduced context versus baseline context.

    This tool is repository-agnostic and intended for cross-project efficiency benchmarking.
    """
    payload = {
        "query": query,
        "notes": notes,
        "baseline": {},
        "cg": {},
    }

    if baseline_text:
        payload["baseline"]["text"] = baseline_text
    if baseline_snippets:
        payload["baseline"]["snippets"] = baseline_snippets
    if baseline_file_paths:
        payload["baseline"]["filePaths"] = baseline_file_paths

    if cg_text:
        payload["cg"]["text"] = cg_text
    if cg_snippets:
        payload["cg"]["snippets"] = cg_snippets
    if cg_file_paths:
        payload["cg"]["filePaths"] = cg_file_paths

    return run_token_efficiency_benchmark(payload=payload, repo_root=_repo_root)


@mcp.tool()
def benchmark_context_quality(
    cases: list[dict] | None = None,
    weights: dict | None = None,
) -> dict:
    """Compute Hallucination Pressure Score (HPS) for baseline-vs-CG contexts.

    Cases should provide goldItems plus baseline/cg chunks with evidence IDs.
    This is deterministic and scores context risk before an LLM answer is generated.
    """
    payload = {"cases": cases or []}
    if weights:
        payload["weights"] = weights
    return run_context_quality_benchmark(payload=payload, repo_root=_repo_root)


# ---------------------------------------------------------------------------
# Aggregation & architecture analysis
# ---------------------------------------------------------------------------

@mcp.tool()
def get_architecture_overview() -> dict:
    """Get high-level architecture stats: file count, symbols, languages, interconnectedness."""
    if not _graph:
        raise RuntimeError("MCP server not initialized")
    
    def _fetch():
        result = _graph.query(S.QUERY_ARCHITECTURE_OVERVIEW)
        if result.result_set:
            row = result.result_set[0]
            return {
                "total_files": row[0],
                "total_symbols": row[1],
                "languages": row[2],
                "files_with_incoming_calls": row[3],
                "avg_symbols_per_file": round(float(row[4]) if row[4] else 0, 2),
                "avg_callers_per_file": round(float(row[5]) if row[5] else 0, 2),
            }
        return {"error": "no_data"}
    
    return _cached_read("get_architecture_overview", {}, _fetch)


@mcp.tool()
def get_key_modules(limit: int = 10) -> list[dict]:
    """Find key modules (files with high importance based on symbols and incoming calls)."""
    if not _graph:
        raise RuntimeError("MCP server not initialized")
    
    def _fetch():
        result = _graph.query(S.QUERY_KEY_FILES, {"limit": limit})
        return [
            {
                "file_path": row[0],
                "language": row[1],
                "symbol_count": row[2],
                "incoming_calls": row[3],
                "symbols_with_calls": row[4],
                "importance_score": round(float(row[5]), 2),
            }
            for row in result.result_set
        ]
    
    return _cached_read("get_key_modules", {"limit": limit}, _fetch)


@mcp.tool()
def get_file_stats(file_path: str) -> dict:
    """Get detailed stats for a specific file: symbols, incoming calls, outgoing calls."""
    if not _graph:
        raise RuntimeError("MCP server not initialized")
    
    def _fetch():
        result = _graph.query(S.QUERY_FILE_STATS, {"file_path": file_path})
        if result.result_set:
            row = result.result_set[0]
            return {
                "file_path": file_path,
                "symbol_count": row[0],
                "incoming_calls": row[1],
                "symbols_with_outgoing_calls": row[2],
            }
        return {"file_path": file_path, "error": "not_found"}
    
    return _cached_read("get_file_stats", {"file_path": file_path}, _fetch)


@mcp.tool()
def analyze_dependencies(limit: int = 20) -> list[dict]:
    """Analyze top file dependencies: which files call which, and how often."""
    if not _graph:
        raise RuntimeError("MCP server not initialized")
    
    def _fetch():
        result = _graph.query(S.QUERY_MODULE_DEPENDENCIES, {"limit": limit})
        return [
            {
                "from_file": row[0],
                "to_file": row[1],
                "caller_symbols": row[2],
                "callee_symbols": row[3],
            }
            for row in result.result_set
        ]
    
    return _cached_read("analyze_dependencies", {"limit": limit}, _fetch)


@mcp.tool()
def find_dependency_chain(source_path: str, target_path: str) -> dict:
    """Analyze how source_path reaches target_path through call chains (hops)."""
    if not _graph:
        raise RuntimeError("MCP server not initialized")
    
    def _fetch():
        result = _graph.query(
            S.QUERY_FILE_DEPENDENCY_CHAIN,
            {"source_path": source_path, "target_path": target_path},
        )
        if result.result_set:
            chains = [
                {"hops": row[0], "path_count": row[1]}
                for row in result.result_set
            ]
            return {
                "source_path": source_path,
                "target_path": target_path,
                "chains": chains,
                "closest_distance": chains[0]["hops"] if chains else None,
            }
        return {
            "source_path": source_path,
            "target_path": target_path,
            "chains": [],
            "closest_distance": None,
        }
    
    return _cached_read(
        "find_dependency_chain",
        {"source_path": source_path, "target_path": target_path},
        _fetch,
    )


# ---------------------------------------------------------------------------
# Import tracking & external dependencies
# ---------------------------------------------------------------------------

@mcp.tool()
def get_file_imports(file_path: str) -> list[dict]:
    """Get all local imports from a specific file."""
    if not _graph:
        raise RuntimeError("MCP server not initialized")
    
    def _fetch():
        result = _graph.query(S.QUERY_FILE_IMPORTS, {"file_path": file_path})
        return [
            {"target_file": row[0], "language": row[1]}
            for row in result.result_set
        ]
    
    return _cached_read("get_file_imports", {"file_path": file_path}, _fetch)


@mcp.tool()
def get_file_dependents(file_path: str) -> list[dict]:
    """Get all files that import this file."""
    if not _graph:
        raise RuntimeError("MCP server not initialized")
    
    def _fetch():
        result = _graph.query(S.QUERY_IMPORT_DEPENDENTS, {"file_path": file_path})
        return [
            {"dependent_file": row[0], "language": row[1]}
            for row in result.result_set
        ]
    
    return _cached_read("get_file_dependents", {"file_path": file_path}, _fetch)


@mcp.tool()
def get_dependency_overview(limit: int = 30) -> list[dict]:
    """Get dependency graph: which files import which (top by count)."""
    if not _graph:
        raise RuntimeError("MCP server not initialized")
    
    def _fetch():
        result = _graph.query(S.QUERY_DEPENDENCY_GRAPH, {"limit": limit})
        return [
            {
                "from_file": row[0],
                "to_file": row[1],
                "from_language": row[2],
                "to_language": row[3],
            }
            for row in result.result_set
        ]
    
    return _cached_read("get_dependency_overview", {"limit": limit}, _fetch)


@mcp.tool()
def analyze_import_surface(limit: int = 15) -> list[dict]:
    """Analyze files by import dependencies: most imported, most importing."""
    if not _graph:
        raise RuntimeError("MCP server not initialized")
    
    def _fetch():
        result = _graph.query(S.QUERY_EXTERNAL_DEPENDENCIES, {"limit": limit})
        return [
            {
                "file_path": row[0],
                "language": row[1],
                "internal_imports": row[2],
                "incoming_imports": row[3],
            }
            for row in result.result_set
        ]
    
    return _cached_read("analyze_import_surface", {"limit": limit}, _fetch)


@mcp.tool()
def strategy_query(
    query: str,
    graph_top_k: int = 8,
    min_graph_hits: int = 3,
    token_budget: int | None = None,
    relation_depth: int = 1,
    fallback_max_files: int = 3,
) -> dict:
    """Run the default CG-first agent routing strategy through MCP itself."""
    if not _graph:
        raise RuntimeError("MCP server not initialized")
    resolved_token_budget = runtime_config.get_indexing_token_budget() if token_budget is None else token_budget
    return run_cg_first_strategy(
        query=query,
        repo_root=_repo_root,
        retrieve_graph_hits=retrieve_context,
        get_call_graph=find_call_graph,
        graph_top_k=max(1, graph_top_k),
        min_graph_hits=max(1, min_graph_hits),
        token_budget=max(runtime_config.MIN_INDEXING_TOKEN_BUDGET, resolved_token_budget),
        relation_depth=max(1, relation_depth),
        fallback_max_files=max(1, fallback_max_files),
        source_label="contextgraph-server",
    )


# ---------------------------------------------------------------------------
# Call-graph analysis & metrics (v1.17.0)
# ---------------------------------------------------------------------------

@mcp.tool()
def compute_symbol_fan_in(qualified_name: str) -> list[dict]:
    """Compute fan-in: how many distinct symbols call this symbol."""
    if not _graph:
        raise RuntimeError("MCP server not initialized")
    
    def _fetch():
        result = _graph.query(S.QUERY_FAN_IN, {"qualified_name": qualified_name})
        return [
            {"caller": row[0]}
            for row in result.result_set
        ]
    
    return _cached_read("compute_symbol_fan_in", {"qualified_name": qualified_name}, _fetch)


@mcp.tool()
def compute_symbol_fan_out(qualified_name: str) -> list[dict]:
    """Compute fan-out: how many distinct symbols does this symbol call."""
    if not _graph:
        raise RuntimeError("MCP server not initialized")
    
    def _fetch():
        result = _graph.query(S.QUERY_FAN_OUT, {"qualified_name": qualified_name})
        return [
            {"callee": row[0]}
            for row in result.result_set
        ]
    
    return _cached_read("compute_symbol_fan_out", {"qualified_name": qualified_name}, _fetch)


@mcp.tool()
def find_critical_functions(top_n: int = 10) -> list[dict]:
    """Find most critical functions (high fan-in, central in call graph)."""
    if not _graph:
        raise RuntimeError("MCP server not initialized")
    
    def _fetch():
        result = _graph.query(S.QUERY_CRITICAL_FUNCTIONS, {"limit": top_n})
        return [
            {
                "qualified_name": row[0],
                "symbol_type": row[1],
                "fan_in": row[2],
                "fan_out": row[3],
                "importance_score": row[4],
            }
            for row in result.result_set
        ]
    
    return _cached_read("find_critical_functions", {"top_n": top_n}, _fetch)


@mcp.tool()
def detect_cycles() -> list[dict]:
    """Detect all cyclic dependencies in the call graph."""
    if not _graph:
        raise RuntimeError("MCP server not initialized")
    
    def _fetch():
        result = _graph.query(S.QUERY_CYCLIC_DEPENDENCIES, {})
        return [
            {"symbol": row[0]}
            for row in result.result_set
        ]
    
    return _cached_read("detect_cycles", {}, _fetch)


@mcp.tool()
async def workassist_record_activity(
    event_type: str,
    title: str,
    project_id: str | None = None,
    workspace_name: str | None = None,
    external_id: str | None = None,
    summary: str | None = None,
    body_text: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    owner: str | None = None,
    source_url: str | None = None,
    tags: list[str] | None = None,
    occurred_at: str | None = None,
    metadata: dict | None = None,
) -> dict:
    """Record one project work event for CGA-hosted WorkAssist briefing aggregation."""
    service = _require_work_briefing_service()
    resolved_project_id = _resolve_project_external_id(project_id)
    payload = {
        "project_id": resolved_project_id,
        "workspace_name": workspace_name or _resolve_project_name(),
        "event_type": event_type,
        "external_id": external_id,
        "title": title,
        "summary": summary,
        "body_text": body_text,
        "status": status,
        "priority": priority,
        "owner": owner,
        "source_url": source_url,
        "tags": tags,
        "occurred_at": occurred_at,
        "metadata": metadata,
    }
    result = await service.record_activity(payload)
    return {
        "operation": result.operation,
        "activity": result.activity.to_dict(),
    }


@mcp.tool()
async def workassist_list_recent_activity(project_id: str | None = None, limit: int = 25) -> dict:
    """List recent activity for the authenticated MCP project."""
    service = _require_work_briefing_service()
    resolved_project_id = _resolve_project_external_id(project_id)
    safe_limit = max(1, min(int(limit), 100))
    activities = await service.list_recent(project_id=resolved_project_id, limit=safe_limit)
    return {
        "project_id": resolved_project_id,
        "count": len(activities),
        "activities": [activity.to_dict() for activity in activities],
    }


@mcp.tool()
async def workassist_get_activity_briefing(project_id: str | None = None, limit: int = 25) -> dict:
    """Summarize recent activity for the authenticated MCP project."""
    service = _require_work_briefing_service()
    resolved_project_id = _resolve_project_external_id(project_id)
    safe_limit = max(1, min(int(limit), 100))
    return await service.get_briefing(project_id=resolved_project_id, limit=safe_limit)


@mcp.tool()
async def workassist_cleanup_duplicate_activity(project_id: str | None = None, dry_run: bool = True) -> dict:
    """Detect and optionally remove exact duplicate activity rows (same project/plugin/source type/content hash)."""
    service = _require_work_briefing_service()
    resolved_project_id = _resolve_project_external_id(project_id)
    return await service.cleanup_exact_duplicates(project_id=resolved_project_id, dry_run=dry_run)

