"""Backup / restore service for the CGA auth PostgreSQL database.

Design goals
------------
* Online snapshots via ``pg_dump`` (no service interruption).
* Restore via ``psql`` against a transactional script.
* Bounded-memory streaming through private disk-backed staging files.
* Configurable auto-backup loop (enabled / interval / retention).
* Pure-stdlib persistence of config to a JSON sidecar file.

Snapshot files are stored under ``backup_dir`` with names of the form
``auth-<UTC-ISO>.sql.gz`` (gzip-compressed plain-text dumps).  A
``auth-latest.sql.gz`` pointer is maintained for convenience.
"""

from __future__ import annotations

import asyncio
import dataclasses
from contextlib import contextmanager
import errno
import gzip
import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
import zlib
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Callable, Optional, TypeVar
from urllib.parse import urlparse

log = logging.getLogger(__name__)
_T = TypeVar("_T")
_IO_CHUNK_SIZE = 1024 * 1024
_STDERR_LIMIT = 64 * 1024


class BackupError(Exception):
    """Raised for user-visible backup/restore failures."""


@contextmanager
def _advisory_lock(path: Path, timeout: float):
    try:
        lock = path.open("a+b")
    except OSError as exc:
        if path.is_dir():
            raise BackupError("Legacy backup lock directory exists; stop old workers and verify it before removal") from exc
        raise BackupError(f"Could not open backup lock: {exc}") from exc
    with lock:
        if os.name == "nt":
            import msvcrt

            if os.fstat(lock.fileno()).st_size == 0:
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
        else:
            import fcntl

        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == "nt":
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise BackupError(f"Could not acquire backup lock: {exc}") from exc
                if time.monotonic() >= deadline:
                    raise BackupError("Timed out waiting for an active backup/restore lock") from exc
                time.sleep(0.05)
        try:
            yield lock
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


@dataclass
class BackupConfig:
    enabled: bool = True
    interval_minutes: int = 60
    keep_count: int = 24

    @classmethod
    def from_dict(cls, data: dict) -> "BackupConfig":
        return cls(
            enabled=bool(data.get("enabled", True)),
            interval_minutes=max(1, int(data.get("interval_minutes", 60))),
            keep_count=max(1, int(data.get("keep_count", 24))),
        )

    def to_dict(self) -> dict:
        return asdict(self)


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _process_error(stderr: BinaryIO) -> str:
    stderr.flush()
    stderr.seek(0)
    data = stderr.read(_STDERR_LIMIT + 1)
    message = data[:_STDERR_LIMIT].decode("utf-8", errors="replace").strip()
    if len(data) > _STDERR_LIMIT:
        message += " [stderr truncated]"
    return message or "unknown error"


def _pg_env_from_dsn(dsn: str) -> dict[str, str]:
    """Build the env dict that ``pg_dump`` / ``psql`` expect.

    asyncpg-style DSNs map cleanly onto PG* environment variables; using
    env vars avoids leaking the password into the process listing.
    """
    parsed = urlparse(dsn)
    env = os.environ.copy()
    if parsed.hostname:
        env["PGHOST"] = parsed.hostname
    if parsed.port:
        env["PGPORT"] = str(parsed.port)
    if parsed.username:
        env["PGUSER"] = parsed.username
    if parsed.password:
        env["PGPASSWORD"] = parsed.password
    db = (parsed.path or "").lstrip("/")
    if db:
        env["PGDATABASE"] = db
    return env


