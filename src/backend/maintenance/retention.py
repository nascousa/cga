from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone

import jwt
import structlog
from pydantic import BaseModel, ConfigDict, Field

from backend.auth.pgshim import get_pool
from backend.auth.security import JWT_SECRET

log = structlog.get_logger()
LOCK_ID = 718234019
_active_cleanups = 0
POLL_PATHS = (
    "/api/auth/projects/index-status", "/api/auth/audit", "/api/auth/audit/paged",
    "/api/admin/schedules",
)


class RetentionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool = False
    polling_days: int = Field(default=7, ge=1, le=3650)
    request_days: int = Field(default=90, ge=1, le=3650)
    protected_days: int = Field(default=365, ge=90, le=36500)
    batch_size: int = Field(default=1000, ge=100, le=5000)


def _policy_hash(policy: RetentionPolicy) -> str:
    return hashlib.sha256(policy.model_dump_json().encode()).hexdigest()


async def read_state(conn, key: str, default: dict) -> dict:
    row = await conn.fetchrow("SELECT payload_json FROM maintenance_state WHERE key=$1", key)
    return json.loads(row["payload_json"]) if row else default


async def write_state(conn, key: str, value: dict) -> None:
    await conn.execute(
        """INSERT INTO maintenance_state(key,payload_json) VALUES($1,$2)
        ON CONFLICT(key) DO UPDATE SET payload_json=excluded.payload_json""",
        key, json.dumps(value),
    )


async def get_policy(conn) -> RetentionPolicy:
    return RetentionPolicy.model_validate(await read_state(conn, "audit-policy", {}))


def expired_query(policy: RetentionPolicy, now: datetime | None = None) -> tuple[str, list]:
    now = now or datetime.now(timezone.utc)
    # Unknown historical time formats are preserved, not guessed or cast blindly.
    timestamp_pattern = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(\+00:00|Z)?$"
    protected = """(status_code >= 400 OR method NOT IN ('GET','HEAD')
        OR path ~ '/(login|logout|users|tokens|runtime-config|updates|audit-retention)(/|$)')"""
    category = f"""CASE WHEN {protected} THEN 'protected'
        WHEN path = ANY($1::text[]) THEN 'polling' ELSE 'request' END"""
    cutoffs = [
        (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
        for days in (policy.protected_days, policy.polling_days, policy.request_days)
    ]
    predicate = f"""created_at ~ $2 AND created_at < CASE
        WHEN {protected} THEN $3
        WHEN path = ANY($1::text[]) THEN $4 ELSE $5 END"""
    return f"SELECT id, {category} AS category FROM audit_logs WHERE {predicate}", [
        list(POLL_PATHS), timestamp_pattern, *cutoffs,
    ]


async def preview(policy: RetentionPolicy) -> dict:
    sql, args = expired_query(policy)
    async with get_pool().acquire() as db:
        rows = await db.raw.fetch(f"SELECT category,count(*) AS count FROM ({sql}) q GROUP BY category", *args)
    token = jwt.encode({
        "aud": "cga-retention-preview", "policy": _policy_hash(policy),
        "exp": datetime.now(timezone.utc) + timedelta(minutes=15),
    }, JWT_SECRET, algorithm="HS256")
    return {"counts": {row["category"]: row["count"] for row in rows},
            "preview_token": token, "expires_in_seconds": 900}


def validate_preview(policy: RetentionPolicy, token: str) -> None:
    try:
        claims = jwt.decode(token, JWT_SECRET, algorithms=["HS256"], audience="cga-retention-preview",
                            options={"require": ["exp", "aud", "policy"]})
    except jwt.InvalidTokenError as exc:
        raise ValueError("Preview expired or invalid. Preview this policy again.") from exc
    if claims["policy"] != _policy_hash(policy):
        raise ValueError("Policy changed after preview. Preview it again.")


async def status() -> dict:
    async with get_pool().acquire() as db:
        conn = db.raw
        sizes = await conn.fetchrow("""SELECT count(*) AS rows,min(created_at) AS oldest,
            pg_relation_size('audit_logs') AS data_bytes,
            pg_indexes_size('audit_logs') AS index_bytes,
            pg_total_relation_size('audit_logs') AS total_bytes FROM audit_logs""")
        return {"policy": (await get_policy(conn)).model_dump(), "storage": dict(sizes),
                "last_run": await read_state(conn, "audit-run", {}),
                "scheduler_interval_seconds": 3600}


async def cleanup() -> dict:
    global _active_cleanups
    from backend.maintenance.spool import pending

    if pending():
        return {"status": "paused-for-upgrade", "deleted": 0}
    _active_cleanups += 1
    try:
        return await _cleanup()
    finally:
        _active_cleanups -= 1


def active_cleanups() -> int:
    return _active_cleanups


async def _cleanup() -> dict:
    async with get_pool().acquire() as db:
        conn = db.raw
        if not await conn.fetchval("SELECT pg_try_advisory_lock($1)", LOCK_ID):
            return {"status": "busy", "deleted": 0}
        deleted = 0
        try:
            for _ in range(5):
                async with conn.transaction():
                    # The policy writer takes this same lock before disabling cleanup.
                    policy = await get_policy(conn)
                    if not policy.enabled:
                        break
                    await conn.execute("SET LOCAL statement_timeout = '10s'")
                    sql, args = expired_query(policy)
                    rows = await conn.fetch(
                        f"""DELETE FROM audit_logs WHERE id IN
                        (SELECT id FROM ({sql}) eligible ORDER BY id LIMIT $6)
                        RETURNING id""", *args, policy.batch_size,
                    )
                    deleted += len(rows)
                if len(rows) < policy.batch_size:
                    break
                await asyncio.sleep(0)
            result = {"status": "ok", "deleted": deleted,
                      "finished_at": datetime.now(timezone.utc).isoformat()}
            await write_state(conn, "audit-run", result)
            return result
        except Exception as exc:
            log.exception("audit.cleanup_failed", deleted=deleted)
            await write_state(conn, "audit-run", {
                "status": "error", "deleted": deleted, "error": str(exc),
                "finished_at": datetime.now(timezone.utc).isoformat(),
            })
            raise
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", LOCK_ID)


async def save_policy(policy: RetentionPolicy, token: str) -> dict:
    if policy.protected_days < max(policy.polling_days, policy.request_days):
        raise ValueError("Protected events must be retained at least as long as other requests.")
    validate_preview(policy, token)
    async with get_pool().acquire() as db:
        async with db.raw.transaction():
            await db.raw.execute("SELECT pg_advisory_xact_lock($1)", LOCK_ID)
            await write_state(db.raw, "audit-policy", policy.model_dump())
    return policy.model_dump()


async def retention_loop() -> None:
    while True:
        try:
            async with get_pool().acquire() as db:
                policy = await get_policy(db.raw)
                last = await read_state(db.raw, "audit-run", {})
            last_time = datetime.fromisoformat(last["finished_at"]) if last.get("finished_at") else None
            due = last_time is None or datetime.now(timezone.utc) - last_time >= timedelta(hours=1)
            if policy.enabled and due:
                await cleanup()
        except Exception:
            log.exception("audit.retention_worker_failed")
        await asyncio.sleep(60)
