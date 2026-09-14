from __future__ import annotations

import asyncio
import errno
import gzip
import hashlib
import os
import stat
import subprocess
import sys
import tempfile
import threading
import tracemalloc
from pathlib import Path

import pytest

from backend.backup import service as backup_module
from backend.backup.service import BackupError, BackupService


SELECTED_SQL = b"-- selected snapshot\nSELECT 'selected';\n"
DUMP_HEADER = b"-- Dumped from database version 16.12\n-- Dumped by pg_dump version 16.12\n"
CURRENT_SQL = DUMP_HEADER + b"-- current database\nSELECT 'current';\n"


def tool_result(
    cmd: list[str], options: dict, returncode: int = 0,
    stdout: bytes = CURRENT_SQL, stderr: bytes = b"",
) -> subprocess.CompletedProcess:
    assert cmd[0] in {"pg_dump", "psql"}, "Tests must not run external commands"
    assert "input" not in options, "SQL must be streamed from a file, not passed as input bytes"
    assert options["stderr"] != subprocess.PIPE, "Diagnostics must not grow an unbounded memory buffer"
    options["stderr"].write(stderr)
    if cmd[0] == "pg_dump":
        assert options["stdout"] != subprocess.PIPE
        options["stdout"].write(stdout)
    else:
        assert options["stdout"] == subprocess.DEVNULL
        stream = options["stdin"]
        assert stream.readable() and not stream.writable()
        assert stream.tell() == 0
        options["_test_stdin"] = stream.read()
    return subprocess.CompletedProcess(cmd, returncode, stdout=None, stderr=None)


@pytest.fixture
def service(tmp_path: Path) -> BackupService:
    return BackupService("postgresql://unit:unit@never-connect.invalid/unit", str(tmp_path))


@pytest.fixture
def pg_tools(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], dict]]:
    calls: list[tuple[list[str], dict]] = []

    def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
        calls.append((cmd, kwargs))
        return tool_result(cmd, kwargs)

    monkeypatch.setattr(backup_module.subprocess, "run", run)
    return calls


@pytest.fixture(autouse=True)
def private_files(service: BackupService, monkeypatch: pytest.MonkeyPatch):
    original = tempfile.TemporaryFile
    created = []

    def create(*args, **kwargs):
        private = str(kwargs.get("prefix", "")).startswith(".cga-")
        if private:
            assert Path(kwargs["dir"]).resolve() == service._backup_dir.resolve()
        file = original(*args, **kwargs)
        if private:
            if os.name != "nt":
                assert stat.S_IMODE(os.fstat(file.fileno()).st_mode) & 0o077 == 0
            created.append(file)
        return file

    monkeypatch.setattr(tempfile, "TemporaryFile", create)
    yield created
    assert all(file.closed for file in created), "Private staging handles must close on every path"
    assert not list(service._backup_dir.glob(".cga-*")), "Private SQL files must be removed"


def snapshot(service: BackupService, name: str, content: bytes = SELECTED_SQL) -> Path:
    path = service._backup_dir / name
    path.write_bytes(gzip.compress(content))
    return path


@pytest.mark.parametrize("name", ["auth-latest.sql.gz", "auth-20000101T000000Z.sql.gz"])
async def test_restore_freezes_selected_source_before_safety_backup(
    service: BackupService, pg_tools: list, name: str
) -> None:
    source = snapshot(service, name)
    service.update_config({"keep_count": 1})
    os.utime(source, (1, 1))

    result = await service.restore(name)

    assert next(kwargs["_test_stdin"] for cmd, kwargs in pg_tools if cmd[0] == "psql") == SELECTED_SQL
    assert next(kwargs["stdin"] for cmd, kwargs in pg_tools if cmd[0] == "psql").closed
    assert gzip.decompress(source.read_bytes()) == SELECTED_SQL
    assert result["restored_from"] == name
    assert result["pre_restore_snapshot"] != name
    assert gzip.decompress((service._backup_dir / result["pre_restore_snapshot"]).read_bytes()) == CURRENT_SQL


async def test_restore_same_second_does_not_overwrite_source(
    service: BackupService, pg_tools: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backup_module, "_iso_now", lambda: "20260914T120000Z")
    source = snapshot(service, "auth-20260914T120000Z.sql.gz")

    result = await service.restore(source.name)

    assert result["pre_restore_snapshot"] != source.name
    assert gzip.decompress(source.read_bytes()) == SELECTED_SQL
    assert next(kwargs["_test_stdin"] for cmd, kwargs in pg_tools if cmd[0] == "psql") == SELECTED_SQL


