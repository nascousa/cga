"""CGA (ContextGraphAgent) application entry point.

Startup sequence
----------------
1. Connect to FalkorDB and ensure graph indexes.
2. Connect MCP producer to Redis Stream (client-side MQ).
3. Initialize Redis query cache (Phase 3).
4. Initialize trace recorder + evaluator (Phase 3).
5. Initialize MCP tool registry with live graph + producer + cache + recorder.
6. Launch indexer consumer loop (server-side MQ) as background task.
7. Launch trace evaluator background coroutine.

Shutdown sequence reverses the above gracefully.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import httpx

import structlog
import uvicorn
from fastapi import Depends, FastAPI
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from jose import JWTError
from fastapi.responses import FileResponse
from fastapi.responses import RedirectResponse
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from backend import runtime_config
from backend.auth.database import DB_PATH, init_db, insert_audit_log
from backend.auth.dependencies import require_admin
from backend.auth.middleware import ProjectTokenMiddleware
from backend.auth.pgshim import get_pool as get_auth_pool
from backend.auth.router import router as auth_router
from backend.auth.security import decode_access_token, hash_token
from backend.backup import BackupError, BackupService
from backend.graph.registry import GraphRegistry
from backend.integrations.azure_devops import AzureDevOpsEnricher, AZURE_DEVOPS_RESOURCE_SCOPE
from backend.indexer.consumer import IndexerConsumer
from backend.tools.producer import MCPProducer
from backend.tools import server as mcp_server
from backend.perf.context_quality import benchmark_context_quality, ContextQualityInputError
from backend.perf.token_efficiency import benchmark_token_efficiency, TokenBenchmarkInputError
from backend.schedules.router import router as schedules_router
from backend.schedules.service import ScheduledTaskWorker
from backend.viewer.router import router as viewer_router
from backend.workbriefing.service import WorkActivityValidationError, WorkBriefingService
from backend.workbriefing.store import PgVectorActivityStore, resolve_dsn

log = structlog.get_logger()

APP_VERSION = "1.30.43"

FALKORDB_HOST = os.getenv("FALKORDB_HOST", "localhost")
FALKORDB_PORT = int(os.getenv("FALKORDB_PORT", "6379"))
FALKORDB_URL = os.getenv("FALKORDB_URL", f"falkor://{FALKORDB_HOST}:{FALKORDB_PORT}")
_fdb_parsed = urlparse(FALKORDB_URL)
_fdb_browser_default = (
    f"http://{_fdb_parsed.hostname}:3000"
    if _fdb_parsed.hostname and _fdb_parsed.hostname not in {"localhost", "127.0.0.1"}
    else "http://localhost:13000"
)
FALKORDB_BROWSER_URL = os.getenv("FALKORDB_BROWSER_URL", _fdb_browser_default).rstrip("/")
FALKORDB_BROWSER_PUBLIC_URL = os.getenv("FALKORDB_BROWSER_PUBLIC_URL", "http://localhost:13000").rstrip("/")
QUEUE_REDIS_URL = os.getenv("QUEUE_REDIS_URL", "redis://localhost:6380/1")
CACHE_REDIS_URL = os.getenv("CACHE_REDIS_URL", "redis://localhost:6380")  # db=2 set in QueryCache
TRACE_ENABLED = os.getenv("TRACE_ENABLED", "true").lower() == "true"
REPO_ROOT = Path(os.getenv("CONTEXTGRAPH_REPO_ROOT", ".")).resolve()
BACKUP_DIR = os.getenv("BACKUP_DIR") or "/app/data/backups"
MICROSOFT_OAUTH_TENANT = os.getenv("MICROSOFT_OAUTH_TENANT", "organizations").strip() or "organizations"
MICROSOFT_OAUTH_CLIENT_ID = (
    os.getenv("MICROSOFT_OAUTH_CLIENT_ID")
    or os.getenv("CGA_MICROSOFT_CLIENT_ID")
    or os.getenv("AZURE_DEVOPS_OAUTH_CLIENT_ID")
    or "04b07795-8ddb-461a-bbee-02f9e1bf7b46"
).strip()
MICROSOFT_OAUTH_SCOPE = (os.getenv("MICROSOFT_OAUTH_SCOPE") or AZURE_DEVOPS_RESOURCE_SCOPE).strip()
MICROSOFT_TOKEN_URL = f"https://login.microsoftonline.com/{MICROSOFT_OAUTH_TENANT}/oauth2/v2.0/token"


class NoStoreStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: dict) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

_registry = GraphRegistry(host=FALKORDB_HOST, port=FALKORDB_PORT)
_producer = MCPProducer(redis_url=QUEUE_REDIS_URL)
_consumer: IndexerConsumer | None = None
_consumer_task: asyncio.Task | None = None
_trace_evaluator = None
_work_briefing_pg_dsn = resolve_dsn(os.getenv("WORKBRIEFING_POSTGRES_DSN"))
_work_briefing_service = WorkBriefingService(store=PgVectorActivityStore(dsn=_work_briefing_pg_dsn))
_backup_service = BackupService(dsn=DB_PATH, backup_dir=BACKUP_DIR)
_scheduled_task_worker = ScheduledTaskWorker()


def _redact_dsn(dsn: str) -> str:
    try:
        parsed = urlparse(dsn)
        if parsed.password:
            netloc = f"{parsed.username}:***@{parsed.hostname}"
            if parsed.port:
                netloc += f":{parsed.port}"
            return parsed._replace(netloc=netloc).geturl()
    except Exception:
        pass
    return dsn


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _consumer, _consumer_task, _trace_evaluator

    # Startup: auth DB (PostgreSQL via asyncpg shim)
    await init_db()
    log.info("auth.pg.ready", dsn=_redact_dsn(DB_PATH))

    # WorkBriefing pgvector storage
    try:
        await _work_briefing_service._store.ensure_schema()
        log.info("workbriefing.pgvector.ready", dsn=_redact_dsn(_work_briefing_pg_dsn))
    except Exception as exc:
        log.warning("workbriefing.pgvector.unavailable", reason=str(exc), dsn=_redact_dsn(_work_briefing_pg_dsn))

    # Registry connects graphs lazily per project on first use
    await _producer.connect()

    # Phase 3: query cache
    cache = None
    recorder = None
    try:
        from backend.cache.query_cache import QueryCache
        cache = QueryCache(redis_url=CACHE_REDIS_URL)
        log.info("cache.connected")
    except Exception as exc:
        log.warning("cache.disabled", reason=str(exc))

    # Phase 3: trace recorder + evaluator
    if TRACE_ENABLED:
        try:
            from backend.eval.trace_eval import TraceRecorder, TraceEvaluator
            recorder = TraceRecorder(redis_url=CACHE_REDIS_URL)
            # TraceEvaluator uses a default graph; per-project eval is a future improvement
            _trace_evaluator = TraceEvaluator(redis_url=CACHE_REDIS_URL, graph_client=None)
            await _trace_evaluator.start()
        except Exception as exc:
            log.warning("trace_eval.disabled", reason=str(exc))

    mcp_server.init(
        registry=_registry,
        producer=_producer,
        cache=cache,
        recorder=recorder,
        work_briefing_service=_work_briefing_service,
    )
    _consumer = IndexerConsumer(redis_url=QUEUE_REDIS_URL, registry=_registry)
    mcp_server.set_consumer(_consumer)  # Wire consumer for MCP queue enrichment
    _consumer_task = asyncio.create_task(_consumer.start())
    
    # Make consumer available via app state for API endpoints
    app.state.consumer = _consumer
    app.state.registry = _registry

    await _backup_service.start()
    await _scheduled_task_worker.start()

    log.info("cga.started", falkordb=f"{FALKORDB_HOST}:{FALKORDB_PORT}")

    yield

    # Shutdown
    await _scheduled_task_worker.stop()
    await _backup_service.stop()
    if _consumer:
        await _consumer.stop()
    if _consumer_task:
        _consumer_task.cancel()
        try:
            await _consumer_task
        except asyncio.CancelledError:
            pass
    if _trace_evaluator:
        await _trace_evaluator.stop()
    if recorder:
        recorder.close()
    if cache:
        cache.close()
    await _producer.close()
    _registry.close_all()
    try:
        await _work_briefing_service._store.close()
    except Exception as exc:
        log.warning("workbriefing.pgvector.close_failed", reason=str(exc))
    try:
        from backend.auth.pgshim import close_pool as _close_auth_pool
        await _close_auth_pool()
    except Exception as exc:
        log.warning("auth.pg.close_failed", reason=str(exc))
    log.info("cga.stopped")


app = FastAPI(title="CGA (ContextGraphAgent)", version="1.30.43", lifespan=lifespan)

# ── Auth middleware (validates Bearer token on /mcp routes) ────────────────
app.add_middleware(ProjectTokenMiddleware)

# ── Auth API ───────────────────────────────────────────────────────────────
app.include_router(auth_router, prefix="/api")
app.include_router(viewer_router, prefix="/api")
app.include_router(schedules_router, prefix="/api")


def _truncate(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    return value if len(value) <= limit else (value[: limit - 1] + "…")


def _redact_sensitive(data):
    if isinstance(data, dict):
        redacted = {}
        for key, value in data.items():
            key_l = str(key).lower()
            if any(s in key_l for s in ("password", "token", "secret", "authorization", "api_key", "access_key")):
                redacted[key] = "***"
            else:
                redacted[key] = _redact_sensitive(value)
        return redacted
    if isinstance(data, list):
        return [_redact_sensitive(v) for v in data]
    return data


def _parse_json_text(value: str | bytes | None):
    if value is None:
        return None
    try:
        if isinstance(value, bytes):
            if not value:
                return None
            return json.loads(value.decode("utf-8", errors="ignore"))
        if isinstance(value, str):
            if not value.strip():
                return None
            return json.loads(value)
    except Exception:
        return None
    return None


def _normalize_usage_dict(candidate: dict) -> dict | None:
    prompt = candidate.get("prompt_tokens", candidate.get("input_tokens"))
    completion = candidate.get("completion_tokens", candidate.get("output_tokens"))
    total = candidate.get("total_tokens", candidate.get("token_usage_total"))

    try:
        prompt_n = int(prompt) if prompt is not None else None
    except Exception:
        prompt_n = None
    try:
        completion_n = int(completion) if completion is not None else None
    except Exception:
        completion_n = None
    try:
        total_n = int(total) if total is not None else None
    except Exception:
        total_n = None

    if total_n is None and (prompt_n is not None or completion_n is not None):
        total_n = max(0, int(prompt_n or 0) + int(completion_n or 0))

    if total_n is None and prompt_n is None and completion_n is None:
        return None

    return {
        "prompt_tokens": max(0, int(prompt_n or 0)),
        "completion_tokens": max(0, int(completion_n or 0)),
        "total_tokens": max(0, int(total_n or 0)),
    }


def _find_usage_dict(obj) -> dict | None:
    if not isinstance(obj, dict):
        return None

    direct = _normalize_usage_dict(obj)
    if direct:
        return direct

    usage_like = obj.get("usage")
    if isinstance(usage_like, dict):
        from_usage = _normalize_usage_dict(usage_like)
        if from_usage:
            return from_usage

    for value in obj.values():
        if isinstance(value, dict):
            nested = _find_usage_dict(value)
            if nested:
                return nested
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    nested = _find_usage_dict(item)
                    if nested:
                        return nested
    return None


def _require_project_token_context(request: Request) -> dict:
    state = request.scope.get("state", {})
    project_id = state.get("project_id")
    project_name = state.get("project_name")
    if not project_id or not project_name:
        raise HTTPException(status_code=401, detail="Project token required")
    return {
        "project_id": str(project_id),
        "project_name": str(project_name),
        "project_db_id": state.get("project_db_id"),
        "project_token_id": state.get("project_token_id"),
        "project_token_type": state.get("project_token_type"),
    }


def _resolve_project_scope_id(requested_project_id: str | None, project: dict) -> str:
    cleaned = requested_project_id.strip() if isinstance(requested_project_id, str) else ""
    if cleaned and cleaned != project["project_id"]:
        raise HTTPException(status_code=403, detail="project_id must match the authenticated project")
    return project["project_id"]


@app.middleware("http")
async def audit_request_middleware(request: Request, call_next):
    path = request.url.path
    should_audit = path.startswith("/api") or path.startswith("/mcp")
    if not should_audit:
        return await call_next(request)

    start = time.perf_counter()
    method = request.method
    query_string = request.url.query
    user_agent = request.headers.get("user-agent")
    client_ip = request.client.host if request.client else None

    actor_type = "anonymous"
    actor_id = None
    actor_name = None
    project_id = None
    project_name = None
    token_id = None

    raw_auth = request.headers.get("authorization", "")
    token_usage_eligible = path.startswith("/mcp/messages") or path in {
        "/api/benchmark/token-efficiency",
        "/api/benchmark/context-quality",
    }
    if raw_auth.startswith("Bearer "):
        bearer_token = raw_auth[len("Bearer ") :]
        if path.startswith("/api"):
            try:
                claims = decode_access_token(bearer_token)
                actor_name = claims.get("sub")
                actor_type = "user"
                if actor_name:
                    from backend.auth import pgshim as _pgshim

                    async with _pgshim.get_pool().acquire() as db:
                        async with db.execute(
                            "SELECT id FROM users WHERE username = ?",
                            (actor_name,),
                        ) as cur:
                            user_row = await cur.fetchone()
                            actor_id = user_row["id"] if user_row else None
            except JWTError:
                actor_type = "anonymous"
        elif path.startswith("/mcp"):
            try:
                from backend.auth import pgshim as _pgshim

                digest = hash_token(bearer_token)
                async with _pgshim.get_pool().acquire() as db:
                    async with db.execute(
                        """
                        SELECT pt.id AS token_id, pt.project_id, p.project_name
                        FROM project_tokens pt
                        JOIN projects p ON p.id = pt.project_id
                        WHERE pt.token_hash = ?
                        """,
                        (digest,),
                    ) as cur:
                        token_row = await cur.fetchone()
                        if token_row:
                            actor_type = "project_token"
                            token_id = token_row["token_id"]
                            project_id = token_row["project_id"]
                            project_name = token_row["project_name"]
                            actor_name = f"token:{token_id}"
            except Exception:
                pass

    request_body = None
    request_obj = None
    if method in {"POST", "PUT", "PATCH", "DELETE"}:
        try:
            body_bytes = await request.body()
            if body_bytes:
                content_type = request.headers.get("content-type", "")
                if "application/json" in content_type:
                    request_obj = _parse_json_text(body_bytes)
                    request_body = json.dumps(_redact_sensitive(request_obj), ensure_ascii=True)
                else:
                    request_body = "<non-json-body>"
        except Exception:
            request_body = "<body-unavailable>"

    response = None
    status_code = 500
    error_message = None
    response_obj = None
    usage_obj = None
    token_usage_total = None
    try:
        response = await call_next(request)
        status_code = response.status_code

        content_type = (response.headers.get("content-type") or "").lower()
        is_json_response = "application/json" in content_type
        is_sse = path.startswith("/mcp/sse") or "text/event-stream" in content_type

        if is_json_response and not is_sse:
            response_body_bytes = b""
            if getattr(response, "body", None) is not None:
                response_body_bytes = response.body  # type: ignore[assignment]
            elif getattr(response, "body_iterator", None) is not None:
                chunks = []
                async for chunk in response.body_iterator:
                    chunks.append(chunk)
                response_body_bytes = b"".join(chunks)
                response_headers = dict(response.headers)
                response_headers.pop("content-length", None)
                response = Response(
                    content=response_body_bytes,
                    status_code=response.status_code,
                    headers=response_headers,
                    media_type=response.media_type,
                    background=response.background,
                )

            response_obj = _parse_json_text(response_body_bytes)
            usage_obj = (_find_usage_dict(response_obj) or _find_usage_dict(request_obj)) if token_usage_eligible else None
            if usage_obj and token_usage_eligible:
                token_usage_total = int(usage_obj.get("total_tokens") or 0)

        return response
    except Exception as exc:
        error_message = str(exc)
        raise
    finally:
        duration_ms = int((time.perf_counter() - start) * 1000)
        scope_state = request.scope.get("state", {})
        if not project_id:
            project_id = scope_state.get("project_db_id")
        if not project_name:
            project_name = scope_state.get("project_name")
        if not token_id:
            token_id = scope_state.get("project_token_id")
        if actor_type == "anonymous" and token_id:
            actor_type = "project_token"
            actor_name = actor_name or f"token:{token_id}"
        scope_name = "mcp" if path.startswith("/mcp") else "api"
        details_payload = {
            "auth_header_present": bool(raw_auth),
            "status_bucket": f"{status_code // 100}xx",
        }
        if usage_obj:
            details_payload["llm_usage"] = usage_obj
        try:
            await insert_audit_log(
                scope=scope_name,
                method=method,
                path=path,
                status_code=status_code,
                duration_ms=duration_ms,
                actor_type=actor_type,
                actor_id=actor_id,
                actor_name=actor_name,
                project_id=project_id,
                project_name=project_name,
                token_id=token_id,
                client_ip=_truncate(client_ip, 128),
                user_agent=_truncate(user_agent, 512),
                query_string=_truncate(query_string, 512),
                request_body=_truncate(request_body, 2000),
                response_error=_truncate(error_message, 1000),
                details=details_payload,
                token_usage_total=token_usage_total,
            )
        except Exception as exc:
            log.warning("audit.write_failed", reason=str(exc), path=path)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "cga", "name": "ContextGraphAgent", "version": APP_VERSION}


@app.get("/mcp")
async def mcp_info() -> dict:
    return {
        "transport": "sse",
        "sse_endpoint": "/mcp/sse",
        "message_endpoint": "/mcp/messages",
        "auth": {
            "type": "Bearer",
            "required_headers": ["Authorization", "X-Project-ID"],
            "notes": "Bearer token must be an active mcp token bound to the provided project_id",
        },
    }


@app.post("/api/benchmark/token-efficiency")
async def api_benchmark_token_efficiency(payload: dict) -> dict:
    """Benchmark estimated token savings between baseline and CG/MCP contexts.

    Shared API intended for any downstream project, not tied to a specific repository.
    """
    try:
        return benchmark_token_efficiency(payload=payload, repo_root=REPO_ROOT)
    except TokenBenchmarkInputError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/benchmark/context-quality")
async def api_benchmark_context_quality(payload: dict) -> dict:
    """Benchmark context quality and HPS for baseline-vs-CG context bundles."""
    try:
        return benchmark_context_quality(payload=payload, repo_root=REPO_ROOT)
    except ContextQualityInputError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/admin/runtime-config")
async def api_admin_runtime_config(_: dict = Depends(require_admin)) -> dict:
    return {
        "falkordb_url": FALKORDB_URL,
        **runtime_config.get_runtime_config(),
    }


@app.patch("/api/admin/runtime-config")
async def api_admin_runtime_config_update(payload: dict, _: dict = Depends(require_admin)) -> dict:
    try:
        updated = runtime_config.update_runtime_config(payload or {})
    except runtime_config.RuntimeConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "falkordb_url": FALKORDB_URL, **updated}


@app.get("/api/admin/backups")
async def api_admin_backups_list(_: dict = Depends(require_admin)) -> dict:
    return {
        "status": _backup_service.status(),
        "snapshots": _backup_service.list_snapshots(),
    }


@app.post("/api/admin/backups/run")
async def api_admin_backups_run(_: dict = Depends(require_admin)) -> dict:
    try:
        result = await _backup_service.run_backup(reason="manual")
    except BackupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "snapshot": result}


@app.post("/api/admin/backups/restore")
async def api_admin_backups_restore(payload: dict, _: dict = Depends(require_admin)) -> dict:
    name = (payload or {}).get("name")
    if not isinstance(name, str) or not name:
        raise HTTPException(status_code=400, detail="missing snapshot name")
    try:
        result = await _backup_service.restore(name)
    except BackupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, **result}


@app.delete("/api/admin/backups/{name}")
async def api_admin_backups_delete(name: str, _: dict = Depends(require_admin)) -> dict:
    try:
        await _backup_service.delete(name)
    except BackupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


@app.get("/api/admin/backups/download/{name}")
async def api_admin_backups_download(name: str, _: dict = Depends(require_admin)):
    try:
        path = _backup_service.snapshot_path(name)
    except BackupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=path.name,
        headers={"Cache-Control": "no-store"},
    )


@app.patch("/api/admin/backups/config")
async def api_admin_backups_update_config(payload: dict, _: dict = Depends(require_admin)) -> dict:
    cfg = _backup_service.update_config(payload or {})
    return {"ok": True, "config": cfg.to_dict()}


@app.get("/api/admin/work-briefing")
async def api_admin_work_briefing(
    project_id: str | None = Query(default=None),
    limit: int = Query(default=25, ge=1, le=5000),
    include_external: bool = Query(default=False),
    admin: dict = Depends(require_admin),
) -> dict:
    cleaned_project_id = project_id.strip() if isinstance(project_id, str) else None
    payload = await _work_briefing_service.get_briefing(project_id=cleaned_project_id or None, limit=limit)
    if include_external and MICROSOFT_OAUTH_CLIENT_ID:
        async with get_auth_pool().acquire() as db:
            enricher = AzureDevOpsEnricher(
                db=db,
                user_id=int(admin["id"]),
                client_id=MICROSOFT_OAUTH_CLIENT_ID,
                token_url=MICROSOFT_TOKEN_URL,
                scope=MICROSOFT_OAUTH_SCOPE,
            )
            return await enricher.enrich_briefing(payload)
    if include_external:
        return {**payload, "external_enrichment": {"status": "not_configured", "provider": "azure_devops"}}
    return payload


@app.get("/api/admin/work-briefing/activities")
async def api_admin_work_briefing_activities(
    project_id: str | None = Query(default=None),
    limit: int = Query(default=25, ge=1, le=5000),
    _: dict = Depends(require_admin),
) -> dict:
    cleaned_project_id = project_id.strip() if isinstance(project_id, str) else None
    activities = await _work_briefing_service.list_recent(project_id=cleaned_project_id or None, limit=limit)
    return {
        "project_id": cleaned_project_id or None,
        "count": len(activities),
        "activities": [activity.to_dict() for activity in activities],
    }


@app.post("/api/project/work-briefing/activity", status_code=201)
async def api_project_work_briefing_record_activity(
    payload: dict,
    request: Request,
) -> dict:
    project = _require_project_token_context(request)
    body = dict(payload or {})
    body["project_id"] = _resolve_project_scope_id(body.get("project_id"), project)
    if not body.get("workspace_name"):
        body["workspace_name"] = project["project_name"]

    try:
        result = await _work_briefing_service.record_activity(body)
    except WorkActivityValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "operation": result.operation,
        "activity": result.activity.to_dict(),
    }


@app.get("/api/project/work-briefing")
async def api_project_work_briefing(
    request: Request,
    project_id: str | None = Query(default=None),
    limit: int = Query(default=25, ge=1, le=5000),
) -> dict:
    project = _require_project_token_context(request)
    resolved_project_id = _resolve_project_scope_id(project_id, project)
    return await _work_briefing_service.get_briefing(project_id=resolved_project_id, limit=limit)


@app.get("/api/project/work-briefing/activities")
async def api_project_work_briefing_activities(
    request: Request,
    project_id: str | None = Query(default=None),
    limit: int = Query(default=25, ge=1, le=5000),
) -> dict:
    project = _require_project_token_context(request)
    resolved_project_id = _resolve_project_scope_id(project_id, project)
    activities = await _work_briefing_service.list_recent(project_id=resolved_project_id, limit=limit)
    return {
        "project_id": resolved_project_id,
        "count": len(activities),
        "activities": [activity.to_dict() for activity in activities],
    }


@app.post("/api/admin/fdb-browser/launch")
async def api_admin_fdb_browser_launch(response: Response, _: dict = Depends(require_admin)) -> dict:
    parsed = urlparse(FALKORDB_URL)
    host = parsed.hostname or FALKORDB_HOST or "localhost"
    port = parsed.port or 6379

    login_url = f"{FALKORDB_BROWSER_PUBLIC_URL}/login?host={host}&port={port}"
    graph_url = f"{FALKORDB_BROWSER_PUBLIC_URL}/graph"

    try:
      with httpx.Client(timeout=8.0, follow_redirects=True) as client:
          csrf_resp = client.get(f"{FALKORDB_BROWSER_URL}/api/auth/csrf")
          csrf_resp.raise_for_status()
          csrf_token = (csrf_resp.json() or {}).get("csrfToken")
          if not csrf_token:
              raise ValueError("Missing csrfToken from FalkorDB Browser")

          callback_payload = {
              "redirect": "false",
              "host": host,
              "port": str(port),
              "tls": "false",
              "csrfToken": csrf_token,
              "callbackUrl": login_url,
              "json": "true",
          }
          cb_resp = client.post(
              f"{FALKORDB_BROWSER_URL}/api/auth/callback/credentials",
              data=callback_payload,
              headers={"Content-Type": "application/x-www-form-urlencoded"},
          )
          cb_resp.raise_for_status()

          for cookie in client.cookies.jar:
              if not cookie.name.startswith("next-auth."):
                  continue
              response.set_cookie(
                  key=cookie.name,
                  value=cookie.value,
                  path=cookie.path or "/",
                  expires=cookie.expires,
                  httponly=True,
                  secure=bool(cookie.secure),
                  samesite="lax",
              )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to establish Falkor session: {exc}") from exc

    return {
        "url": graph_url,
    }


# ── MCP SSE transport ──────────────────────────────────────────────────────
# Mount at /mcp; let FastMCP generate relative endpoints to avoid duplicated
# /mcp prefix (which can otherwise yield /mcp/mcp/messages on clients).
app.mount("/mcp", mcp_server.mcp.sse_app())

# ── Admin SPA (served last so API routes take precedence) ─────────────────
_FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
_VIEWER = Path(__file__).resolve().parents[1] / "viewer"


def _admin_ui_response() -> FileResponse:
    return FileResponse(
        _FRONTEND / "index.html",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/admin", include_in_schema=False)
async def admin_ui():
    return _admin_ui_response()


@app.get("/admin/{admin_path:path}", include_in_schema=False)
async def admin_deep_link_ui(admin_path: str):
    return _admin_ui_response()


@app.get("/viewer", include_in_schema=False)
async def graph_viewer_ui():
    return RedirectResponse(url="/viewer/", status_code=307)


if _VIEWER.is_dir():
    app.mount("/viewer", NoStoreStaticFiles(directory=str(_VIEWER), html=True), name="viewer")


if _FRONTEND.is_dir():
    app.mount("/", StaticFiles(directory=str(_FRONTEND), html=True), name="frontend")


if __name__ == "__main__":
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=False)


