from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path


class QueueLockBusyError(ValueError):
    pass


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
def queue_lock(root: Path):
    lock = root / "queue.lock"
    try:
        lock.mkdir()
    except FileExistsError as exc:
        raise QueueLockBusyError("Update queue is busy. Retry; inspect a stale lock only after stopping its owner.") from exc
    try:
        yield
    finally:
        lock.rmdir()


def pending() -> bool:
    configured = os.getenv("CGA_UPDATE_STATE_DIR", "").strip()
    if not configured:
        return False
    root = Path(configured)
    return (root / "request.json").exists() or (root / "active.json").exists()
