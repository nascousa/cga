from __future__ import annotations

import json
import os
import re
import socket
import stat
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


class QueueLockBusyError(ValueError):
    def __init__(self, details: dict):
        self.details = details
        super().__init__(
            "Update queue is busy. Wait and retry; inspect a stale lock only after stopping its owner. "
            f"No lock was removed. Lock diagnostics: {json.dumps(details, sort_keys=True)}"
        )


def _read_owner(path: Path) -> dict:
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("owner metadata is not a regular file")
    with path.open("rb") as handle:
        raw = handle.read(4097)
    if len(raw) > 4096:
        raise ValueError("owner metadata exceeds 4096 bytes")
    try:
        owner = json.loads(raw)
    except RecursionError as exc:
        raise ValueError("owner metadata exceeds nesting limit") from exc
    if not isinstance(owner, dict):
        raise ValueError("owner metadata is not an object")
    if type(owner.get("pid")) is not int or owner["pid"] <= 0:
        raise ValueError("owner PID is invalid")
    for key in ("owner_id", "host", "operation", "acquired_at", "task_id", "session_id"):
        value = owner.get(key)
        if key in {"task_id", "session_id"} and value is None:
            continue
        if not isinstance(value, str) or not value or len(value) > 256 or any(ord(char) < 32 for char in value):
            raise ValueError(f"owner {key} is invalid")
    acquired = datetime.fromisoformat(owner["acquired_at"])
    if acquired.utcoffset() is None:
        raise ValueError("owner acquisition time has no timezone")
    return {key: owner.get(key) for key in (
        "owner_id", "pid", "host", "operation", "acquired_at", "task_id", "session_id",
    )}


def _lock_details(lock: Path) -> dict:
    details: dict = {
        "code": "CGA_UPDATE_QUEUE_LOCKED",
        "lock_path": str(lock.absolute()),
        "owner_metadata_path": str((lock / "owner.json").absolute()),
        "owner": None,
        "held_seconds": None,
        "process_started_at": None,
        "heartbeat_at": None,
        "expires_at": None,
        "expiry_policy": "No TTL; only explicit owner release. Age or PID alone does not authorize removal.",
        "unavailable": {
            "process_started_at": "Not tracked; PID may be reused and is local to the recorded host/container.",
            "heartbeat_at": "Not used for this short-lived directory mutex.",
        },
    }
    try:
        owner = _read_owner(lock / "owner.json")
    except (OSError, ValueError, UnicodeError) as exc:
        details["metadata_status"] = "unavailable"
        details["metadata_error"] = (
            f"{type(exc).__name__} (errno={exc.errno})" if isinstance(exc, OSError) else "Invalid owner metadata"
        )
    else:
        details["owner"] = owner
        details["metadata_status"] = "recorded; diagnostic snapshot, not proof of liveness"
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(owner["acquired_at"])).total_seconds()
        if elapsed >= 0:
            details["held_seconds"] = round(elapsed, 3)
        else:
            details["unavailable"]["held_seconds"] = "Recorded acquisition time is in the future; check clock skew."
        for key in ("task_id", "session_id"):
            if owner[key] is None:
                details["unavailable"][key] = "Not supplied by the owning operation."
    return details


def atomic_json(path: Path, value: dict) -> None:
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("x", encoding="utf-8") as handle:
            json.dump(value, handle)
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


@contextmanager
def queue_lock(root: Path, *, operation: str = "maintenance", task_id: str | None = None,
               session_id: str | None = None):
    for label, value in (("operation", operation), ("task_id", task_id), ("session_id", session_id)):
        if value is None and label != "operation":
            continue
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
            raise ValueError(f"Invalid lock {label}; expected a bounded identifier.")
    lock = root / "queue.lock"
    try:
        lock.mkdir()
    except FileExistsError as exc:
        raise QueueLockBusyError(_lock_details(lock)) from exc
    identity = lock.stat()
    owner_path = lock / "owner.json"
    owner_id = uuid.uuid4().hex
    try:
        atomic_json(owner_path, {
            "owner_id": owner_id, "pid": os.getpid(), "host": socket.gethostname(),
            "operation": operation, "task_id": task_id, "session_id": session_id,
            "acquired_at": datetime.now(timezone.utc).isoformat(),
        })
        yield
    finally:
        current = lock.stat()
        if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
            raise RuntimeError(f"Queue lock identity changed; no lock was removed: {lock.absolute()}")
        if owner_path.exists():
            try:
                owner = _read_owner(owner_path)
            except (OSError, ValueError) as exc:
                raise RuntimeError(
                    f"Cannot verify queue lock owner ({type(exc).__name__}); "
                    f"no lock was removed: {lock.absolute()}"
                ) from exc
            if owner["owner_id"] != owner_id:
                raise RuntimeError(f"Queue lock owner changed; no lock was removed: {lock.absolute()}")
            owner_path.unlink()
        lock.rmdir()


def pending() -> bool:
    configured = os.getenv("CGA_UPDATE_STATE_DIR", "").strip()
    if not configured:
        return False
    root = Path(configured)
    return (root / "request.json").exists() or (root / "active.json").exists()
