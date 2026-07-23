"""Persistence and execution service for CGA extensions."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend import runtime_config
from backend.auth.pgshim import Connection
from backend.extensions.models import (
    ExtensionConfigOut,
    ExtensionConfigUpdate,
    ExtensionDefinition,
    ExtensionList,
    ExtensionProjectOverview,
    ExtensionRunOut,
)
from backend.extensions.registry import (
    deliver_extension_notifications,
    get_extension_definition,
    get_extension_snapshot_scope,
    list_extension_definitions,
    run_extension,
    validate_extension_config,
)


MAX_SNAPSHOT_BYTES = 50 * 1024 * 1024


class ExtensionNotFoundError(KeyError):
    pass


class ExtensionProjectNotFoundError(KeyError):
    pass


def iso_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _decode_json(value: str | None, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
    if not value:
        return dict(fallback or {})
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else dict(fallback or {})
    except Exception:
        return dict(fallback or {})


def _encode_json(value: dict[str, Any] | None) -> str:
    return json.dumps(value or {}, sort_keys=True, ensure_ascii=True)


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except Exception:
        return default


async def _load_latest_snapshot(
    db: Connection,
    extension_id: str,
    project_id: int | None,
    scope_key: str,
) -> dict[str, Any] | None:
    if project_id is None:
        sql = """
            SELECT snapshot_json FROM extension_snapshots
            WHERE extension_id = ? AND project_id IS NULL AND scope_key = ?
            ORDER BY captured_at DESC, id DESC LIMIT 1
        """
        params = (extension_id, scope_key)
    else:
        sql = """
            SELECT snapshot_json FROM extension_snapshots
            WHERE extension_id = ? AND project_id = ? AND scope_key = ?
            ORDER BY captured_at DESC, id DESC LIMIT 1
        """
        params = (extension_id, project_id, scope_key)
    async with db.execute(sql, params) as cur:
        row = await cur.fetchone()
    return _decode_json(row["snapshot_json"]) if row else None


async def _save_snapshot(
    db: Connection,
    *,
    extension_id: str,
    project_id: int | None,
    run_id: int,
    scope_key: str,
    snapshot: dict[str, Any],
    retention_count: int,
) -> None:
    encoded = _encode_json(snapshot)
    if len(encoded.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
        raise ValueError("Extension snapshot exceeds the 50 MiB persistence limit.")
    captured_at = str(snapshot.get("captured_at") or iso_utc())
    created_at = iso_utc()
    await db.execute(
        """
        INSERT INTO extension_snapshots(
            extension_id, project_id, run_id, scope_key, captured_at, snapshot_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (extension_id, project_id, run_id, scope_key, captured_at, encoded, created_at),
    )
    keep = max(2, min(1000, int(retention_count)))
    if project_id is None:
        await db.execute(
            """
            DELETE FROM extension_snapshots WHERE id IN (
                SELECT id FROM extension_snapshots
                WHERE extension_id = ? AND project_id IS NULL AND scope_key = ?
                ORDER BY captured_at DESC, id DESC OFFSET ?
            )
            """,
            (extension_id, scope_key, keep),
        )
    else:
        await db.execute(
            """
            DELETE FROM extension_snapshots WHERE id IN (
                SELECT id FROM extension_snapshots
                WHERE extension_id = ? AND project_id = ? AND scope_key = ?
                ORDER BY captured_at DESC, id DESC OFFSET ?
            )
            """,
            (extension_id, project_id, scope_key, keep),
        )


def _merge_config(definition: ExtensionDefinition, project: dict[str, Any] | None, stored: dict[str, Any], override: dict[str, Any] | None = None) -> dict[str, Any]:
    merged = dict(definition.default_config)
    merged.update(stored or {})
    merged.update(override or {})
    if not str(merged.get("repo_path") or "").strip():
        merged["repo_path"] = _default_repo_path(project)
    validate_extension_config(definition.extension_id, merged, require_complete=True)
    return merged


def _default_repo_path(project: dict[str, Any] | None) -> str:
    if not project:
        return ""
    explicit = str(project.get("repo_path") or "").strip()
    if explicit:
        return explicit
    repos_mount = Path("/repos")
    if repos_mount.exists():
        for key in ("project_name", "project_id"):
            value = str(project.get(key) or "").strip()
            if value:
                candidate = repos_mount / value
                if candidate.exists():
                    return str(candidate)
    return ""


