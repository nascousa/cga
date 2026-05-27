"""PostgreSQL-backed auth database (asyncpg via the aiosqlite-shim).

History
-------
This module previously used aiosqlite against ``/app/data/auth.db``.
It has been migrated to PostgreSQL.  The schema deliberately mirrors
the original SQLite layout — ``BIGSERIAL`` for autoincrement ids,
``INTEGER`` for boolean flags, ``TEXT`` for ISO-8601 timestamps — so
that the SQL emitted by :mod:`backend.auth.router` and friends
continues to work unchanged.

Public surface
--------------
* :data:`DB_PATH` — kept for backward compatibility but now holds the
  active PostgreSQL DSN string (logged at startup, used by tests).
* :func:`init_db` — create tables if missing.
* :func:`get_db` — FastAPI dependency yielding a shimmed connection.
* :func:`insert_audit_log` — write a single audit row.
"""
from __future__ import annotations

import json
import secrets
import string
from datetime import datetime, timezone
from typing import AsyncGenerator

from backend.auth.pgshim import (
    Connection,
    get_pool,
    init_pool,
    resolve_auth_dsn,
)

# ``DB_PATH`` historically held a filesystem path; we keep the name so
# existing imports (middleware, main, scripts) continue to work but it
# now stores the active Postgres DSN.  Callers that only need a label
# can use this verbatim; callers that mutate or open files based on it
# have been updated.
DB_PATH = resolve_auth_dsn()


