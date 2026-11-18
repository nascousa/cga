import json
import asyncio
import os
import subprocess
import sys
import time
from contextlib import ExitStack, asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.maintenance import retention, updates
from backend.auth.database import get_db
from backend.maintenance.history import validate_change_diff, version_tuple
from backend.maintenance.router import router
from backend.maintenance.spool import QueueLockBusyError, atomic_json, queue_lock
from backend.maintenance.update_manifest import verify_manifest
from backend.maintenance.gate import MaintenanceGate
from backend.indexer.consumer import IndexerConsumer
from backend.schedules import service as schedules
from scripts.cga_update_host import HostUpdater
from scripts import sign_update_manifest


def test_queue_lock_reports_real_owner_across_processes_without_removing_lock(tmp_path):
    root = Path(__file__).resolve().parents[2]
    with queue_lock(tmp_path, operation="test_owner", task_id="task-42", session_id="session-17"):
        owner_path = tmp_path / "queue.lock" / "owner.json"
        original = owner_path.read_bytes()
        script = """
import json,sys
from pathlib import Path
from backend.maintenance.spool import queue_lock,QueueLockBusyError
try:
    with queue_lock(Path(sys.argv[1]),operation='contender',task_id='other-task'):
        raise AssertionError('Contender acquired the owner lock')
except QueueLockBusyError as error:
    print(json.dumps({'message':str(error),'details':error.details}))
"""
        result = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True, timeout=10,
            env={**os.environ, "PYTHONPATH": str(root / "src"), "CGA_SESSION_ID": "not-the-owner"},
        )
        assert result.returncode == 0, result.stderr
        error = json.loads(result.stdout)
        details = error["details"]
        assert details["owner"]["pid"] == os.getpid()
        assert details["owner"]["task_id"] == "task-42"
        assert details["owner"]["session_id"] == "session-17"
        assert details["owner"]["operation"] == "test_owner"
        assert details["owner"]["host"] and details["owner"]["acquired_at"]
        assert details["held_seconds"] >= 0
        assert details["expires_at"] is None and "No TTL" in details["expiry_policy"]
        assert details["lock_path"] == str((tmp_path / "queue.lock").absolute())
        assert details["owner_metadata_path"] == str(owner_path.absolute())
        assert "No lock was removed" in error["message"]
        assert "not-the-owner" not in error["message"]
        assert owner_path.read_bytes() == original
    assert not (tmp_path / "queue.lock").exists()
    with queue_lock(tmp_path):
        pass


@pytest.mark.parametrize("record", [
    None, "not-json-secret-sentinel", '{"pid":true}', "x" * 4097,
    pytest.param("[" * 2000 + "]" * 2000, id="deeply-nested"),
])
def test_queue_lock_legacy_or_invalid_owner_remains_locked(tmp_path, record):
    lock = tmp_path / "queue.lock"
    lock.mkdir()
    if record is not None:
        (lock / "owner.json").write_text(record, encoding="utf-8")
    before = {path.name: path.read_bytes() for path in lock.iterdir()}
    with pytest.raises(QueueLockBusyError) as caught:
        with queue_lock(tmp_path):
            pytest.fail("Legacy or invalid owner must not bypass the lock")
    assert caught.value.details["metadata_status"] == "unavailable"
    assert caught.value.details["owner"] is None
    assert caught.value.details["metadata_error"]
    assert "secret-sentinel" not in str(caught.value)
    assert lock.is_dir()
    assert {path.name: path.read_bytes() for path in lock.iterdir()} == before


def test_queue_lock_decoder_recursion_remains_busy_without_removing_owner(tmp_path, monkeypatch):
    lock = tmp_path / "queue.lock"
    lock.mkdir()
    path = lock / "owner.json"
    original = ("[" * 1100 + "]" * 1100).encode()
    path.write_bytes(original)

    def decode_recursion(raw):
        assert raw == original
        raise RecursionError("private-error-sentinel")

    monkeypatch.setattr(json, "loads", decode_recursion)
    with pytest.raises(QueueLockBusyError) as caught:
        with queue_lock(tmp_path):
            pytest.fail("Decoder recursion must not bypass the lock")
    assert caught.value.details["metadata_status"] == "unavailable"
    assert caught.value.details["owner"] is None
    assert caught.value.details["metadata_error"] == "Invalid owner metadata"
    assert "private-error-sentinel" not in str(caught.value)
    assert path.read_bytes() == original


