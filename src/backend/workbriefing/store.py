from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

import asyncpg
import structlog

from backend.workbriefing.models import WorkActivity


DEFAULT_DSN_ENV = "WORKBRIEFING_POSTGRES_DSN"
DEFAULT_DSN = "postgresql://app:app@localhost:15432/appdb"


log = structlog.get_logger()

_ACTIVITY_COLUMNS = (
    "activity_id, plugin_id, source_system, source_type, source_item_id, project_id, workspace_name, "
    "event_type, title, summary, body_text, status, priority, owner, source_url, tags, occurred_at, "
    "synced_at, content_hash, raw_metadata, embedding_text"
)


@dataclass(frozen=True)
class ActivityUpsertResult:
    operation: str
    activity: WorkActivity


@dataclass(frozen=True)
class ActivitySearchResult:
    activities: list[WorkActivity]
    mode: str
    fts_available: bool


class ActivityStore(Protocol):
    async def upsert(self, activity: WorkActivity) -> ActivityUpsertResult:
        ...

    async def list_recent(self, project_id: str | None = None, limit: int = 25) -> list[WorkActivity]:
        ...

    async def count_recent(self, project_id: str | None = None) -> int:
        ...

    async def find_by_content_hash(
        self,
        *,
        plugin_id: str,
        project_id: str,
        source_type: str,
        content_hash: str,
    ) -> WorkActivity | None:
        ...

    async def cleanup_exact_duplicates(self, project_id: str | None = None, dry_run: bool = True) -> dict[str, Any]:
        ...

    async def search_activities(self, *, project_id: str, query: str, limit: int = 25) -> ActivitySearchResult:
        ...


def resolve_dsn(dsn: str | None = None) -> str:
    if dsn:
        return dsn
    env_dsn = os.getenv(DEFAULT_DSN_ENV)
    if env_dsn:
        return env_dsn
    return DEFAULT_DSN