_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS users (
    id            BIGSERIAL PRIMARY KEY,
    username      TEXT    UNIQUE NOT NULL,
    password_hash TEXT    NOT NULL,
    email         TEXT    NOT NULL DEFAULT '',
    auth_provider TEXT    NOT NULL DEFAULT 'local',
    github_id     TEXT    UNIQUE,
    role          TEXT    NOT NULL DEFAULT 'developer',
    created_at    TEXT    NOT NULL DEFAULT to_char((now() at time zone 'utc'), 'YYYY-MM-DD"T"HH24:MI:SS'),
    is_active     INTEGER NOT NULL DEFAULT 1
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_users_github_id
    ON users(github_id)
    WHERE github_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS projects (
    id           BIGSERIAL PRIMARY KEY,
    project_name TEXT    UNIQUE NOT NULL,
    project_id   TEXT    UNIQUE NOT NULL,
    upstream_url TEXT    NOT NULL DEFAULT '',
    description  TEXT    NOT NULL DEFAULT '',
    repo_path    TEXT    NOT NULL DEFAULT '',
    created_at   TEXT    NOT NULL DEFAULT to_char((now() at time zone 'utc'), 'YYYY-MM-DD"T"HH24:MI:SS'),
    is_active    INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS project_tokens (
    id          BIGSERIAL PRIMARY KEY,
    project_id  BIGINT  NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    token_type  TEXT    NOT NULL,
    token_hash  TEXT    UNIQUE NOT NULL,
    token_hint  TEXT    NOT NULL,
    version     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT    NOT NULL DEFAULT to_char((now() at time zone 'utc'), 'YYYY-MM-DD"T"HH24:MI:SS'),
    is_active   INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_token_hash
    ON project_tokens(token_hash)
    WHERE is_active = 1;

CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id              BIGSERIAL PRIMARY KEY,
    task_id         TEXT    NOT NULL,
    name            TEXT    NOT NULL,
    description     TEXT    NOT NULL DEFAULT '',
    task_type       TEXT    NOT NULL,
    project_id      BIGINT  REFERENCES projects(id) ON DELETE SET NULL,
    agent_id        TEXT    NOT NULL DEFAULT '',
    target_url      TEXT    NOT NULL DEFAULT '',
    payload_json    TEXT    NOT NULL DEFAULT '{}',
    cadence_minutes INTEGER NOT NULL DEFAULT 60,
    timeout_seconds INTEGER NOT NULL DEFAULT 30,
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT    NOT NULL DEFAULT to_char((now() at time zone 'utc'), 'YYYY-MM-DD"T"HH24:MI:SS'),
    updated_at      TEXT    NOT NULL DEFAULT to_char((now() at time zone 'utc'), 'YYYY-MM-DD"T"HH24:MI:SS'),
    next_run_at     TEXT    NOT NULL DEFAULT to_char((now() at time zone 'utc'), 'YYYY-MM-DD"T"HH24:MI:SS'),
    last_run_at     TEXT,
    last_run_status TEXT    NOT NULL DEFAULT '',
    last_run_error  TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_due
    ON scheduled_tasks(enabled, next_run_at);

CREATE TABLE IF NOT EXISTS scheduled_task_runs (
    id            BIGSERIAL PRIMARY KEY,
    schedule_id   BIGINT  NOT NULL REFERENCES scheduled_tasks(id) ON DELETE CASCADE,
    started_at    TEXT    NOT NULL,
    finished_at   TEXT    NOT NULL,
    status        TEXT    NOT NULL,
    status_code   INTEGER,
    duration_ms   INTEGER NOT NULL DEFAULT 0,
    error         TEXT    NOT NULL DEFAULT '',
    response_json TEXT    NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_scheduled_task_runs_schedule_started
    ON scheduled_task_runs(schedule_id, started_at DESC);

CREATE TABLE IF NOT EXISTS oauth_connections (
    id              BIGSERIAL PRIMARY KEY,
    user_id         BIGINT  NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider        TEXT    NOT NULL,
    account_id      TEXT    NOT NULL DEFAULT 'default',
    display_name    TEXT    NOT NULL DEFAULT '',
    scope           TEXT    NOT NULL DEFAULT '',
    token_cache_enc TEXT    NOT NULL,
    expires_at      TEXT,
    created_at      TEXT    NOT NULL DEFAULT to_char((now() at time zone 'utc'), 'YYYY-MM-DD"T"HH24:MI:SS'),
    updated_at      TEXT    NOT NULL DEFAULT to_char((now() at time zone 'utc'), 'YYYY-MM-DD"T"HH24:MI:SS'),
    is_active       INTEGER NOT NULL DEFAULT 1,
    UNIQUE(user_id, provider, account_id)
);

CREATE INDEX IF NOT EXISTS idx_oauth_connections_user_provider
    ON oauth_connections(user_id, provider)
    WHERE is_active = 1;

CREATE TABLE IF NOT EXISTS external_ticket_cache (
    cache_key    TEXT PRIMARY KEY,
    provider     TEXT NOT NULL,
    item_type    TEXT NOT NULL,
    organization TEXT NOT NULL DEFAULT '',
    project      TEXT NOT NULL DEFAULT '',
    repository   TEXT NOT NULL DEFAULT '',
    item_id      TEXT NOT NULL,
    details_json TEXT NOT NULL,
    fetched_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_external_ticket_cache_expires
    ON external_ticket_cache(provider, expires_at);

CREATE TABLE IF NOT EXISTS audit_logs (
    id                BIGSERIAL PRIMARY KEY,
    created_at        TEXT    NOT NULL DEFAULT to_char((now() at time zone 'utc'), 'YYYY-MM-DD"T"HH24:MI:SS'),
    scope             TEXT    NOT NULL,
    method            TEXT    NOT NULL,
    path              TEXT    NOT NULL,
    status_code       INTEGER NOT NULL,
    duration_ms       INTEGER NOT NULL,
    actor_type        TEXT    NOT NULL DEFAULT 'anonymous',
    actor_id          BIGINT,
    actor_name        TEXT,
    project_id        BIGINT,
    project_name      TEXT,
    token_id          BIGINT,
    client_ip         TEXT,
    user_agent        TEXT,
    query_string      TEXT,
    request_body      TEXT,
    response_error    TEXT,
    details_json      TEXT,
    token_usage_total BIGINT
);

CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at ON audit_logs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_logs_scope       ON audit_logs(scope);
CREATE INDEX IF NOT EXISTS idx_audit_logs_project_id  ON audit_logs(project_id);
CREATE INDEX IF NOT EXISTS idx_audit_logs_actor_name  ON audit_logs(actor_name);
"""


async def init_db(dsn: str | None = None) -> None:
    """Ensure tables exist.  Idempotent and safe to call on every startup."""
    pool = await init_pool(dsn)
    async with pool.acquire() as db:
        await db.executescript(_CREATE_TABLES)
        await _ensure_scheduled_task_ids(db)
        # Best-effort legacy role rename.
        try:
            await db.execute("UPDATE users SET role = 'developer' WHERE role = 'viewer'")
        except Exception:
            pass


def _random_scheduled_task_id(length: int = 8) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


async def _ensure_scheduled_task_ids(db: Connection) -> None:
    try:
        await db.execute("ALTER TABLE scheduled_tasks ADD COLUMN IF NOT EXISTS task_id TEXT")
        async with db.execute(
            "SELECT id FROM scheduled_tasks WHERE task_id IS NULL OR task_id = '' OR length(task_id) <> 8 ORDER BY id"
        ) as cur:
            rows = await cur.fetchall()
        for row in rows:
            for _ in range(20):
                candidate = _random_scheduled_task_id()
                async with db.execute("SELECT 1 FROM scheduled_tasks WHERE task_id = ?", (candidate,)) as cur:
                    if await cur.fetchone():
                        continue
                await db.execute("UPDATE scheduled_tasks SET task_id = ? WHERE id = ?", (candidate, row["id"]))
                break
        await db.execute("ALTER TABLE scheduled_tasks ALTER COLUMN task_id SET NOT NULL")
        await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_scheduled_tasks_task_id ON scheduled_tasks(task_id)")
        await db.commit()
    except Exception:
        pass


async def get_db() -> AsyncGenerator[Connection, None]:
    """FastAPI dependency yielding a shimmed connection from the pool."""
    pool = get_pool()
    async with pool.acquire() as db:
        yield db


def _truncate_text(value: str | None, max_len: int = 2000) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= max_len else (text[: max_len - 1] + "…")


async def insert_audit_log(
    *,
    scope: str,
    method: str,
    path: str,
    status_code: int,
    duration_ms: int,
    actor_type: str = "anonymous",
    actor_id: int | None = None,
    actor_name: str | None = None,
    project_id: int | None = None,
    project_name: str | None = None,
    token_id: int | None = None,
    client_ip: str | None = None,
    user_agent: str | None = None,
    query_string: str | None = None,
    request_body: str | None = None,
    response_error: str | None = None,
    details: dict | None = None,
    token_usage_total: int | None = None,
) -> None:
    """Write a normalized audit row for admin troubleshooting and compliance."""
    created_at = datetime.now(timezone.utc).isoformat()
    details_json = json.dumps(details, ensure_ascii=True) if details else None
    pool = get_pool()
    async with pool.acquire() as db:
        await db.execute(
            """
            INSERT INTO audit_logs(
                created_at, scope, method, path, status_code, duration_ms,
                actor_type, actor_id, actor_name,
                project_id, project_name, token_id,
                client_ip, user_agent, query_string, request_body, response_error, details_json, token_usage_total
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                created_at,
                scope,
                method,
                path,
                status_code,
                max(0, int(duration_ms)),
                actor_type,
                actor_id,
                _truncate_text(actor_name, 128),
                project_id,
                _truncate_text(project_name, 128),
                token_id,
                _truncate_text(client_ip, 128),
                _truncate_text(user_agent, 512),
                _truncate_text(query_string, 512),
                _truncate_text(request_body, 2000),
                _truncate_text(response_error, 1000),
                _truncate_text(details_json, 4000),
                int(token_usage_total) if token_usage_total is not None else None,
            ),
        )