def test_queue_lock_reports_metadata_permission_error_without_leaking_or_removing_owner(tmp_path, monkeypatch):
    from backend.maintenance import spool

    with queue_lock(tmp_path):
        original = (tmp_path / "queue.lock/owner.json").read_bytes()
        with monkeypatch.context() as patch:
            def denied(_path):
                raise PermissionError(13, "private-error-sentinel")
            patch.setattr(spool, "_read_owner", denied)
            with pytest.raises(QueueLockBusyError) as caught:
                with queue_lock(tmp_path):
                    pytest.fail("Metadata errors must not bypass the lock")
            assert caught.value.details["metadata_error"] == "PermissionError (errno=13)"
            assert "private-error-sentinel" not in str(caught.value)
        assert (tmp_path / "queue.lock/owner.json").read_bytes() == original


def test_queue_lock_does_not_remove_replaced_owner_metadata(tmp_path):
    with pytest.raises(RuntimeError, match="owner changed; no lock was removed"):
        with queue_lock(tmp_path):
            path = tmp_path / "queue.lock/owner.json"
            owner = json.loads(path.read_text())
            owner["owner_id"] = "replacement-owner"
            atomic_json(path, owner)
    assert json.loads(path.read_text())["owner_id"] == "replacement-owner"


def test_queue_lock_corrupt_owner_on_release_reports_path_without_removing_lock(tmp_path):
    with pytest.raises(RuntimeError, match="Cannot verify queue lock owner") as caught:
        with queue_lock(tmp_path):
            path = tmp_path / "queue.lock" / "owner.json"
            path.write_text("invalid-secret-sentinel", encoding="utf-8")
    assert str(path.parent.absolute()) in str(caught.value)
    assert "no lock was removed" in str(caught.value)
    assert "secret-sentinel" not in str(caught.value)
    assert path.read_text() == "invalid-secret-sentinel"


@pytest.mark.parametrize("context", [{"operation": None}, {"task_id": "invalid private-sentinel"}])
def test_queue_lock_rejects_invalid_context_before_acquisition(tmp_path, context):
    with pytest.raises(ValueError, match="Invalid lock") as caught:
        with queue_lock(tmp_path, **context):
            pytest.fail("Invalid context must not enter the protected section")
    assert "private-sentinel" not in str(caught.value)
    assert not (tmp_path / "queue.lock").exists()


def test_queue_lock_metadata_write_failure_releases_only_its_new_directory(tmp_path, monkeypatch):
    from backend.maintenance import spool

    def fail(_path, _value):
        raise OSError("Injected metadata persistence failure")
    monkeypatch.setattr(spool, "atomic_json", fail)
    with pytest.raises(OSError, match="metadata persistence"):
        with queue_lock(tmp_path):
            pytest.fail("A failed initialization must not enter the protected section")
    assert not (tmp_path / "queue.lock").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("initially_pending", [False, True])
