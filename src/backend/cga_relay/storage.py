"""Durable, project-isolated relay batches; storing a batch never indexes it.

The complete canonical JSON is retained for recovery, including tombstones and
client metadata. Receipts identify content, not a client's supplied batch ID.
No payload path is ever resolved or written to the server filesystem.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal, TypedDict

from fastapi import HTTPException

from backend.auth.pgshim import Connection

MAX_SYNC_ITEMS = 500
MAX_SYNC_BYTES = 8 * 1024 * 1024


class SyncSnapshot(TypedDict):
    path: str
    content: str
    bytes: int
    sha256: str


class SyncPayload(TypedDict, total=False):
    """Known wire fields; additional JSON metadata is preserved at runtime."""

    agent_id: str
    project_id: str
    namespace: str | None
    project_tag: str | None
    root: str | None
    counts: dict[str, Any]
    snapshots: list[SyncSnapshot]
    tombstones: list[str]


class SyncReceipt(TypedDict):
    accepted: Literal[True]
    durable: Literal[True]
    batch_id: str


class SyncBatchMetadata(TypedDict):
    id: int
    project_db_id: int
    batch_id: str
    created_at: str


class StoredSyncBatch(SyncBatchMetadata):
    payload: dict[str, Any]


def _validate_project_id(project_db_id: int) -> None:
    if type(project_db_id) is not int or project_db_id <= 0:
        raise HTTPException(400, "A positive project database ID is required")


def _relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise HTTPException(400, "Sync paths must be nonempty relative paths")
    parts = value.replace("\\", "/").split("/")
    if (
        ":" in value
        or any(ord(char) < 32 for char in value)
        or any(part in ("", ".", "..") or part.endswith((" ", ".")) for part in parts)
    ):
        raise HTTPException(400, "Sync paths must be relative and cannot traverse directories")
    return "/".join(parts)


def _canonical_payload(payload: dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        raise HTTPException(400, "Sync payload must be an object")
    snapshots = payload.get("snapshots", [])
    tombstones = payload.get("tombstones", [])
    if not isinstance(snapshots, list) or not isinstance(tombstones, list):
        raise HTTPException(400, "Sync snapshots and tombstones must be arrays")
    if len(snapshots) + len(tombstones) > MAX_SYNC_ITEMS:
        raise HTTPException(413, "Sync batch exceeds 500 combined items")

    try:
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        size = len(canonical.encode("utf-8"))
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise HTTPException(400, "Sync payload must contain valid UTF-8 JSON") from exc
    if size > MAX_SYNC_BYTES:
        raise HTTPException(413, "Sync batch exceeds 8 MiB")

    seen: set[str] = set()
    for snapshot in snapshots:
        if not isinstance(snapshot, dict):
            raise HTTPException(400, "Each sync snapshot must be an object")
        path = _relative_path(snapshot.get("path"))
        if path in seen:
            raise HTTPException(409, "Sync batch contains conflicting paths")
        seen.add(path)
        content = snapshot.get("content")
        byte_count = snapshot.get("bytes")
        digest = snapshot.get("sha256")
        if not isinstance(content, str):
            raise HTTPException(400, "Sync snapshot content must be UTF-8 text")
        encoded = content.encode("utf-8")
        if type(byte_count) is not int or byte_count != len(encoded):
            raise HTTPException(400, "Sync snapshot bytes does not match UTF-8 content")
        if (
            not isinstance(digest, str)
            or re.fullmatch(r"[a-fA-F0-9]{64}", digest) is None
            or hashlib.sha256(encoded).hexdigest() != digest.lower()
        ):
            raise HTTPException(400, "Sync snapshot sha256 does not match content")
    for tombstone in tombstones:
        path = _relative_path(tombstone)
        if path in seen:
            raise HTTPException(409, "Sync batch contains conflicting paths")
        seen.add(path)
    return canonical


async def save_sync_batch(
    db: Connection, project_db_id: int, payload: dict[str, Any],
) -> SyncReceipt:
    """Validate and commit a complete batch before issuing a durable receipt.

    HTTPException codes: 400 invalid payload, 413 capacity, 409 content conflict.
    All database/commit errors propagate. Call with get_db outside an existing
    transaction: releasing a nested savepoint would not be a durable commit.
    The module bounds canonical JSON size; HTTP routes must additionally bound
    raw request bytes (including whitespace) before parsing.
    """
    _validate_project_id(project_db_id)
    canonical = _canonical_payload(payload)
    batch_id = hashlib.sha256(
        f"{project_db_id}\n{canonical}".encode("utf-8")
    ).hexdigest()
    if db.raw.is_in_transaction():
        raise RuntimeError("Durable sync requires its own committed transaction")

    async with db.raw.transaction(isolation="read_committed"):
        # Override asynchronous commit so a receipt waits for PostgreSQL WAL.
        await db.execute("SET LOCAL synchronous_commit = on")
        # Assign replay IDs in project commit order, so pagination never skips
        # a delayed lower-ID commit after observing a higher committed ID.
        await db.execute(
            "SELECT id FROM projects WHERE id = ? FOR NO KEY UPDATE", (project_db_id,),
        )
        async with db.execute(
            """INSERT INTO relay_sync_batches(project_db_id, batch_id, payload_json)
               VALUES (?, ?, ?)
               ON CONFLICT (project_db_id, batch_id) DO NOTHING
               RETURNING payload_json""",
            (project_db_id, batch_id, canonical),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            async with db.execute(
                """SELECT payload_json FROM relay_sync_batches
                   WHERE project_db_id = ? AND batch_id = ? FOR SHARE""",
                (project_db_id, batch_id),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("Sync batch disappeared before confirmation")
        if row["payload_json"] != canonical:
            raise HTTPException(409, "Sync batch ID conflicts with stored content")

    return {"accepted": True, "durable": True, "batch_id": batch_id}


async def load_sync_batch(
    db: Connection, project_db_id: int, batch_id: str,
) -> StoredSyncBatch | None:
    """Read a full stored batch without indexing, deleting, or acknowledging it.

    The caller must authorize project_db_id. Cross-project and missing receipts
    return None; malformed IDs raise HTTPException(400). Database failures
    propagate, and corrupt stored payloads raise RuntimeError rather than
    returning data that no longer matches its receipt.
    """
    _validate_project_id(project_db_id)
    if not isinstance(batch_id, str) or re.fullmatch(r"[a-f0-9]{64}", batch_id) is None:
        raise HTTPException(400, "Sync batch ID must be 64 lowercase hexadecimal characters")
    async with db.execute(
        """SELECT id, project_db_id, batch_id, created_at, payload_json
           FROM relay_sync_batches WHERE project_db_id = ? AND batch_id = ?""",
        (project_db_id, batch_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    canonical = row["payload_json"]
    if hashlib.sha256(f"{project_db_id}\n{canonical}".encode("utf-8")).hexdigest() != batch_id:
        raise RuntimeError("Stored sync payload does not match its receipt")
    try:
        payload = json.loads(canonical)
    except (TypeError, ValueError, RecursionError) as exc:
        raise RuntimeError("Stored sync payload is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Stored sync payload must be an object")
    return {
        "id": row["id"], "project_db_id": row["project_db_id"],
        "batch_id": row["batch_id"], "created_at": row["created_at"], "payload": payload,
    }


async def list_sync_batches(
    db: Connection, project_db_id: int, *, after_id: int = 0, limit: int = 100,
) -> list[SyncBatchMetadata]:
    """List project-scoped replay metadata in ascending ID order, without content.

    Pass the last returned ID as after_id. No records are consumed or indexed.
    Pagination errors raise HTTPException(400); database failures propagate.
    """
    _validate_project_id(project_db_id)
    if type(after_id) is not int or after_id < 0:
        raise HTTPException(400, "Sync replay after_id must be a nonnegative integer")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise HTTPException(400, "Sync replay limit must be between 1 and 100")
    async with db.execute(
        """SELECT id, project_db_id, batch_id, created_at FROM relay_sync_batches
           WHERE project_db_id = ? AND id > ? ORDER BY id LIMIT ?""",
        (project_db_id, after_id, limit),
    ) as cursor:
        rows = await cursor.fetchall()
    return [{
        "id": row["id"], "project_db_id": row["project_db_id"],
        "batch_id": row["batch_id"], "created_at": row["created_at"],
    } for row in rows]


get_sync_batch = load_sync_batch