class BackupService:
    """Manage logical snapshots of the auth PostgreSQL database."""

    def __init__(self, dsn: str, backup_dir: str) -> None:
        self._dsn = dsn
        self._backup_dir = Path(backup_dir)
        self._config_path = self._backup_dir / "config.json"
        self._backup_dir.mkdir(parents=True, exist_ok=True)
        self._config = self._load_config()
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._operation_lock: BinaryIO | None = None
        self._last_run_at: Optional[float] = None
        self._last_run_status: Optional[str] = None
        self._last_run_error: Optional[str] = None

    # ── config ────────────────────────────────────────────────────────────
    def _load_config(self) -> BackupConfig:
        if self._config_path.is_file():
            try:
                with self._config_path.open("r", encoding="utf-8") as fh:
                    return BackupConfig.from_dict(json.load(fh))
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                log.warning("backup.config.load_failed", extra={"error": str(exc)})
        return BackupConfig()

    def _save_config(self) -> None:
        tmp = self._config_path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(self._config.to_dict(), fh, indent=2)
        tmp.replace(self._config_path)

    def get_config(self) -> BackupConfig:
        return dataclasses.replace(self._config)

    def update_config(self, patch: dict) -> BackupConfig:
        merged = {**self._config.to_dict(), **(patch or {})}
        self._config = BackupConfig.from_dict(merged)
        self._save_config()
        return self.get_config()

    # ── snapshots ─────────────────────────────────────────────────────────
    def list_snapshots(self) -> list[dict]:
        items: list[dict] = []
        for path in sorted(self._backup_dir.glob("auth-*.sql.gz"), reverse=True):
            if path.name == "auth-latest.sql.gz":
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            items.append(
                {
                    "name": path.name,
                    "size_bytes": stat.st_size,
                    "created_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "format": "pg_dump",
                }
            )
        items.sort(key=lambda item: item["created_at"], reverse=True)
        return items

    def snapshot_path(self, name: str) -> Path:
        # Allow only simple snapshot file names produced by us.
        if (
            not name
            or "/" in name
            or "\\" in name
            or not name.startswith("auth-")
            or not name.endswith(".sql.gz")
        ):
            raise BackupError("invalid snapshot name")
        path = (self._backup_dir / name).resolve()
        try:
            path.relative_to(self._backup_dir.resolve())
        except ValueError as exc:
            raise BackupError("invalid snapshot path") from exc
        if not path.is_file():
            raise BackupError("snapshot not found")
        return path

    async def run_backup(self, *, reason: str = "manual") -> dict:
        async with self._lock:
            return await asyncio.to_thread(self._run_locked, self._do_backup_sync, reason)

    def _run_locked(self, operation: Callable[..., _T], *args: object) -> _T:
        # Never unlink this file: its inode carries the kernel-managed lock.
        lock = self._backup_dir / ".auth.lock"
        try:
            timeout = float(os.environ.get("BACKUP_LOCK_TIMEOUT_SECONDS", "300"))
            if not math.isfinite(timeout) or timeout < 0:
                raise ValueError("expected a finite non-negative number")
        except ValueError as exc:
            raise BackupError(f"invalid BACKUP_LOCK_TIMEOUT_SECONDS: {exc}") from exc
        with _advisory_lock(lock, timeout) as handle:
            self._operation_lock = handle
            try:
                return operation(*args)
            finally:
                self._operation_lock = None

    def _subprocess_lock_options(self) -> dict:
        if os.name != "nt" and self._operation_lock is not None:
            # An orphaned POSIX pg_dump/psql must retain the lease until it exits.
            return {"pass_fds": (self._operation_lock.fileno(),)}
        return {}

    def _do_backup_sync(self, reason: str, *, protected: frozenset[str] = frozenset()) -> dict:
        started = time.time()
        ts = _iso_now()
        target = self._backup_dir / f"auth-{ts}-{uuid.uuid4().hex}.sql.gz"
        tmp = self._backup_dir / f".{target.name}.tmp"
        latest_tmp = self._backup_dir / f".{target.name}.latest.tmp"
        env = _pg_env_from_dsn(self._dsn)
        # ``pg_dump --clean --if-exists`` produces a self-contained script
        # that can be replayed against an empty or existing database.
        cmd = [
            "pg_dump",
            "--no-owner",
            "--no-privileges",
            "--clean",
            "--if-exists",
            "--format=plain",
        ]
        try:
            with (
                tmp.open("xb") as out,
                tempfile.TemporaryFile(
                    mode="w+b", prefix=".cga-pg-dump-", dir=self._backup_dir
                ) as dump,
                tempfile.TemporaryFile(
                    mode="w+b", prefix=".cga-pg-errors-", dir=self._backup_dir
                ) as errors,
            ):
                try:
                    proc = subprocess.run(
                        cmd,
                        env=env,
                        stdout=dump,
                        stderr=errors,
                        check=False,
                        **self._subprocess_lock_options(),
                    )
                except FileNotFoundError as exc:
                    raise BackupError(f"pg_dump not installed: {exc}") from exc
                except OSError as exc:
                    raise BackupError(f"could not start pg_dump: {exc}") from exc
                if proc.returncode != 0:
                    raise BackupError(f"pg_dump failed: {_process_error(errors)}")
                dump.flush()
                dump.seek(0)
                header = dump.read(16384)
                source_version = re.search(rb"-- Dumped from database version (\d+)", header)
                client_version = re.search(rb"-- Dumped by pg_dump version (\d+)", header)
                if source_version is None or client_version is None:
                    raise BackupError("pg_dump output is missing its PostgreSQL version headers")
                if source_version.group(1) != client_version.group(1):
                    raise BackupError(
                        "pg_dump major version must match the PostgreSQL server "
                        f"({client_version.group(1).decode()} != {source_version.group(1).decode()}); "
                        "install matching client tools before creating a restorable backup"
                    )
                dump.seek(0)
                with gzip.GzipFile(fileobj=out, mode="wb") as gz:
                    shutil.copyfileobj(dump, gz, length=_IO_CHUNK_SIZE)
                out.flush()
                os.fsync(out.fileno())
            tmp.replace(target)

            if "auth-latest.sql.gz" not in protected:
                try:
                    shutil.copyfile(target, latest_tmp)
                    with latest_tmp.open("r+b") as fh:
                        os.fsync(fh.fileno())
                    latest_tmp.replace(self._backup_dir / "auth-latest.sql.gz")
                except OSError as exc:
                    raise BackupError(f"backup latest update failed: {exc}") from exc
            size_bytes = target.stat().st_size
        except BackupError as exc:
            self._record_run(False, str(exc))
            raise
        except OSError as exc:
            self._record_run(False, str(exc))
            raise BackupError(f"backup write failed: {exc}") from exc
        finally:
            for pending in (tmp, latest_tmp):
                try:
                    pending.unlink(missing_ok=True)
                except OSError as exc:
                    log.warning("backup.staging_cleanup_failed", extra={"error": str(exc)})

        self._prune(protected | {target.name})
        self._record_run(True, None)
        return {
            "name": target.name,
            "size_bytes": size_bytes,
            "duration_ms": int((time.time() - started) * 1000),
            "reason": reason,
        }

    def _prune(self, protected: frozenset[str] = frozenset()) -> None:
        # Prune only the pg_dump-format snapshots; legacy SQLite snapshots
        # are left in place for the operator to manage manually.
        dated = []
        for path in self._backup_dir.glob("auth-2*.sql.gz"):
            try:
                dated.append((path.stat().st_mtime_ns, path))
            except OSError:
                continue
        snapshots = [path for _, path in sorted(dated, reverse=True)]
        excess = snapshots[self._config.keep_count :]
        for path in excess:
            if path.name in protected:
                continue
            try:
                path.unlink()
            except OSError as exc:
                log.warning("backup.prune_failed", extra={"path": str(path), "error": str(exc)})

    def _record_run(self, ok: bool, error: Optional[str]) -> None:
        self._last_run_at = time.time()
        self._last_run_status = "ok" if ok else "error"
        self._last_run_error = error

    # ── restore ───────────────────────────────────────────────────────────
    async def restore(self, name: str) -> dict:
        async with self._lock:
            return await asyncio.to_thread(self._run_locked, self._do_restore_sync, name)

    def _freeze_restore_source(self, src: Path, name: str) -> BinaryIO:
        # Disk-backed, private, delete-on-close (anonymous where supported).
        # Never use the system temp directory, which may be a memory filesystem.
        try:
            with tempfile.TemporaryFile(
                mode="w+b", prefix=".cga-restore-", dir=self._backup_dir
            ) as staging:
                has_sql = False
                try:
                    with gzip.open(src, "rb") as gz:
                        for chunk in iter(lambda: gz.read(_IO_CHUNK_SIZE), b""):
                            has_sql = has_sql or bool(chunk.strip())
                            try:
                                staging.write(chunk)
                            except OSError as exc:
                                raise BackupError(f"restore staging write failed: {exc}") from exc
                except (OSError, EOFError, zlib.error) as exc:
                    raise BackupError(f"snapshot read failed ({name}): {exc}") from exc
                if not has_sql:
                    raise BackupError(f"snapshot is empty ({name})")
                # Reaching gzip EOF above verifies its trailer/CRC before any
                # safety dump. Retain only a read-only stream for the frozen FD.
                staging.flush()
                os.fsync(staging.fileno())
                staging.seek(0)
                fd = os.dup(staging.fileno())
                try:
                    frozen = os.fdopen(fd, "rb")
                except BaseException:
                    os.close(fd)
                    raise
                try:
                    staging.close()
                except BaseException:
                    frozen.close()
                    raise
                return frozen
        except OSError as exc:
            raise BackupError(f"restore staging failed: {exc}") from exc

    def _do_restore_sync(self, name: str) -> dict:
        src = self.snapshot_path(name)
        # The frozen FD is independent of the original path and remains open
        # throughout safety publication/pruning and the transactional restore.
        with self._freeze_restore_source(src, name) as frozen:
            try:
                safety = self._do_backup_sync(
                    reason="pre-restore-safety", protected=frozenset({name, src.name})
                )
            except BackupError as exc:
                raise BackupError(f"failed to capture pre-restore safety dump: {exc}") from exc

            env = _pg_env_from_dsn(self._dsn)
            try:
                with tempfile.TemporaryFile(
                    mode="w+b", prefix=".cga-psql-errors-", dir=self._backup_dir
                ) as errors:
                    try:
                        proc = subprocess.run(
                            [
                                "psql",
                                "--quiet",
                                "--no-psqlrc",
                                "--single-transaction",
                                "--set=ON_ERROR_STOP=1",
                                "--file=-",
                            ],
                            env=env,
                            stdin=frozen,
                            stdout=subprocess.DEVNULL,
                            stderr=errors,
                            check=False,
                            **self._subprocess_lock_options(),
                        )
                    except FileNotFoundError as exc:
                        raise BackupError(f"psql not installed: {exc}") from exc
                    except OSError as exc:
                        raise BackupError(f"could not start psql: {exc}") from exc
                    if proc.returncode != 0:
                        raise BackupError(f"psql restore failed: {_process_error(errors)}")
            except OSError as exc:
                raise BackupError(f"restore diagnostics I/O failed: {exc}") from exc

        return {
            "restored_from": name,
            "pre_restore_snapshot": safety.get("name"),
            "note": "Restart the CGA service to ensure all components pick up the restored database.",
        }

    async def delete(self, name: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._run_locked, self._do_delete_sync, name)

    def _do_delete_sync(self, name: str) -> None:
        path = self.snapshot_path(name)
        try:
            path.unlink()
        except OSError as exc:
            raise BackupError(f"snapshot delete failed: {exc}") from exc

    # ── status ────────────────────────────────────────────────────────────
    def status(self) -> dict:
        # NOTE: ``db_path`` is preserved for frontend compatibility (the
        # admin UI reads ``status.db_path``); it now holds the redacted PG DSN.
        redacted = self._redact_dsn(self._dsn)
        return {
            "config": self._config.to_dict(),
            "dsn": redacted,
            "db_path": redacted,
            "backup_dir": str(self._backup_dir),
            "scheduler_running": bool(self._task and not self._task.done()),
            "last_run_at": (
                datetime.fromtimestamp(self._last_run_at, tz=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
                if self._last_run_at
                else None
            ),
            "last_run_status": self._last_run_status,
            "last_run_error": self._last_run_error,
        }

    @staticmethod
    def _redact_dsn(dsn: str) -> str:
        try:
            parsed = urlparse(dsn)
        except ValueError:
            return dsn
        if not parsed.password:
            return dsn
        netloc = parsed.netloc.replace(f":{parsed.password}@", ":***@", 1)
        return parsed._replace(netloc=netloc).geturl()

    # ── scheduler ─────────────────────────────────────────────────────────
    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(), name="cga-backup-scheduler")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _loop(self) -> None:
        # Short initial delay so app startup is not blocked.
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            return
        while True:
            try:
                if self._config.enabled:
                    try:
                        await self.run_backup(reason="scheduled")
                    except BackupError as exc:
                        log.warning("backup.scheduled_failed", extra={"error": str(exc)})
                    except Exception as exc:  # pragma: no cover - defensive
                        log.exception("backup.scheduled_crash", extra={"error": str(exc)})
                await asyncio.sleep(max(1, self._config.interval_minutes) * 60)
            except asyncio.CancelledError:
                return