async def test_scheduler_stops_starting_work_when_update_is_pending(tmp_path, monkeypatch, initially_pending):
    monkeypatch.setenv("CGA_UPDATE_STATE_DIR", str(tmp_path))
    if initially_pending:
        atomic_json(tmp_path / "request.json", {})
    calls = []

    @asynccontextmanager
    async def acquire():
        yield SimpleNamespace()

    async def due(*args, **kwargs):
        assert not initially_pending
        return ["first", "second"]

    async def execute(db, task):
        calls.append(task)
        atomic_json(tmp_path / "active.json", {})

    monkeypatch.setattr(schedules, "get_pool", lambda: SimpleNamespace(acquire=acquire))
    monkeypatch.setattr(schedules, "get_due_scheduled_tasks", due)
    monkeypatch.setattr(schedules, "execute_scheduled_task", execute)
    count = await schedules.ScheduledTaskWorker().run_due_once()
    assert calls == ([] if initially_pending else ["first"])
    assert count == len(calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("initially_pending", [False, True])
async def test_indexer_pauses_before_fetch_and_before_starting_fetched_work(tmp_path, monkeypatch, initially_pending):
    monkeypatch.setenv("CGA_UPDATE_STATE_DIR", str(tmp_path))
    if initially_pending:
        atomic_json(tmp_path / "request.json", {})
    connected = asyncio.Event()

    async def connect():
        connected.set()

    async def consume(**kwargs):
        atomic_json(tmp_path / "request.json", {})
        return [("message", SimpleNamespace())]

    worker = IndexerConsumer("redis://unused.invalid", SimpleNamespace())
    worker._consumer = SimpleNamespace(
        connect=connect, close=AsyncMock(), consume=AsyncMock(side_effect=consume),
    )
    worker._process = AsyncMock()
    running = asyncio.create_task(worker.start())
    try:
        await asyncio.wait_for(connected.wait(), timeout=2)
        assert worker._consumer.consume.await_count == (0 if initially_pending else 1)
        worker._process.assert_not_awaited()
    finally:
        worker._running = False
        running.cancel()
        await running


def test_change_logs_gate_requires_notes_and_preserves_published_history():
    release = {"version": "1.30.132", "summary": "Published evidence"}
    data = {"releases": [release], "unreleased": {"changes": {}}}
    validate_change_diff(data, [release], ["README.md"])
    with pytest.raises(ValueError, match="require"):
        validate_change_diff(data, [release], ["src/frontend/maintenance.js"])
    with pytest.raises(ValueError, match="require"):
        validate_change_diff(data, [release], [
            "src/frontend/maintenance.js", "src/backend/maintenance/changes/deleted.json",
        ])
    data["unreleased"]["changes"] = {"Added": ["User-facing change"]}
    validate_change_diff(data, [release], [
        "src/frontend/maintenance.js", "src/backend/maintenance/changes/maintenance.json",
    ])
    data["releases"] = [{"version": "1.30.132", "summary": "Rewritten history"}]
    with pytest.raises(ValueError, match="overwritten"):
        validate_change_diff(data, [release], ["README.md"])


@pytest.mark.parametrize("case", ["success", "unsigned", "oversized", "mismatch"])
def test_release_signer_requires_valid_frozen_package_and_private_key(tmp_path, signed_update, monkeypatch, case):
    private, key, claims, _, _ = signed_update
    script = tmp_path / "source" / "scripts" / "sign.py"
    script.parent.mkdir(parents=True)
    script.touch()
    backend = script.parents[1] / "backend"
    backend.mkdir()
    (backend / "main.py").write_text('APP_VERSION = "1.30.133"\n')
    monkeypatch.setattr(sign_update_manifest, "__file__", str(script))
    monkeypatch.setattr(sign_update_manifest, "history", lambda version: {
        "unreleased": {"changes": {}}, "releases": [{
            "version": "1.30.133", "summary": "x" * 50001 if case == "oversized" else "Maintenance",
            "changes": {"Added": ["Verified maintenance controls"]},
        }],
    })
    if case == "unsigned":
        monkeypatch.delenv("CGA_UPDATE_SIGNING_KEY", raising=False)
    else:
        monkeypatch.setenv("CGA_UPDATE_SIGNING_KEY", private.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode())
    output, notes = tmp_path / "manifest.jwt", tmp_path / "notes.txt"
    monkeypatch.setattr("sys.argv", [
        "sign", "--version", "1.30.134" if case == "mismatch" else "1.30.133",
        "--image", claims["image"], "--minimum-version", "1.30.132",
        "--output", str(output), "--notes-output", str(notes),
    ])
    if case != "success":
        with pytest.raises(SystemExit):
            sign_update_manifest.main()
        assert not output.exists() and not notes.exists()
    else:
        sign_update_manifest.main()
        verified = verify_manifest(output.read_text().strip(), key, "1.30.132")
        assert verified["notes"] == notes.read_text().strip()
        assert verified["image"] == claims["image"]


@pytest.fixture
def signed_update(tmp_path, monkeypatch):
    private = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    key = tmp_path / "publisher.pem"
    key.write_bytes(private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
    ))
    claims = {
        "iss": "nascousa/cga", "aud": "cga-updater",
        "iat": datetime.now(timezone.utc), "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        "version": "1.30.133", "minimum_version": "1.30.132", "platform": "docker-compose",
        "image": "ghcr.io/nascousa/cga-api@sha256:" + "a" * 64, "notes": "Verified release notes",
        "schema": {"auth": 1, "work_briefing": 1, "graph": 2, "runtime_config": 1},
    }
    token = jwt.encode(claims, private, algorithm="PS384")
    root = tmp_path / "shared"
    root.mkdir()
    monkeypatch.setenv("CGA_UPDATE_PUBLIC_KEY", str(key))
    monkeypatch.setenv("CGA_UPDATE_STATE_DIR", str(root))
    monkeypatch.setenv("CGA_UPDATE_SINGLE_PROCESS", "1")
    monkeypatch.delenv("CGA_PINNED_VERSION", raising=False)
    updates._cache.clear()
    return private, key, claims, token, root


