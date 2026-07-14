"""CGA (Context Graph Agent) application entry point.

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
import shutil
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
from backend.auth.dependencies import get_current_user, require_admin
from backend.auth.middleware import ProjectTokenMiddleware
from backend.auth.pgshim import get_pool as get_auth_pool
from backend.auth.router import router as auth_router
from backend.auth.security import decode_access_token, hash_token
from backend.ai_first.service import AiFirstEvidencePackNotFoundError
from backend.ai_first.service import AiFirstPolicyProfileError
from backend.ai_first.service import AiFirstProjectNotFoundError
from backend.ai_first.service import AiFirstSignalError
from backend.ai_first.service import AiFirstSignalImportError
from backend.ai_first.service import build_evidence_pack
from backend.ai_first.service import build_readiness_response
from backend.ai_first.service import get_evidence_pack
from backend.ai_first.service import import_azure_devops_signals
from backend.ai_first.service import import_github_signals
from backend.ai_first.service import list_policy_profiles
from backend.ai_first.service import list_evidence_packs
from backend.ai_first.service import list_signals
from backend.ai_first.service import record_signal
from backend.ai_first.service import render_pr_evidence_template
from backend.ai_first.service import save_evidence_pack
from backend.ai_first.service import update_policy_profile
from backend.backup import BackupError, BackupService
from backend.graph.registry import GraphRegistry
from backend.integrations.azure_devops import AzureDevOpsEnricher, AZURE_DEVOPS_RESOURCE_SCOPE
from backend.indexer.consumer import IndexerConsumer
from backend.cga_relay.router import account_router as cga_relay_account_router
from backend.cga_relay.router import router as cga_relay_router
from backend.extensions.router import router as extensions_router
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

APP_VERSION = "1.30.113"
AUTH_SCHEMA_VERSION = 1
GRAPH_SCHEMA_VERSION = 1
RUNTIME_CONFIG_VERSION = 1
WORK_BRIEFING_SCHEMA_VERSION = 1
MIN_RELAY_VERSION = "1.30.83"
RECOMMENDED_RELAY_VERSION = os.getenv("CGA_RECOMMENDED_RELAY_VERSION", MIN_RELAY_VERSION)

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


def _infer_install_flavor() -> str:
    configured = os.getenv("CGA_INSTALL_FLAVOR", "").strip()
    if configured:
        return configured
    backup_dir = BACKUP_DIR.lower()
    if "cga-desktop" in backup_dir:
        return "desktop-bundle"
    if os.getenv("CGA_VERSION"):
        return "release-compose"
    if (REPO_ROOT / ".git").exists():
        return "source-dev"
    if Path("/app").exists():
        return "container-runtime"
    return "unknown"


def _disk_status(path: str) -> dict:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return {"status": "unknown", "detail": "backup path is not available"}
    free_gb = usage.free / (1024**3)
    return {
        "status": "ok" if free_gb >= 2 else "warn",
        "detail": f"{free_gb:.1f} GB free where backups are stored",
        "free_bytes": usage.free,
        "total_bytes": usage.total,
    }


def _upgrade_commands(install_flavor: str) -> list[dict]:
    return [
        {
            "id": "desktop-bundle",
            "label": "Docker Desktop bundle",
            "recommended": install_flavor in {"desktop-bundle", "unknown"},
            "commands": [
                ".\\upgrade-cga-desktop.cmd",
                ".\\start-cga-desktop.cmd",
            ],
        },
        {
            "id": "release-compose",
            "label": "Release compose",
            "recommended": install_flavor == "release-compose",
            "commands": [
                f"# Set CGA_VERSION=v{APP_VERSION} in .env first",
                "docker compose -f docker-compose.release.yml pull",
                "docker compose -f docker-compose.release.yml up -d",
            ],
        },
        {
            "id": "source-dev",
            "label": "Source checkout",
            "recommended": install_flavor == "source-dev",
            "commands": [
                "git pull",
                "docker compose --profile dev up --build",
            ],
        },
    ]


def _upgrade_status_payload() -> dict:
    backup_status = _backup_service.status()
    snapshots = _backup_service.list_snapshots()
    latest_snapshot = snapshots[0] if snapshots else None
    backup_cfg = backup_status.get("config", {})
    backup_dir = backup_status.get("backup_dir") or BACKUP_DIR
    install_flavor = _infer_install_flavor()
    release_channel = os.getenv("CGA_RELEASE_CHANNEL", os.getenv("CGA_UPDATE_CHANNEL", "manual")).strip() or "manual"
    latest_known_version = os.getenv("CGA_LATEST_VERSION", APP_VERSION).strip() or APP_VERSION
    pinned_version = os.getenv("CGA_PINNED_VERSION", "").strip() or None
    disk = _disk_status(backup_dir)

    checks = [
        {
            "id": "backup",
            "label": "Pre-upgrade backup",
            "status": "ok" if latest_snapshot else "warn",
            "detail": latest_snapshot["name"] if latest_snapshot else "create a manual backup before upgrading",
        },
        {
            "id": "backup-scheduler",
            "label": "Backup scheduler",
            "status": "ok" if backup_status.get("scheduler_running") else "warn",
            "detail": "running" if backup_status.get("scheduler_running") else "not running in this process",
        },
        {
            "id": "disk-space",
            "label": "Backup disk space",
            "status": disk["status"],
            "detail": disk["detail"],
        },
        {
            "id": "database-schema",
            "label": "Database schema",
            "status": "ok",
            "detail": f"auth v{AUTH_SCHEMA_VERSION}, work briefing v{WORK_BRIEFING_SCHEMA_VERSION}",
        },
        {
            "id": "graph-schema",
            "label": "Graph index compatibility",
            "status": "ok",
            "detail": f"graph schema v{GRAPH_SCHEMA_VERSION}; reindex not required for this version",
        },
        {
            "id": "relay",
            "label": "CGA-Relay compatibility",
            "status": "ok",
            "detail": f"minimum {MIN_RELAY_VERSION}; recommended {RECOMMENDED_RELAY_VERSION}",
        },
    ]
    score = round(sum(1 for check in checks if check["status"] == "ok") / len(checks) * 100)
    recommendation = (
        "Ready for a backup-first upgrade."
        if score == 100
        else "Create or verify a fresh backup, then upgrade when ready."
    )

    return {
        "ok": True,
        "app_version": APP_VERSION,
        "install_flavor": install_flavor,
        "release_channel": release_channel,
        "latest_known_version": latest_known_version,
        "pinned_version": pinned_version,
        "update_policy": os.getenv("CGA_UPDATE_POLICY", "manual"),
        "manifest_url": os.getenv("CGA_UPDATE_MANIFEST_URL", ""),
        "backup": {
            "backup_dir": backup_dir,
            "scheduler_running": bool(backup_status.get("scheduler_running")),
            "auto_enabled": bool(backup_cfg.get("enabled")),
            "last_run_at": backup_status.get("last_run_at"),
            "last_run_status": backup_status.get("last_run_status"),
            "latest_snapshot": latest_snapshot,
            "snapshot_count": len(snapshots),
        },
        "schema": {
            "auth": AUTH_SCHEMA_VERSION,
            "work_briefing": WORK_BRIEFING_SCHEMA_VERSION,
            "graph": GRAPH_SCHEMA_VERSION,
            "runtime_config": RUNTIME_CONFIG_VERSION,
        },
        "compatibility": {
            "min_relay_version": MIN_RELAY_VERSION,
            "recommended_relay_version": RECOMMENDED_RELAY_VERSION,
            "requires_database_migration": False,
            "requires_graph_reindex": False,
            "recommended_graph_reindex": False,
            "rollback_mode": "restore-pre-upgrade-backup",
        },
        "readiness": {
            "score": score,
            "status": "ready" if score == 100 else "attention",
            "recommendation": recommendation,
            "checks": checks,
        },
        "commands": _upgrade_commands(install_flavor),
    }


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


app = FastAPI(title="CGA (Context Graph Agent)", version=APP_VERSION, lifespan=lifespan)

# ── Auth middleware (validates Bearer token on /mcp routes) ────────────────
app.add_middleware(ProjectTokenMiddleware)

# ── Auth API ───────────────────────────────────────────────────────────────
app.include_router(auth_router, prefix="/api")
app.include_router(cga_relay_router, prefix="/api")
app.include_router(cga_relay_account_router, prefix="/api")
app.include_router(viewer_router, prefix="/api")
app.include_router(schedules_router, prefix="/api")
app.include_router(extensions_router, prefix="/api")


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
    return {"status": "ok", "service": "cga", "name": "Context Graph Agent", "version": APP_VERSION}


@app.get("/mcp")
async def mcp_info() -> dict:
    return {
        "transport": "sse",
        "sse_endpoint": "/mcp/sse",
        "message_endpoint": "/mcp/messages",
        "auth": {
            "type": "Bearer",
            "required_headers": [
                "Authorization",
                "X-Project-ID",
                "X-CGA-Communication-Profile",
                "X-CGA-Key-Establishment",
                "X-CGA-Signature",
                "X-CGA-Transport-Scope",
            ],
            "crystals_profile": {
                "profile": "CRYSTALS-CNSA-2.0",
                "key_establishment": "ML-KEM-1024",
                "signature": "ML-DSA-87",
                "local_transport_scope": "local-ipc",
            },
            "notes": "MCP transport routes require an active mcp token bound to the provided project_id and CRYSTALS/CNSA 2.0 communication profile headers",
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


@app.get("/api/admin/upgrade/status")
async def api_admin_upgrade_status(_: dict = Depends(require_admin)) -> dict:
    return _upgrade_status_payload()


@app.get("/api/indexing/settings")
async def api_indexing_settings(_: dict = Depends(get_current_user)) -> dict:
    return {"indexing": runtime_config.get_runtime_config()["indexing"]}


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


@app.get("/api/admin/ai-first/readiness")
async def api_admin_ai_first_readiness(
    project_id: str | None = Query(default=None),
    _: dict = Depends(require_admin),
) -> dict:
    cleaned_project_id = project_id.strip() if isinstance(project_id, str) else None
    async with get_auth_pool().acquire() as db:
        return await build_readiness_response(
            db=db,
            registry=_registry,
            consumer=_consumer,
            work_briefing_service=_work_briefing_service,
            project_id=cleaned_project_id or None,
        )


@app.get("/api/admin/ai-first/policy-profiles")
async def api_admin_ai_first_policy_profile_list(
    project_id: str | None = Query(default=None),
    _: dict = Depends(require_admin),
) -> dict:
    cleaned_project_id = project_id.strip() if isinstance(project_id, str) else None
    async with get_auth_pool().acquire() as db:
        return await list_policy_profiles(db=db, project_id=cleaned_project_id or None)


@app.patch("/api/admin/ai-first/policy-profiles")
async def api_admin_ai_first_policy_profile_update(
    payload: dict,
    admin: dict = Depends(require_admin),
) -> dict:
    body = dict(payload or {})
    project_id = str(body.get("project_id") or "").strip()
    profile_name = str(body.get("profile_name") or body.get("name") or "").strip()
    if not project_id:
        raise HTTPException(status_code=400, detail="project_id is required")
    if not profile_name:
        raise HTTPException(status_code=400, detail="profile_name is required")
    async with get_auth_pool().acquire() as db:
        try:
            profile = await update_policy_profile(
                db=db,
                project_id=project_id,
                profile_name=profile_name,
                notes=str(body.get("notes") or ""),
                updated_by=str(admin.get("username") or admin.get("id") or "admin"),
            )
        except AiFirstPolicyProfileError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except AiFirstProjectNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"profile": profile}


@app.get("/api/admin/ai-first/signals")
async def api_admin_ai_first_signal_list(
    project_id: str | None = Query(default=None),
    signal_type: str | None = Query(default=None),
    limit: int = Query(default=25, ge=1, le=200),
    _: dict = Depends(require_admin),
) -> dict:
    cleaned_project_id = project_id.strip() if isinstance(project_id, str) else None
    cleaned_signal_type = signal_type.strip() if isinstance(signal_type, str) else None
    async with get_auth_pool().acquire() as db:
        return await list_signals(
            db=db,
            project_id=cleaned_project_id or None,
            signal_type=cleaned_signal_type or None,
            limit=limit,
        )


@app.post("/api/admin/ai-first/signals", status_code=201)
async def api_admin_ai_first_signal_record(
    payload: dict,
    admin: dict = Depends(require_admin),
) -> dict:
    body = dict(payload or {})
    project_id = str(body.get("project_id") or "").strip()
    signal_type = str(body.get("signal_type") or body.get("type") or "").strip()
    if not project_id:
        raise HTTPException(status_code=400, detail="project_id is required")
    if not signal_type:
        raise HTTPException(status_code=400, detail="signal_type is required")
    async with get_auth_pool().acquire() as db:
        try:
            signal = await record_signal(
                db=db,
                project_id=project_id,
                signal_type=signal_type,
                name=str(body.get("name") or ""),
                status=str(body.get("status") or "unknown"),
                value=body.get("value"),
                unit=str(body.get("unit") or ""),
                source_url=str(body.get("source_url") or ""),
                metadata=body.get("metadata") if isinstance(body.get("metadata"), dict) else None,
                observed_at=body.get("observed_at") if isinstance(body.get("observed_at"), str) else None,
                created_by=str(admin.get("username") or admin.get("id") or "admin"),
            )
        except AiFirstSignalError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except AiFirstProjectNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"signal": signal}


@app.post("/api/admin/ai-first/signals/import-github", status_code=201)
async def api_admin_ai_first_signal_import_github(
    payload: dict,
    admin: dict = Depends(require_admin),
) -> dict:
    body = dict(payload or {})
    project_id = str(body.get("project_id") or "").strip()
    if not project_id:
        raise HTTPException(status_code=400, detail="project_id is required")
    async with get_auth_pool().acquire() as db:
        try:
            return await import_github_signals(
                db=db,
                project_id=project_id,
                repo_url=str(body.get("repo_url") or "") or None,
                limit=max(1, min(int(body.get("limit") or 5), 20)),
                created_by=str(admin.get("username") or admin.get("id") or "admin"),
            )
        except AiFirstProjectNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except AiFirstSignalImportError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/admin/ai-first/signals/import-azure-devops", status_code=201)
async def api_admin_ai_first_signal_import_azure_devops(
    payload: dict,
    admin: dict = Depends(require_admin),
) -> dict:
    body = dict(payload or {})
    project_id = str(body.get("project_id") or "").strip()
    if not project_id:
        raise HTTPException(status_code=400, detail="project_id is required")
    async with get_auth_pool().acquire() as db:
        try:
            return await import_azure_devops_signals(
                db=db,
                project_id=project_id,
                repo_url=str(body.get("repo_url") or "") or None,
                organization=str(body.get("organization") or "") or None,
                ado_project=str(body.get("ado_project") or body.get("project") or "") or None,
                repository=str(body.get("repository") or "") or None,
                limit=max(1, min(int(body.get("limit") or 5), 20)),
                created_by=str(admin.get("username") or admin.get("id") or "admin"),
            )
        except AiFirstProjectNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except AiFirstSignalImportError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/admin/ai-first/evidence")
async def api_admin_ai_first_evidence(
    project_id: str = Query(..., min_length=1),
    limit: int = Query(default=25, ge=1, le=200),
    task_id: str | None = Query(default=None),
    issue_id: str | None = Query(default=None),
    pr_id: str | None = Query(default=None),
    activity_id: str | None = Query(default=None),
    _: dict = Depends(require_admin),
) -> dict:
    cleaned_project_id = project_id.strip()
    if not cleaned_project_id:
        raise HTTPException(status_code=400, detail="project_id is required")
    async with get_auth_pool().acquire() as db:
        try:
            return await build_evidence_pack(
                db=db,
                registry=_registry,
                consumer=_consumer,
                work_briefing_service=_work_briefing_service,
                project_id=cleaned_project_id,
                limit=limit,
                task_id=task_id,
                issue_id=issue_id,
                pr_id=pr_id,
                activity_id=activity_id,
                trace_redis_url=CACHE_REDIS_URL,
            )
        except AiFirstProjectNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/admin/ai-first/evidence-packs", status_code=201)
async def api_admin_ai_first_evidence_pack_save(
    payload: dict,
    admin: dict = Depends(require_admin),
) -> dict:
    body = dict(payload or {})
    project_id = str(body.get("project_id") or "").strip()
    if not project_id:
        raise HTTPException(status_code=400, detail="project_id is required")
    limit = int(body.get("limit") or 25)
    async with get_auth_pool().acquire() as db:
        try:
            pack = await build_evidence_pack(
                db=db,
                registry=_registry,
                consumer=_consumer,
                work_briefing_service=_work_briefing_service,
                project_id=project_id,
                limit=max(1, min(limit, 200)),
                task_id=body.get("task_id"),
                issue_id=body.get("issue_id"),
                pr_id=body.get("pr_id"),
                activity_id=body.get("activity_id"),
                trace_redis_url=CACHE_REDIS_URL,
            )
            saved = await save_evidence_pack(
                db=db,
                pack=pack,
                created_by=str(admin.get("username") or admin.get("id") or "admin"),
            )
        except AiFirstProjectNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"saved": saved, "evidence": pack}


@app.get("/api/admin/ai-first/evidence-packs")
async def api_admin_ai_first_evidence_pack_list(
    project_id: str | None = Query(default=None),
    limit: int = Query(default=25, ge=1, le=200),
    _: dict = Depends(require_admin),
) -> dict:
    cleaned_project_id = project_id.strip() if isinstance(project_id, str) else None
    async with get_auth_pool().acquire() as db:
        return await list_evidence_packs(db=db, project_id=cleaned_project_id or None, limit=limit)


@app.get("/api/admin/ai-first/evidence-packs/{evidence_id}")
async def api_admin_ai_first_evidence_pack_get(
    evidence_id: str,
    _: dict = Depends(require_admin),
) -> dict:
    async with get_auth_pool().acquire() as db:
        try:
            return await get_evidence_pack(db=db, evidence_id=evidence_id)
        except AiFirstEvidencePackNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/admin/ai-first/evidence-packs/{evidence_id}/pr-template")
async def api_admin_ai_first_evidence_pack_pr_template(
    evidence_id: str,
    request: Request,
    _: dict = Depends(require_admin),
) -> dict:
    async with get_auth_pool().acquire() as db:
        try:
            saved = await get_evidence_pack(db=db, evidence_id=evidence_id)
        except AiFirstEvidencePackNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    return render_pr_evidence_template(saved, base_url=str(request.base_url).rstrip("/"))


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