class PgVectorActivityStore:
    """Postgres + pgvector backed store for WorkBriefing activities."""

    def __init__(
        self,
        dsn: str | None = None,
        *,
        schema: str = "public",
        min_pool_size: int = 1,
        max_pool_size: int = 5,
    ) -> None:
        self._dsn = resolve_dsn(dsn)
        if not schema.replace("_", "").isalnum():
            raise ValueError("schema must be alphanumeric/underscore only")
        self._schema = schema
        self._min_pool_size = min_pool_size
        self._max_pool_size = max_pool_size
        self._pool: asyncpg.Pool | None = None
        self._pool_lock = asyncio.Lock()
        self._schema_ready = False
        self._schema_lock = asyncio.Lock()

    @property
    def schema(self) -> str:
        return self._schema

    @property
    def dsn(self) -> str:
        return self._dsn

    async def _get_pool(self) -> asyncpg.Pool:
        if self._pool is not None:
            return self._pool
        async with self._pool_lock:
            if self._pool is None:
                self._pool = await asyncpg.create_pool(
                    dsn=self._dsn,
                    min_size=self._min_pool_size,
                    max_size=self._max_pool_size,
                )
        return self._pool

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            self._schema_ready = False

    async def ensure_schema(self) -> None:
        if self._schema_ready:
            return
        async with self._schema_lock:
            if self._schema_ready:
                return
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                await conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
                await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{self._schema}";')
                await conn.execute(self._create_table_sql())
                await conn.execute(self._create_unique_index_sql())
                await conn.execute(self._create_project_time_index_sql())
                await conn.execute(self._create_time_index_sql())
            self._schema_ready = True

    def _qualified(self, name: str) -> str:
        return f'"{self._schema}"."{name}"'

    def _create_table_sql(self) -> str:
        return f"""
        CREATE TABLE IF NOT EXISTS {self._qualified("work_activities")} (
            activity_id       text PRIMARY KEY,
            plugin_id         text NOT NULL,
            source_system     text NOT NULL,
            source_type       text NOT NULL,
            source_item_id    text NOT NULL,
            project_id        text NOT NULL,
            workspace_name    text,
            event_type        text NOT NULL,
            title             text NOT NULL,
            summary           text NOT NULL DEFAULT '',
            body_text         text NOT NULL DEFAULT '',
            status            text,
            priority          text,
            owner             text,
            source_url        text,
            tags              text[] NOT NULL DEFAULT ARRAY[]::text[],
            occurred_at       timestamptz NOT NULL,
            synced_at         timestamptz NOT NULL,
            content_hash      text NOT NULL,
            raw_metadata      jsonb NOT NULL DEFAULT '{{}}'::jsonb,
            embedding_text    text NOT NULL DEFAULT '',
            embedding         vector
        );
        """

    def _create_unique_index_sql(self) -> str:
        idx_name = f"idx_{self._schema}_work_activities_identity"
        return (
            f'CREATE UNIQUE INDEX IF NOT EXISTS "{idx_name}" '
            f'ON {self._qualified("work_activities")}'
            "(plugin_id, project_id, source_type, source_item_id);"
        )

    def _create_project_time_index_sql(self) -> str:
        idx_name = f"idx_{self._schema}_work_activities_project_time"
        return (
            f'CREATE INDEX IF NOT EXISTS "{idx_name}" '
            f'ON {self._qualified("work_activities")}'
            "(project_id, occurred_at DESC, synced_at DESC);"
        )

    def _create_time_index_sql(self) -> str:
        idx_name = f"idx_{self._schema}_work_activities_time"
        return (
            f'CREATE INDEX IF NOT EXISTS "{idx_name}" '
            f'ON {self._qualified("work_activities")}'
            "(occurred_at DESC, synced_at DESC);"
        )

    async def upsert(self, activity: WorkActivity) -> ActivityUpsertResult:
        await self.ensure_schema()
        pool = await self._get_pool()
        sql = f"""
        INSERT INTO {self._qualified("work_activities")} (
            activity_id,
            plugin_id,
            source_system,
            source_type,
            source_item_id,
            project_id,
            workspace_name,
            event_type,
            title,
            summary,
            body_text,
            status,
            priority,
            owner,
            source_url,
            tags,
            occurred_at,
            synced_at,
            content_hash,
            raw_metadata,
            embedding_text
        ) VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
            $11, $12, $13, $14, $15, $16, $17, $18, $19, $20::jsonb, $21
        )
        ON CONFLICT (plugin_id, project_id, source_type, source_item_id)
        DO UPDATE SET
            source_system  = EXCLUDED.source_system,
            workspace_name = EXCLUDED.workspace_name,
            event_type     = EXCLUDED.event_type,
            title          = EXCLUDED.title,
            summary        = EXCLUDED.summary,
            body_text      = EXCLUDED.body_text,
            status         = EXCLUDED.status,
            priority       = EXCLUDED.priority,
            owner          = EXCLUDED.owner,
            source_url     = EXCLUDED.source_url,
            tags           = EXCLUDED.tags,
            occurred_at    = EXCLUDED.occurred_at,
            synced_at      = EXCLUDED.synced_at,
            content_hash   = EXCLUDED.content_hash,
            raw_metadata   = EXCLUDED.raw_metadata,
            embedding_text = EXCLUDED.embedding_text
        RETURNING activity_id, (xmax = 0) AS inserted;
        """

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                sql,
                activity.activity_id,
                activity.plugin_id,
                activity.source_system,
                activity.source_type,
                activity.source_item_id,
                activity.project_id,
                activity.workspace_name,
                activity.event_type,
                activity.title,
                activity.summary,
                activity.body_text,
                activity.status,
                activity.priority,
                activity.owner,
                activity.source_url,
                list(activity.tags),
                activity.occurred_at,
                activity.synced_at,
                activity.content_hash,
                json.dumps(activity.raw_metadata, sort_keys=True, ensure_ascii=True),
                activity.embedding_text,
            )

        operation = "created" if row["inserted"] else "updated"
        persisted_id = str(row["activity_id"])
        if persisted_id != activity.activity_id:
            persisted = WorkActivity(
                activity_id=persisted_id,
                plugin_id=activity.plugin_id,
                source_system=activity.source_system,
                source_type=activity.source_type,
                source_item_id=activity.source_item_id,
                project_id=activity.project_id,
                workspace_name=activity.workspace_name,
                event_type=activity.event_type,
                title=activity.title,
                summary=activity.summary,
                body_text=activity.body_text,
                status=activity.status,
                priority=activity.priority,
                owner=activity.owner,
                source_url=activity.source_url,
                tags=activity.tags,
                occurred_at=activity.occurred_at,
                synced_at=activity.synced_at,
                content_hash=activity.content_hash,
                raw_metadata=activity.raw_metadata,
                embedding_text=activity.embedding_text,
            )
        else:
            persisted = activity
        return ActivityUpsertResult(operation=operation, activity=persisted)

    async def list_recent(
        self,
        project_id: str | None = None,
        limit: int = 25,
    ) -> list[WorkActivity]:
        await self.ensure_schema()
        pool = await self._get_pool()
        safe_limit = max(1, min(limit, 5000))

        base_sql = f"""
        SELECT
            activity_id,
            plugin_id,
            source_system,
            source_type,
            source_item_id,
            project_id,
            workspace_name,
            event_type,
            title,
            summary,
            body_text,
            status,
            priority,
            owner,
            source_url,
            tags,
            occurred_at,
            synced_at,
            content_hash,
            raw_metadata,
            embedding_text
        FROM {self._qualified("work_activities")}
        """

        if project_id:
            sql = base_sql + " WHERE project_id = $1 ORDER BY occurred_at DESC, synced_at DESC, source_item_id DESC LIMIT $2"
            params: tuple[Any, ...] = (project_id, safe_limit)
        else:
            sql = base_sql + " ORDER BY occurred_at DESC, synced_at DESC, source_item_id DESC LIMIT $1"
            params = (safe_limit,)

        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)

        return [self._activity_from_row(row) for row in rows]

    async def count_recent(self, project_id: str | None = None) -> int:
        await self.ensure_schema()
        pool = await self._get_pool()

        if project_id:
            sql = f"SELECT COUNT(*) FROM {self._qualified('work_activities')} WHERE project_id = $1"
            params: tuple[Any, ...] = (project_id,)
        else:
            sql = f"SELECT COUNT(*) FROM {self._qualified('work_activities')}"
            params = ()

        async with pool.acquire() as conn:
            value = await conn.fetchval(sql, *params)

        return int(value or 0)

    async def find_by_content_hash(
        self,
        *,
        plugin_id: str,
        project_id: str,
        source_type: str,
        content_hash: str,
    ) -> WorkActivity | None:
        await self.ensure_schema()
        pool = await self._get_pool()
        sql = f"""
        SELECT
            activity_id,
            plugin_id,
            source_system,
            source_type,
            source_item_id,
            project_id,
            workspace_name,
            event_type,
            title,
            summary,
            body_text,
            status,
            priority,
            owner,
            source_url,
            tags,
            occurred_at,
            synced_at,
            content_hash,
            raw_metadata,
            embedding_text
        FROM {self._qualified("work_activities")}
        WHERE plugin_id = $1 AND project_id = $2 AND source_type = $3 AND content_hash = $4
        ORDER BY synced_at DESC, occurred_at DESC
        LIMIT 1
        """

        async with pool.acquire() as conn:
            row = await conn.fetchrow(sql, plugin_id, project_id, source_type, content_hash)

        if not row:
            return None
        return self._activity_from_row(row)

    async def cleanup_exact_duplicates(self, project_id: str | None = None, dry_run: bool = True) -> dict[str, Any]:
        await self.ensure_schema()
        pool = await self._get_pool()
        base_where = "WHERE project_id = $1" if project_id else ""
        params: tuple[Any, ...] = (project_id,) if project_id else ()
        duplicate_sql = f"""
        WITH ranked AS (
            SELECT
                activity_id,
                project_id,
                content_hash,
                source_item_id,
                occurred_at,
                synced_at,
                ROW_NUMBER() OVER (
                    PARTITION BY plugin_id, project_id, source_type, content_hash
                    ORDER BY synced_at DESC, occurred_at DESC, activity_id DESC
                ) AS rn,
                COUNT(*) OVER (
                    PARTITION BY plugin_id, project_id, source_type, content_hash
                ) AS grp_count
            FROM {self._qualified("work_activities")}
            {base_where}
        )
        SELECT activity_id, project_id, content_hash, source_item_id, occurred_at, synced_at, grp_count
        FROM ranked
        WHERE grp_count > 1 AND rn > 1
        ORDER BY synced_at DESC, occurred_at DESC
        """

        async with pool.acquire() as conn:
            rows = await conn.fetch(duplicate_sql, *params)
            deleted_ids = [str(row["activity_id"]) for row in rows]
            if deleted_ids and not dry_run:
                await conn.execute(
                    f'DELETE FROM {self._qualified("work_activities")} WHERE activity_id = ANY($1::text[])',
                    deleted_ids,
                )

        return {
            "project_id": project_id,
            "dry_run": dry_run,
            "duplicate_count": len(deleted_ids),
            "deleted_activity_ids": deleted_ids,
        }

    async def search_activities(self, *, project_id: str, query: str, limit: int = 25) -> ActivitySearchResult:
        """Rank project activity by full-text match on title, summary and tags; fall back to substring match."""
        await self.ensure_schema()
        safe_limit = max(1, min(int(limit), 100))
        try:
            activities = await self._search_full_text(project_id, query, safe_limit)
            return ActivitySearchResult(activities=activities, mode="full_text", fts_available=True)
        except asyncpg.PostgresError as exc:
            log.warning("work_activity_full_text_unavailable", error_type=type(exc).__name__)
        activities = await self._search_lexical(project_id, query, safe_limit)
        return ActivitySearchResult(activities=activities, mode="lexical_fallback", fts_available=False)

    def _search_document_sql(self) -> str:
        return "coalesce(title, '') || ' ' || coalesce(summary, '') || ' ' || array_to_string(tags, ' ')"

    async def _search_full_text(self, project_id: str, query: str, limit: int) -> list[WorkActivity]:
        pool = await self._get_pool()
        document = f"to_tsvector('simple', {self._search_document_sql()})"
        sql = f"""
        SELECT {_ACTIVITY_COLUMNS}
        FROM {self._qualified("work_activities")}
        WHERE project_id = $1 AND {document} @@ websearch_to_tsquery('simple', $2)
        ORDER BY ts_rank({document}, websearch_to_tsquery('simple', $2)) DESC,
                 occurred_at DESC, source_item_id DESC
        LIMIT $3
        """
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, project_id, query, limit)
        return [self._activity_from_row(row) for row in rows]

    async def _search_lexical(self, project_id: str, query: str, limit: int) -> list[WorkActivity]:
        pool = await self._get_pool()
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        sql = f"""
        SELECT {_ACTIVITY_COLUMNS}
        FROM {self._qualified("work_activities")}
        WHERE project_id = $1 AND ({self._search_document_sql()}) ILIKE $2 ESCAPE '\\'
        ORDER BY occurred_at DESC, source_item_id DESC
        LIMIT $3
        """
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, project_id, f"%{escaped}%", limit)
        return [self._activity_from_row(row) for row in rows]

    @staticmethod
    def _activity_from_row(row: asyncpg.Record) -> WorkActivity:
        raw_metadata = row["raw_metadata"]
        if isinstance(raw_metadata, str):
            raw_metadata = json.loads(raw_metadata or "{}")
        elif raw_metadata is None:
            raw_metadata = {}
        occurred_at = _ensure_utc(row["occurred_at"])
        synced_at = _ensure_utc(row["synced_at"])
        return WorkActivity(
            activity_id=str(row["activity_id"]),
            plugin_id=str(row["plugin_id"]),
            source_system=str(row["source_system"]),
            source_type=str(row["source_type"]),
            source_item_id=str(row["source_item_id"]),
            project_id=str(row["project_id"]),
            workspace_name=row["workspace_name"],
            event_type=str(row["event_type"]),
            title=str(row["title"]),
            summary=str(row["summary"] or ""),
            body_text=str(row["body_text"] or ""),
            status=row["status"],
            priority=row["priority"],
            owner=row["owner"],
            source_url=row["source_url"],
            tags=tuple(row["tags"] or ()),
            occurred_at=occurred_at,
            synced_at=synced_at,
            content_hash=str(row["content_hash"]),
            raw_metadata=raw_metadata,
            embedding_text=str(row["embedding_text"] or ""),
        )


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