@pytest.mark.parametrize("change", ["expired", "schema", "image", "old", "tampered"])
def test_manifest_rejects_untrusted_or_incompatible_updates(signed_update, change):
    private, key, claims, token, _ = signed_update
    if change == "expired":
        claims["exp"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    elif change == "schema":
        claims["schema"]["auth"] = 99
    elif change == "image":
        claims["image"] = "attacker/image:latest"
    elif change == "old":
        claims["version"] = "1.30.131"
    else:
        private = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    token = jwt.encode(claims, private, algorithm="PS384")
    with pytest.raises((ValueError, jwt.InvalidTokenError)):
        verify_manifest(token, key, "1.30.132")


def release_transport(monkeypatch, version="1.30.133", status=200):
    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(status, json={
        "tag_name": "v" + version, "draft": False, "prerelease": False, "body": "Unsigned remote notes",
    }))
    monkeypatch.setattr(updates.httpx, "AsyncClient", lambda **kwargs: original(transport=transport, **kwargs))


@pytest.mark.asyncio
async def test_update_check_never_mistakes_older_release_or_failure_for_upgrade(monkeypatch):
    updates._cache.clear()
    release_transport(monkeypatch, "1.30.113")
    result = await updates.check("1.30.132", force=True)
    assert result["status"] == "ok" and not result["available"]
    assert "newer local" in result["reason"]
    assert version_tuple("1.10.0") > version_tuple("1.9.0")


@pytest.mark.asyncio
async def test_failed_release_check_is_not_up_to_date(monkeypatch):
    updates._cache.clear()
    release_transport(monkeypatch, status=503)
    result = await updates.check("1.30.132", force=True)
    assert result["status"] == "error"
    assert not result["installable"]
    assert "failed" in result["reason"].lower()


@pytest.mark.asyncio
async def test_install_requires_matching_confirmation_and_is_exclusive(signed_update, monkeypatch):
    _, _, claims, token, root = signed_update
    release_transport(monkeypatch)

    async def download(version):
        return token

    monkeypatch.setattr(updates, "_download_manifest", download)
    atomic_json(root / "status.json", {"heartbeat": __import__("time").time(), "accepting_requests": True})
    result = await updates.check("1.30.132", force=True)
    assert result["installable"]
    assert result["notes"] == claims["notes"]
    with pytest.raises(ValueError, match="changed"):
        await updates.request_install("1.30.132", "1.30.133", "wrong", "operator")
    assert not (root / "request.json").exists()
    job = await updates.request_install("1.30.132", claims["version"], claims["image"], "operator")
    assert json.loads((root / "request.json").read_text())["id"] == job["id"]
    with pytest.raises(ValueError, match="already"):
        await updates.request_install("1.30.132", claims["version"], claims["image"], "operator")
    atomic_json(root / "drain.json", {"request_id": job["id"], "ready": True})
    assert updates.cancel_queued()["status"] == "cancelled"
    assert not (root / "drain.json").exists()
    atomic_json(root / "drain.json", {"request_id": job["id"], "ready": True})
    next_job = await updates.request_install("1.30.132", claims["version"], claims["image"], "operator")
    assert next_job["id"] != job["id"]
    assert not (root / "drain.json").exists()