async def test_restore_uses_frozen_file_if_external_writer_replaces_source(
    service: BackupService, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = snapshot(service, "auth-latest.sql.gz")
    restored = []

    def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
        if cmd[0] == "pg_dump":
            source.write_bytes(gzip.compress(b"-- external replacement"))
        result = tool_result(cmd, kwargs)
        if cmd[0] == "psql":
            restored.append(kwargs["_test_stdin"])
        return result

    monkeypatch.setattr(backup_module.subprocess, "run", run)
    await service.restore(source.name)
    assert restored == [SELECTED_SQL]


async def test_restore_fails_on_sql_errors_with_single_transaction_and_stop_on_error(
    service: BackupService, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = snapshot(service, "auth-20260914T120000Z.sql.gz")
    psql_commands = []

    def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
        if cmd[0] == "pg_dump":
            return tool_result(cmd, kwargs)
        psql_commands.append(cmd)
        fail_fast = any("ON_ERROR_STOP" in arg for arg in cmd)
        return tool_result(
            cmd, kwargs, 3 if fail_fast else 0, stderr=b"ERROR: deliberately invalid SQL"
        )

    monkeypatch.setattr(backup_module.subprocess, "run", run)
    with pytest.raises(BackupError, match="psql restore failed:.*invalid SQL"):
        await service.restore(source.name)
    assert "--single-transaction" in psql_commands[0]
    assert "--file=-" in psql_commands[0]
    assert "--no-psqlrc" in psql_commands[0]
    assert any(arg in {"--set=ON_ERROR_STOP=1", "--set=ON_ERROR_STOP=on"} for arg in psql_commands[0])
    assert gzip.decompress(source.read_bytes()) == SELECTED_SQL
    assert not (service._backup_dir / ".auth.lock").exists()


@pytest.mark.parametrize("content", [b"not gzip", gzip.compress(SELECTED_SQL)[:-5]])
async def test_corrupt_snapshot_fails_before_any_database_command(
    service: BackupService, pg_tools: list, content: bytes
) -> None:
    source = service._backup_dir / "auth-20260914T120000Z.sql.gz"
    source.write_bytes(content)

    with pytest.raises(BackupError, match="snapshot.*(read|invalid|corrupt)"):
        await service.restore(source.name)
    assert pg_tools == []


async def test_missing_snapshot_is_not_misreported_as_missing_psql(
    service: BackupService, pg_tools: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = service._backup_dir / "auth-20260914T120000Z.sql.gz"
    monkeypatch.setattr(service, "snapshot_path", lambda name: missing)
    with pytest.raises(BackupError) as error:
        await service.restore(missing.name)
    assert "psql not installed" not in str(error.value)
    assert "snapshot" in str(error.value)
    assert pg_tools == []


async def test_missing_psql_is_reported_only_for_psql_spawn(
    service: BackupService, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = snapshot(service, "auth-20260914T120000Z.sql.gz")

    def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
        if cmd[0] == "psql":
            raise FileNotFoundError("psql")
        return tool_result(cmd, kwargs)

    monkeypatch.setattr(backup_module.subprocess, "run", run)
    with pytest.raises(BackupError, match="psql not installed"):
        await service.restore(source.name)


async def test_failed_safety_dump_never_runs_psql_or_modifies_good_backup(
    service: BackupService, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = snapshot(service, "auth-latest.sql.gz")
    calls = []

    def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
        calls.append(cmd[0])
        return tool_result(cmd, kwargs, 1, b"partial dump", b"connection refused")

    monkeypatch.setattr(backup_module.subprocess, "run", run)
    with pytest.raises(BackupError, match="pre-restore safety.*connection refused"):
        await service.restore(source.name)
    assert calls == ["pg_dump"]
    assert gzip.decompress(source.read_bytes()) == SELECTED_SQL
    assert service.list_snapshots() == []


async def test_backup_failure_keeps_good_snapshot_and_latest(
    service: BackupService, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = snapshot(service, "auth-20260914T120000Z.sql.gz")
    latest = snapshot(service, "auth-latest.sql.gz")
    monkeypatch.setattr(backup_module, "_iso_now", lambda: "20260914T120000Z")
    monkeypatch.setattr(
        backup_module.subprocess,
        "run",
        lambda cmd, **kwargs: tool_result(cmd, kwargs, 1, b"partial", b"disk read error"),
    )
    with pytest.raises(BackupError, match="disk read error"):
        await service.run_backup()
    assert gzip.decompress(old.read_bytes()) == SELECTED_SQL
    assert gzip.decompress(latest.read_bytes()) == SELECTED_SQL
    assert "disk read error" in service.status()["last_run_error"]
    assert not list(service._backup_dir.glob("*.tmp"))


async def test_two_backups_in_same_second_are_distinct(
    service: BackupService, pg_tools: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backup_module, "_iso_now", lambda: "20260914T120000Z")
    first = await service.run_backup()
    second = await service.run_backup()
    assert first["name"] != second["name"]
    assert len(service.list_snapshots()) == 2


@pytest.mark.parametrize("content", [b"", b" \t\r\n" * 300_000], ids=["empty", "whitespace-multiple-chunks"])
async def test_empty_dump_is_rejected_before_safety_backup(
    service: BackupService, pg_tools: list, content: bytes
) -> None:
    source = snapshot(service, "auth-20260914T120000Z.sql.gz", content)
    with pytest.raises(BackupError, match="snapshot is empty"):
        await service.restore(source.name)
    assert pg_tools == []


async def test_backup_filesystem_error_is_not_misreported_as_missing_pg_dump(
    service: BackupService, pg_tools: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_open = Path.open

    def failing_open(path, mode="r", *args, **kwargs):
        if mode == "xb":
            raise FileNotFoundError("backup storage disconnected")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failing_open)
    with pytest.raises(BackupError, match="backup write failed:.*storage disconnected"):
        await service.run_backup()
    assert pg_tools == []
    assert not (service._backup_dir / ".auth.lock").exists()


async def test_latest_publication_failure_does_not_prune_previous_good(
    service: BackupService, pg_tools: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = snapshot(service, "auth-20000101T000000Z.sql.gz")
    latest = snapshot(service, "auth-latest.sql.gz")
    service.update_config({"keep_count": 1})
    original_replace = Path.replace

    def failing_replace(path, target):
        if Path(target) == latest:
            raise OSError("latest publication unavailable")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", failing_replace)
    with pytest.raises(BackupError, match="latest update failed"):
        await service.run_backup()
    assert gzip.decompress(latest.read_bytes()) == SELECTED_SQL
    assert gzip.decompress(source.read_bytes()) == SELECTED_SQL
    assert len(service.list_snapshots()) == 2
    assert service.status()["last_run_status"] == "error"


async def test_latest_is_never_written_in_place(
    service: BackupService, pg_tools: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    latest = snapshot(service, "auth-latest.sql.gz")
    real_copy = backup_module.shutil.copyfile

    def copy(src, dst, *args, **kwargs):
        assert Path(dst) != latest, "Publication must replace latest atomically, not truncate it"
        return real_copy(src, dst, *args, **kwargs)

    monkeypatch.setattr(backup_module.shutil, "copyfile", copy)
    await service.run_backup()
    assert gzip.decompress(latest.read_bytes()) == CURRENT_SQL


@pytest.mark.parametrize("other_instance", [False, True])
async def test_delete_waits_for_restore_and_source_is_not_removed_mid_restore(
    service: BackupService, monkeypatch: pytest.MonkeyPatch, other_instance: bool
) -> None:
    source = snapshot(service, "auth-20260914T120000Z.sql.gz")
    deleter = BackupService(service._dsn, str(service._backup_dir)) if other_instance else service
    entered = threading.Event()
    release = threading.Event()

    def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
        if cmd[0] == "psql":
            entered.set()
            assert release.wait(5), "test did not release mocked psql"
        return tool_result(cmd, kwargs)

    monkeypatch.setattr(backup_module.subprocess, "run", run)
    restore = asyncio.create_task(service.restore(source.name))
    delete = None
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        delete = asyncio.create_task(deleter.delete(source.name))
        await asyncio.sleep(0.15)
        assert not delete.done()
        assert source.is_file()
    finally:
        release.set()
        await restore
        if delete is not None:
            await delete
    assert not source.exists()


async def test_cancelling_request_does_not_release_worker_filesystem_lock(
    service: BackupService, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = snapshot(service, "auth-20260914T120000Z.sql.gz")
    entered = threading.Event()
    release = threading.Event()
    active_streams = []

    def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
        if cmd[0] == "psql":
            active_streams.append(kwargs["stdin"])
            entered.set()
            assert release.wait(5), "test did not release mocked psql"
        return tool_result(cmd, kwargs)

    monkeypatch.setattr(backup_module.subprocess, "run", run)
    restore = asyncio.create_task(service.restore(source.name))
    delete = None
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        restore.cancel()
        with pytest.raises(asyncio.CancelledError):
            await restore
        delete = asyncio.create_task(service.delete(source.name))
        await asyncio.sleep(0.15)
        assert not delete.done()
        assert source.is_file()
    finally:
        release.set()
        if not restore.done():
            await restore
        if delete is not None:
            await delete
    assert active_streams and all(stream.closed for stream in active_streams)


async def test_external_sidecar_lock_blocks_backup_without_stealing_it(
    service: BackupService, pg_tools: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = service._backup_dir / ".auth.lock"
    lock.mkdir()
    monkeypatch.setenv("BACKUP_LOCK_TIMEOUT_SECONDS", "0")
    with pytest.raises(BackupError, match="lock"):
        await service.run_backup()
    assert pg_tools == []
    assert lock.is_dir()


async def test_restore_reads_bounded_chunks_and_fsyncs_before_safety_dump(
    service: BackupService, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"SELECT 'bounded';\n" * 140_000
    source = snapshot(service, "auth-20260914T120000Z.sql.gz", content)
    original_read = gzip.GzipFile.read
    original_fsync = os.fsync
    read_sizes = []
    synced = set()
    restored = []

    def bounded_read(file, size=-1):
        assert 0 < size <= 1024 * 1024, "Never decompress the entire dump into memory"
        read_sizes.append(size)
        return original_read(file, size)

    def fsync(fd):
        info = os.fstat(fd)
        synced.add((info.st_dev, info.st_ino, info.st_size))
        return original_fsync(fd)

    def run(cmd, **kwargs):
        assert any(size == len(content) for _, _, size in synced), "Freeze must be fsynced before pg_dump"
        if cmd[0] == "psql":
            info = os.fstat(kwargs["stdin"].fileno())
            assert (info.st_dev, info.st_ino, info.st_size) in synced
            restored.append(kwargs["stdin"])
        return tool_result(cmd, kwargs)

    monkeypatch.setattr(gzip.GzipFile, "read", bounded_read)
    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(backup_module.subprocess, "run", run)
    await service.restore(source.name)
    assert len(read_sizes) >= 4
    assert restored[0].closed


async def test_frozen_descriptor_can_be_passed_to_a_real_local_child_process(
    service: BackupService, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = snapshot(service, "auth-20260914T120000Z.sql.gz")
    original_run = subprocess.run

    def run(cmd, **kwargs):
        if cmd[0] == "pg_dump":
            return tool_result(cmd, kwargs)
        assert cmd[0] == "psql"
        # Exercise native stdin handle inheritance, never a real database tool.
        return original_run(
            [
                sys.executable, "-c",
                "import sys; assert sys.stdin.buffer.read() == sys.argv[1].encode('utf-8')",
                SELECTED_SQL.decode("utf-8"),
            ],
            stdin=kwargs["stdin"], stdout=kwargs["stdout"], stderr=kwargs["stderr"],
            check=False, timeout=10,
        )

    monkeypatch.setattr(backup_module.subprocess, "run", run)
    result = await service.restore(source.name)
    assert result["restored_from"] == source.name


async def test_crc_error_after_multiple_chunks_prevents_safety_dump(
    service: BackupService, pg_tools: list
) -> None:
    source = snapshot(service, "auth-20260914T120000Z.sql.gz", SELECTED_SQL * 80_000)
    encoded = bytearray(source.read_bytes())
    encoded[-8] ^= 1
    source.write_bytes(encoded)

    with pytest.raises(BackupError, match="snapshot read failed.*CRC"):
        await service.restore(source.name)
    assert pg_tools == []
    assert not (service._backup_dir / ".auth.lock").exists()


@pytest.mark.parametrize("failure", ["create", "write", "fsync"])
async def test_restore_staging_io_errors_do_not_run_database_commands(
    service: BackupService, pg_tools: list, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    source = snapshot(service, "auth-20260914T120000Z.sql.gz")
    original = tempfile.TemporaryFile

    class FullDisk:
        def __init__(self, file):
            self.file = file

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.file.close()

        def __getattr__(self, name):
            return getattr(self.file, name)

        def write(self, data):
            raise OSError(errno.ENOSPC, "injected staging disk full")

    def create(*args, **kwargs):
        if failure == "create":
            raise FileNotFoundError("injected staging directory unavailable")
        return FullDisk(original(*args, **kwargs))

    if failure == "fsync":
        def fail_fsync(fd):
            raise OSError(errno.ENOSPC, "injected staging disk full")
        monkeypatch.setattr(os, "fsync", fail_fsync)
    else:
        monkeypatch.setattr(tempfile, "TemporaryFile", create)

    with pytest.raises(BackupError, match="restore staging") as error:
        await service.restore(source.name)
    assert "not installed" not in str(error.value)
    assert pg_tools == []
    assert gzip.decompress(source.read_bytes()) == SELECTED_SQL
    assert not (service._backup_dir / ".auth.lock").exists()


async def test_failed_safety_backup_closes_frozen_readonly_descriptor(
    service: BackupService, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = snapshot(service, "auth-latest.sql.gz")
    opened = []
    original_fdopen = os.fdopen

    def fdopen(fd, mode="r", *args, **kwargs):
        stream = original_fdopen(fd, mode, *args, **kwargs)
        if mode == "rb":
            opened.append(stream)
        return stream

    monkeypatch.setattr(os, "fdopen", fdopen)
    monkeypatch.setattr(
        backup_module.subprocess, "run",
        lambda cmd, **kwargs: tool_result(cmd, kwargs, 1, stderr=b"injected safety failure"),
    )
    with pytest.raises(BackupError, match="pre-restore safety.*injected safety failure"):
        await service.restore(source.name)
    assert len(opened) == 1 and opened[0].closed


async def test_large_restore_and_safety_dump_use_bounded_python_memory(
    service: BackupService, monkeypatch: pytest.MonkeyPatch
) -> None:
    if tracemalloc.is_tracing():
        pytest.skip("Standalone allocation measurement must not reset an existing profiler")
    block = (b"-- synthetic SQL for streaming regression\n").ljust(64 * 1024, b" ")
    block_count = 512
    source = service._backup_dir / "auth-20260914T120000Z.sql.gz"
    digest = hashlib.sha256()
    with gzip.open(source, "wb") as compressed:
        for _ in range(block_count):
            compressed.write(block)
            digest.update(block)
    expected_digest = digest.digest()
    total_bytes = len(block) * block_count
    commands = []

    def run(cmd, **kwargs):
        commands.append(cmd[0])
        assert "input" not in kwargs
        assert kwargs["stderr"] != subprocess.PIPE
        if cmd[0] == "pg_dump":
            assert kwargs["stdout"] != subprocess.PIPE
            kwargs["stdout"].write(DUMP_HEADER)
            for _ in range(block_count):
                kwargs["stdout"].write(block)
        else:
            assert kwargs["stdout"] == subprocess.DEVNULL
            stream = kwargs["stdin"]
            assert not stream.writable()
            assert os.fstat(stream.fileno()).st_size == total_bytes
            restored_digest = hashlib.sha256()
            for chunk in iter(lambda: stream.read(64 * 1024), b""):
                restored_digest.update(chunk)
            assert restored_digest.digest() == expected_digest
        return subprocess.CompletedProcess(cmd, 0, None, None)

    monkeypatch.setattr(backup_module.subprocess, "run", run)
    tracemalloc.start()
    try:
        await service.restore(source.name)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert commands == ["pg_dump", "psql"]
    assert peak < 16 * 1024 * 1024, f"32 MiB restore + safety dump allocated {peak} Python bytes"


@pytest.mark.parametrize("failing_tool", ["pg_dump", "psql"])
async def test_tool_stderr_is_disk_backed_and_error_text_is_bounded(
    service: BackupService, monkeypatch: pytest.MonkeyPatch, failing_tool: str
) -> None:
    source = snapshot(service, "auth-20260914T120000Z.sql.gz")

    def run(cmd, **kwargs):
        if cmd[0] == failing_tool:
            assert kwargs["stderr"] != subprocess.PIPE
            kwargs["stderr"].write(b"injected error: " + b"x" * (128 * 1024))
            return subprocess.CompletedProcess(cmd, 1, None, None)
        return tool_result(cmd, kwargs)

    monkeypatch.setattr(backup_module.subprocess, "run", run)
    with pytest.raises(BackupError, match="injected error:") as error:
        await service.restore(source.name)
    assert len(str(error.value)) < 70_000
    assert "truncated" in str(error.value)


@pytest.mark.parametrize("output", [
    b"",
    b"-- Dumped from database version 16.12\n-- Dumped by pg_dump version 17.10\n",
])
async def test_incompatible_dump_is_not_published(service, monkeypatch, output):
    latest = snapshot(service, "auth-latest.sql.gz")
    monkeypatch.setattr(
        backup_module.subprocess, "run",
        lambda cmd, **kwargs: tool_result(cmd, kwargs, stdout=output),
    )

    with pytest.raises(BackupError, match="version"):
        await service.run_backup()

    assert gzip.decompress(latest.read_bytes()) == SELECTED_SQL
    assert service.list_snapshots() == []