def _config_from_row(row: Any, project: dict[str, Any] | None = None) -> ExtensionConfigOut:
    project_id = row["project_id"]
    config = _decode_json(row["config_json"])
    validate_extension_config(row["extension_id"], config)
    return ExtensionConfigOut(
        extension_id=row["extension_id"],
        project_id=int(project_id) if project_id is not None else None,
        project_name=(project or {}).get("project_name") or _row_get(row, "project_name"),
        project_external_id=(project or {}).get("project_id") or _row_get(row, "project_external_id"),
        enabled=bool(row["enabled"]),
        config=config,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _run_from_row(row: Any) -> ExtensionRunOut:
    return ExtensionRunOut(
        id=int(row["id"]),
        extension_id=row["extension_id"],
        project_id=_row_get(row, "project_id"),
        project_name=_row_get(row, "project_name"),
        project_external_id=_row_get(row, "project_external_id"),
        schedule_id=row["schedule_id"],
        status=row["status"],
        severity=row["severity"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        duration_ms=int(row["duration_ms"] or 0),
        summary=_decode_json(row["summary_json"]),
        result=_decode_json(row["result_json"]),
    )


async def list_extensions() -> ExtensionList:
    return ExtensionList(items=list_extension_definitions())


async def get_project(db: Connection, project_id: int) -> dict[str, Any]:
    async with db.execute(
        "SELECT id, project_name, project_id, repo_path, upstream_url, is_active FROM projects WHERE id = ?",
        (project_id,),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        raise ExtensionProjectNotFoundError(f"Project not found: {project_id}")
    return dict(row)


async def get_extension_config(db: Connection, extension_id: str, project_id: int | None = None) -> ExtensionConfigOut:
    definition = get_extension_definition(extension_id)
    if not definition:
        raise ExtensionNotFoundError(extension_id)
    project = await get_project(db, project_id) if project_id is not None else None
    if project_id is None:
        async with db.execute(
            """
            SELECT ec.*, p.project_name, p.project_id AS project_external_id
            FROM extension_configs ec
            LEFT JOIN projects p ON p.id = ec.project_id
            WHERE ec.extension_id = ? AND ec.project_id IS NULL
            """,
            (extension_id,),
        ) as cur:
            row = await cur.fetchone()
    else:
        async with db.execute(
            """
            SELECT ec.*, p.project_name, p.project_id AS project_external_id
            FROM extension_configs ec
            LEFT JOIN projects p ON p.id = ec.project_id
            WHERE ec.extension_id = ? AND ec.project_id = ?
            """,
            (extension_id, project_id),
        ) as cur:
            row = await cur.fetchone()
    if row:
        return _config_from_row(row, project)
    return ExtensionConfigOut(
        extension_id=extension_id,
        project_id=project_id,
        project_name=(project or {}).get("project_name"),
        project_external_id=(project or {}).get("project_id"),
        enabled=True,
        config=_merge_config(definition, project, {}),
    )


async def update_extension_config(
    db: Connection,
    extension_id: str,
    project_id: int | None,
    body: ExtensionConfigUpdate,
) -> ExtensionConfigOut:
    definition = get_extension_definition(extension_id)
    if not definition:
        raise ExtensionNotFoundError(extension_id)
    project = await get_project(db, project_id) if project_id is not None else None
    now = iso_utc()
    config_json = _encode_json(_merge_config(definition, project, body.config))
    if project_id is None:
        async with db.execute(
            "SELECT id FROM extension_configs WHERE extension_id = ? AND project_id IS NULL",
            (extension_id,),
        ) as cur:
            existing = await cur.fetchone()
    else:
        async with db.execute(
            "SELECT id FROM extension_configs WHERE extension_id = ? AND project_id = ?",
            (extension_id, project_id),
        ) as cur:
            existing = await cur.fetchone()
    if existing:
        if project_id is None:
            await db.execute(
                """
                UPDATE extension_configs
                SET enabled = ?, config_json = ?, updated_at = ?
                WHERE extension_id = ? AND project_id IS NULL
                """,
                (1 if body.enabled else 0, config_json, now, extension_id),
            )
        else:
            await db.execute(
                """
                UPDATE extension_configs
                SET enabled = ?, config_json = ?, updated_at = ?
                WHERE extension_id = ? AND project_id = ?
                """,
                (1 if body.enabled else 0, config_json, now, extension_id, project_id),
            )
    else:
        await db.execute(
            """
            INSERT INTO extension_configs(extension_id, project_id, enabled, config_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (extension_id, project_id, 1 if body.enabled else 0, config_json, now, now),
        )
    await db.commit()
    return await get_extension_config(db, extension_id, project_id)


async def list_extension_runs(
    db: Connection,
    extension_id: str,
    project_id: int | None = None,
    *,
    limit: int = 20,
) -> list[ExtensionRunOut]:
    definition = get_extension_definition(extension_id)
    if not definition:
        raise ExtensionNotFoundError(extension_id)
    if project_id is None:
        async with db.execute(
            """
            SELECT er.*, p.project_name, p.project_id AS project_external_id
            FROM extension_runs er
            LEFT JOIN projects p ON p.id = er.project_id
            WHERE er.extension_id = ? AND er.project_id IS NULL
            ORDER BY er.started_at DESC, er.id DESC
            LIMIT ?
            """,
            (extension_id, max(1, min(100, int(limit)))),
        ) as cur:
            rows = await cur.fetchall()
    else:
        await get_project(db, project_id)
        async with db.execute(
            """
            SELECT er.*, p.project_name, p.project_id AS project_external_id
            FROM extension_runs er
            LEFT JOIN projects p ON p.id = er.project_id
            WHERE er.extension_id = ? AND er.project_id = ?
            ORDER BY er.started_at DESC, er.id DESC
            LIMIT ?
            """,
            (extension_id, project_id, max(1, min(100, int(limit)))),
        ) as cur:
            rows = await cur.fetchall()
    return [_run_from_row(row) for row in rows]


async def get_extension_overview(
    db: Connection,
    extension_id: str,
    project_id: int | None = None,
) -> ExtensionProjectOverview:
    definition = get_extension_definition(extension_id)
    if not definition:
        raise ExtensionNotFoundError(extension_id)
    config = await get_extension_config(db, extension_id, project_id)
    runs = await list_extension_runs(db, extension_id, project_id, limit=10)
    return ExtensionProjectOverview(
        definition=definition,
        config=config,
        last_run=runs[0] if runs else None,
        recent_runs=runs,
    )


async def get_extension_project_overview(
    db: Connection,
    extension_id: str,
    project_id: int,
) -> ExtensionProjectOverview:
    return await get_extension_overview(db, extension_id, project_id)


async def run_project_extension(
    db: Connection,
    extension_id: str,
    project_id: int | None = None,
    *,
    config_override: dict[str, Any] | None = None,
    schedule_id: int | None = None,
) -> ExtensionRunOut:
    definition = get_extension_definition(extension_id)
    if not definition:
        raise ExtensionNotFoundError(extension_id)
    validate_extension_config(extension_id, config_override or {})
    project = await get_project(db, project_id) if project_id is not None else None
    config = await get_extension_config(db, extension_id, project_id)
    started = iso_utc()
    started_clock = time.perf_counter()
    status = "success"
    severity = "info"
    result: dict[str, Any]
    snapshot: dict[str, Any] | None = None
    snapshot_scope = ""
    effective_config: dict[str, Any] = {}
    try:
        effective_config = _merge_config(definition, project, config.config, config_override)
        configured_scope = get_extension_snapshot_scope(extension_id, effective_config)
        previous_snapshot = (
            await _load_latest_snapshot(db, extension_id, project_id, configured_scope)
            if configured_scope
            else None
        )
        result = await asyncio.to_thread(
            run_extension,
            extension_id,
            effective_config,
            previous_snapshot=previous_snapshot,
        )
        raw_snapshot = result.pop("_snapshot", None)
        snapshot = raw_snapshot if isinstance(raw_snapshot, dict) else None
        snapshot_scope = str(result.pop("_snapshot_scope", "") or configured_scope or "").lower()
        if snapshot and not snapshot_scope:
            raise ValueError("Extension returned a snapshot without a scope key.")
        severity = str(result.get("severity") or "info")
        outputs = result.get("outputs") if isinstance(result.get("outputs"), dict) else {}
        try:
            notifications_enabled = str(effective_config.get("notifications_enabled") or "").lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
            smtp_config = runtime_config.get_smtp_delivery_config() if notifications_enabled else {}
            outputs["notifications"] = await asyncio.to_thread(
                deliver_extension_notifications,
                extension_id,
                result,
                effective_config,
                smtp_config=smtp_config,
            )
        except Exception as exc:
            outputs["notifications"] = {
                "status": "failed",
                "channels": {},
                "error_type": type(exc).__name__,
            }
        result["outputs"] = outputs
    except Exception as exc:
        status = "failed"
        severity = "critical"
        error_type = type(exc).__name__
        result = {
            "extension_id": extension_id,
            "status": "failed",
            "severity": severity,
            "summary": {"error_type": error_type},
            "findings": [
                {
                    "check": "extension_run",
                    "severity": "critical",
                    "message": "Extension execution failed.",
                    "evidence": {"error_type": error_type},
                }
            ],
        }
    finished = iso_utc()
    duration_ms = int((time.perf_counter() - started_clock) * 1000)
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    async with db.execute(
        """
        INSERT INTO extension_runs(
            extension_id, project_id, schedule_id, status, severity, started_at, finished_at,
            duration_ms, summary_json, result_json, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING id
        """,
        (
            extension_id,
            project_id,
            schedule_id,
            status,
            severity,
            started,
            finished,
            duration_ms,
            _encode_json(summary),
            _encode_json(result),
            finished,
        ),
    ) as cur:
        row = await cur.fetchone()
    if status == "success" and snapshot is not None:
        await _save_snapshot(
            db,
            extension_id=extension_id,
            project_id=project_id,
            run_id=int(row["id"]),
            scope_key=snapshot_scope,
            snapshot=snapshot,
            retention_count=int(effective_config.get("snapshot_retention_count") or 90),
        )
    await db.commit()
    async with db.execute(
        """
        SELECT er.*, p.project_name, p.project_id AS project_external_id
        FROM extension_runs er
        LEFT JOIN projects p ON p.id = er.project_id
        WHERE er.id = ?
        """,
        (row["id"],),
    ) as cur:
        saved = await cur.fetchone()
    return _run_from_row(saved)