@pytest.mark.asyncio
async def test_update_drain_waits_for_retention_run_and_records_current_request(tmp_path, monkeypatch):
    from backend.plugins import host
    monkeypatch.setenv("CGA_UPDATE_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(host, "catalog_status", lambda: {"items": []})
    started, finish = asyncio.Event(), asyncio.Event()
    recorded = []

    async def cleanup():
        started.set()
        await finish.wait()
        recorded.append("run status persisted")
        return {"status": "ok"}

    monkeypatch.setattr(retention, "_cleanup", cleanup)
    worker = asyncio.create_task(retention.cleanup())
    await asyncio.wait_for(started.wait(), timeout=1)
    atomic_json(tmp_path / "request.json", {"id": "current"})
    idle = asyncio.Event()
    idle.set()
    app = SimpleNamespace(version="1.30.132", state=SimpleNamespace(
        consumer=SimpleNamespace(_idle=idle), maintenance_inflight=0,
    ))

    async def end_iteration(_delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(updates.asyncio, "sleep", end_iteration)
    try:
        with pytest.raises(asyncio.CancelledError):
            await updates.drain_loop(app)
        receipt = json.loads((tmp_path / "drain.json").read_text())
        assert receipt["request_id"] == "current" and not receipt["ready"]
        assert (await retention.cleanup())["status"] == "paused-for-upgrade"
    finally:
        finish.set()
        await worker
    assert recorded and retention.active_cleanups() == 0
    with pytest.raises(asyncio.CancelledError):
        await updates.drain_loop(app)
    assert json.loads((tmp_path / "drain.json").read_text())["ready"]
    (tmp_path / "request.json").unlink()
    with pytest.raises(asyncio.CancelledError):
        await updates.drain_loop(app)
    assert not (tmp_path / "drain.json").exists()


@pytest.mark.asyncio
async def test_retention_releases_inflight_tracking_on_failure(monkeypatch):
    monkeypatch.delenv("CGA_UPDATE_STATE_DIR", raising=False)
    monkeypatch.setattr(retention, "_cleanup", AsyncMock(side_effect=RuntimeError("cleanup failed")))
    with pytest.raises(RuntimeError, match="cleanup failed"):
        await retention.cleanup()
    assert retention.active_cleanups() == 0


@pytest.mark.asyncio
async def test_retention_preserves_protected_recent_and_unknown_time_rows(auth_pg_pool, monkeypatch):
    monkeypatch.delenv("CGA_UPDATE_STATE_DIR", raising=False)
    now = datetime.now(timezone.utc)
    rows = [
        (20, "/api/auth/projects/index-status", "GET", 200),
        (3, "/api/auth/projects/index-status", "GET", 200),
        (120, "/api/projects", "GET", 200),
        (200, "/api/login", "POST", 401),
        (400, "/api/settings", "POST", 200),
        (10, "/api/admin/runtime-config", "GET", 200),
    ]
    async with auth_pg_pool.acquire() as db:
        for days, path, method, status in rows:
            await db.raw.execute("""INSERT INTO audit_logs(created_at,scope,method,path,status_code,duration_ms)
                VALUES($1,'api',$2,$3,$4,1)""", (now - timedelta(days=days)).isoformat(), method, path, status)
        for timestamp in ("unknown", "2020-01-01T10:00:00+05:00"):
            await db.raw.execute("""INSERT INTO audit_logs(created_at,scope,method,path,status_code,duration_ms)
                VALUES($1,'api','GET','/api/projects',200,1)""", timestamp)
    assert (await retention.cleanup())["deleted"] == 0
    policy = retention.RetentionPolicy(enabled=True)
    preview = await retention.preview(policy)
    assert preview["counts"] == {"polling": 1, "request": 1, "protected": 1}
    await retention.save_policy(policy, preview["preview_token"])
    result = await retention.cleanup()
    assert result["deleted"] == 3
    async with auth_pg_pool.acquire() as db:
        assert await db.raw.fetchval("SELECT count(*) FROM audit_logs") == 5
    assert (await retention.status())["last_run"]["deleted"] == 3


@pytest.mark.asyncio
async def test_retention_preview_binding_batch_bound_and_worker_lock(auth_pg_pool, monkeypatch):
    monkeypatch.delenv("CGA_UPDATE_STATE_DIR", raising=False)
    policy = retention.RetentionPolicy(enabled=True, batch_size=100)
    preview = await retention.preview(policy)
    with pytest.raises(ValueError, match="changed"):
        await retention.save_policy(policy.model_copy(update={"request_days": 120}), preview["preview_token"])
    await retention.save_policy(policy, preview["preview_token"])
    async with auth_pg_pool.acquire() as db:
        await db.raw.execute("""INSERT INTO audit_logs(created_at,scope,method,path,status_code,duration_ms)
            SELECT '2020-01-01T00:00:00','api','GET','/api/projects',200,1 FROM generate_series(1,650)""")
        await db.raw.execute("SELECT pg_advisory_lock($1)", retention.LOCK_ID)
        try:
            assert (await retention.cleanup())["status"] == "busy"
        finally:
            await db.raw.execute("SELECT pg_advisory_unlock($1)", retention.LOCK_ID)
    assert (await retention.cleanup())["deleted"] == 500
    async with auth_pg_pool.acquire() as db:
        assert await db.raw.fetchval("SELECT count(*) FROM audit_logs") == 150


def test_admin_mutation_routes_require_authentication():
    app = FastAPI()
    app.include_router(router, prefix="/api")
    async def unused_db():
        yield None
    app.dependency_overrides[get_db] = unused_db
    client = TestClient(app)
    assert client.get("/api/admin/audit-retention").status_code == 401
    assert client.post("/api/admin/updates/install", json={
        "confirm": True, "version": "1.30.133", "image": "x",
    }).status_code == 401


def make_host(tmp_path, signed_update, monkeypatch):
    _, key, claims, token, root = signed_update
    journal = tmp_path / "private"
    journal.mkdir()
    compose = tmp_path / "compose.json"
    compose.write_text(json.dumps({"services": {"cga": {"image": "old-image", "ports": ["18001:8000"]}}}))
    host = HostUpdater({
        "state_dir": str(root), "journal_dir": str(journal), "compose_file": str(compose),
        "public_key": str(key), "project": "test-cga", "service": "cga", "container": "test-cga-api",
        "health_url": "http://127.0.0.1:18001/health", "backup_command": ["trusted-backup"],
        "backup_files": {"auth.sql.gz": "unused", "falkordb.tgz": "unused"},
    })
    before = {"Config": {"Image": "old-image", "Env": ["SECRET=never-write-this-in-journal"]},
              "HostConfig": {"PortBindings": {"8000/tcp": [{"HostPort": "18001"}]}}, "Mounts": []}
    monkeypatch.setattr(host, "inspect", lambda: before)
    monkeypatch.setattr(host, "health", lambda version: None)
    monkeypatch.setattr(host, "wait_drained", lambda version, request_id: None)
    monkeypatch.setattr(host, "backup", lambda job_dir: None)
    monkeypatch.setattr(host, "environment_keys", lambda: ["SECRET"])
    calls = []
    monkeypatch.setattr("scripts.cga_update_host.command", lambda args, **kwargs: calls.append(args) or "")
    atomic_json(root / "request.json", {
        "id": "b" * 32, "version": claims["version"], "image": claims["image"],
        "current_version": "1.30.132", "manifest": token,
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    })
    return host, root, journal, compose, calls


@pytest.mark.parametrize("stale_id", [None, "previous"])
def test_host_requires_current_request_drain_receipt(tmp_path, signed_update, monkeypatch, stale_id):
    host, root, _, _, _ = make_host(tmp_path, signed_update, monkeypatch)
    receipt = {"version": "1.30.132", "ready": True, "heartbeat": time.time()}
    if stale_id is not None:
        receipt["request_id"] = stale_id
    atomic_json(root / "drain.json", receipt)
    sleeps = []

    def refresh(_delay):
        sleeps.append(1)
        atomic_json(root / "drain.json", {**receipt, "request_id": "current"})

    monkeypatch.setattr("scripts.cga_update_host.time.sleep", refresh)
    HostUpdater.wait_drained(host, "1.30.132", "current")
    assert sleeps == [1], "The previous/legacy receipt must not drain a new request"


@pytest.mark.parametrize("configured_change", [False, True])
def test_host_preserves_configured_environment_without_rejecting_image_defaults(
    tmp_path, signed_update, monkeypatch, configured_change,
):
    host, _, _, _, _ = make_host(tmp_path, signed_update, monkeypatch)
    value = host.inspect()
    value["Config"]["Env"] = ["SECRET=preserved", "PYTHON_VERSION=3.12.1"]
    before = host.snapshot(value, ["SECRET"])
    value["Config"]["Env"] = [
        "SECRET=changed" if configured_change else "SECRET=preserved", "PYTHON_VERSION=3.12.2",
    ]
    if configured_change:
        with pytest.raises(ValueError, match="environment changed"):
            host.verify_runtime("1.30.133", before)
    else:
        host.verify_runtime("1.30.133", before)


def test_host_success_preserves_scope_and_keeps_private_journals(tmp_path, signed_update, monkeypatch):
    host, root, journal, compose, calls = make_host(tmp_path, signed_update, monkeypatch)
    monkeypatch.setattr(host, "verify_runtime", lambda *args: None)
    host.process()
    assert not (root / "active.json").exists()
    assert host.progress["status"] == "success"
    assert json.loads(compose.read_text())["services"]["cga"]["ports"] == ["18001:8000"]
    assert calls[1][-4:] == ["stop", "--timeout", "300", "cga"]
    assert all("down" not in call and "--volumes" not in call for call in calls)
    assert "never-write-this-in-journal" not in (journal / ("b" * 32) / "journal.json").read_text()


@pytest.mark.parametrize("queued", [False, True])
def test_host_retries_overlapping_queue_polls_without_stopping_heartbeat(
    tmp_path, signed_update, monkeypatch, capsys, queued,
):
    host, root, _, _, _ = make_host(tmp_path, signed_update, monkeypatch)
    if not queued:
        (root / "request.json").unlink()
    monkeypatch.setattr(host, "verify_runtime", lambda *args: None)
    delays = []
    with ExitStack() as held:
        held.enter_context(queue_lock(root))
        with pytest.raises(QueueLockBusyError):
            host.process()

        def release_after_busy_poll(delay):
            delays.append(delay)
            assert host.thread.is_alive()
            assert not host.stop_event.is_set()
            assert (root / "queue.lock").is_dir()
            assert (root / "status.json").is_file()
            held.close()

        monkeypatch.setattr("scripts.cga_update_host.time.sleep", release_after_busy_poll)
        host.run(once=True)
    assert delays == [2]
    assert host.progress["status"] == ("success" if queued else "ready")
    assert not (root / "request.json").exists()
    assert not host.thread.is_alive()
    error = capsys.readouterr().err
    assert "queue is busy" in error
    assert json.dumps(str((root / "queue.lock").absolute())) in error
    assert f'"pid": {os.getpid()}' in error
    assert "No lock was removed" in error


@pytest.mark.parametrize("stage", ["recover", "process"])
def test_host_does_not_retry_recovery_errors_as_queue_contention(tmp_path, signed_update, monkeypatch, stage):
    host, _, _, _, _ = make_host(tmp_path, signed_update, monkeypatch)

    def recovery_required():
        raise ValueError("Operator recovery required")

    monkeypatch.setattr(host, stage, recovery_required)
    with pytest.raises(ValueError, match="Operator recovery required"):
        host.run(once=True)
    assert host.stop_event.is_set()
    assert host.thread is None or not host.thread.is_alive()


def test_host_target_failure_restores_image_not_database(tmp_path, signed_update, monkeypatch):
    host, root, journal, compose, calls = make_host(tmp_path, signed_update, monkeypatch)

    def verify(version, *args):
        if version == "1.30.133":
            raise RuntimeError("Injected target health failure")

    monkeypatch.setattr(host, "verify_runtime", verify)
    host.process()
    assert host.progress["status"] == "rolled-back"
    assert json.loads(compose.read_text())["services"]["cga"]["image"] == "old-image"
    assert not (root / "active.json").exists()
    assert (journal / ("b" * 32) / "failed.json").exists()
    assert not any("restore" in arg for call in calls for arg in call)


def test_host_interrupted_failure_can_recover_from_private_journal(tmp_path, signed_update, monkeypatch):
    host, root, journal, compose, _ = make_host(tmp_path, signed_update, monkeypatch)
    rollback = host.rollback

    def fail(*args):
        raise RuntimeError("Injected interrupted replacement")

    monkeypatch.setattr(host, "verify_runtime", fail)
    monkeypatch.setattr(host, "rollback", fail)
    with pytest.raises(RuntimeError):
        host.process()
    assert host.progress["status"] == "recovery-required"
    assert (root / "active.json").exists()
    monkeypatch.setattr(host, "rollback", rollback)
    monkeypatch.setattr(host, "verify_runtime", lambda *args: None)
    host.recover()
    assert host.progress["status"] == "rolled-back"
    assert not (root / "active.json").exists()
    assert json.loads(compose.read_text())["services"]["cga"]["image"] == "old-image"
    assert (journal / ("b" * 32) / "interrupted.json").exists()


@pytest.mark.asyncio
async def test_maintenance_gate_drains_complete_response_and_rejects_new_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("CGA_UPDATE_STATE_DIR", str(tmp_path))
    started, release = asyncio.Event(), asyncio.Event()
    state = SimpleNamespace()
    app = SimpleNamespace(state=state)

    async def endpoint(scope, receive, send):
        started.set()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await release.wait()
        await send({"type": "http.response.body", "body": b"done"})

    messages = []

    async def send(message):
        messages.append(message)

    gate = MaintenanceGate(endpoint)
    scope = {"type": "http", "path": "/api/work", "method": "POST", "app": app}
    task = asyncio.create_task(gate(scope, None, send))
    await started.wait()
    assert state.maintenance_inflight == 1
    atomic_json(tmp_path / "request.json", {})
    await gate(scope, None, send)
    assert messages[-2]["status"] == 503
    assert state.maintenance_inflight == 1
    release.set()
    await task
    assert state.maintenance_inflight == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "failure", "cancel"])
async def test_drain_tracks_actual_core_mcp_invocations_and_rejects_late_dispatch(tmp_path, monkeypatch, outcome):
    from backend.maintenance.gate import active_mcp_invocations
    from backend.plugins import host
    from backend.tools import server
    from mcp.server.fastmcp.exceptions import ToolError

    monkeypatch.setenv("CGA_UPDATE_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(host, "catalog_status", lambda: {"items": []})
    started, release = asyncio.Event(), asyncio.Event()

    async def record_learning(**kwargs):
        started.set()
        await release.wait()
        if outcome == "failure":
            raise RuntimeError("Injected storage failure")
        return SimpleNamespace(operation="created", activity=SimpleNamespace(to_dict=lambda: {"id": "learning"}))

    service = SimpleNamespace(record_learning=AsyncMock(side_effect=record_learning))
    monkeypatch.setattr(server, "_work_briefing_service", service)
    idle = asyncio.Event()
    idle.set()
    app = SimpleNamespace(version="1.30.133", state=SimpleNamespace(
        consumer=SimpleNamespace(_idle=idle), maintenance_inflight=0,
    ))
    arguments = dict(title="Test", lesson="Evidence", category="decision", outcome="neutral", project_id="CGA")
    task = asyncio.create_task(server.mcp.call_tool("workassist_record_learning", arguments))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        assert active_mcp_invocations() == 1
        atomic_json(tmp_path / "request.json", {"id": "current"})
        with pytest.raises(RuntimeError, match="draining"):
            await server.mcp.call_tool("workassist_record_learning", arguments)
        assert service.record_learning.await_count == 1

        async def end_iteration(_delay):
            raise asyncio.CancelledError

        monkeypatch.setattr(updates.asyncio, "sleep", end_iteration)
        with pytest.raises(asyncio.CancelledError):
            await updates.drain_loop(app)
        assert not json.loads((tmp_path / "drain.json").read_text())["ready"]
        if outcome == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif outcome == "failure":
            release.set()
            with pytest.raises(ToolError, match="Injected storage failure"):
                await task
        else:
            release.set()
            await task
        assert active_mcp_invocations() == 0
        with pytest.raises(asyncio.CancelledError):
            await updates.drain_loop(app)
        assert json.loads((tmp_path / "drain.json").read_text())["ready"]
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
