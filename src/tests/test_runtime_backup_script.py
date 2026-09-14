from __future__ import annotations

import gzip
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "src" / "scripts" / "backup-runtime-data.sh"


@pytest.fixture
def shell() -> str:
    executable = shutil.which("sh")
    if executable:
        return executable
    git_shell = Path(r"C:\Program Files\Git\bin\sh.exe")
    if git_shell.is_file():
        return str(git_shell)
    pytest.skip("An existing POSIX sh is needed for the runtime backup script tests")


@pytest.fixture
def runtime(tmp_path: Path) -> dict:
    root = tmp_path / "backup root"
    auth = root / "unit" / "auth"
    graph = root / "unit" / "falkordb"
    data = tmp_path / "graph volume"
    commands = tmp_path / "commands"
    for directory in (auth, graph, data, commands):
        directory.mkdir(parents=True)
    (auth / "auth-20000101T000000Z.sql.gz").write_bytes(gzip.compress(b"-- previous good"))
    (auth / "auth-latest.sql.gz").write_bytes(gzip.compress(b"-- previous good"))
    (graph / "falkordb-20000101T000000Z.tgz").write_bytes(b"previous good archive")
    (graph / "falkordb-latest.tgz").write_bytes(b"previous good archive")
    (data / "dump.rdb").write_bytes(b"REDIS0011stale-snapshot")
    (data / "appendonly.aof").write_bytes(b"live AOF must not be archived")
    (data / "temp-rewrite.rdb").write_bytes(b"incomplete live snapshot")

    def command(name: str, body: str) -> None:
        path = commands / name
        path.write_text("#!/bin/sh\n" + body, encoding="utf-8", newline="\n")
        path.chmod(0o755)

    command(
        "pg_dump",
        """if [ "${FAKE_DUMP_FAIL:-0}" = 1 ]; then
  printf '%s\\n' 'partial SQL'
  printf '%s\\n' 'injected pg_dump connection failure' >&2
  exit 44
fi
printf '%s\\n' '-- new good SQL'
""",
    )
    command(
        "nc",
        """request=$(cat)
case "$request" in *AUTH*) printf '%s\\r\\n' '+OK';; esac
case "$request" in
  *CONFIG*dir*)
    value=${FAKE_SERVER_DIR:-/var/lib/falkordb/data}
    printf '*2\\r\\n$3\\r\\ndir\\r\\n$%s\\r\\n%s\\r\\n+OK\\r\\n' "${#value}" "$value"
    ;;
  *CONFIG*dbfilename*)
    value=${FAKE_RDB_NAME:-dump.rdb}
    printf '*2\\r\\n$10\\r\\ndbfilename\\r\\n$%s\\r\\n%s\\r\\n+OK\\r\\n' "${#value}" "$value"
    ;;
  *SAVE*)
    if [ "${FAKE_SAVE_FAIL:-0}" = 1 ]; then
      printf '%s\\r\\n' '-ERR injected RDB save failure' '+OK'
    elif [ "${FAKE_SAVE_INCOMPLETE:-0}" = 1 ]; then
      printf '%s\\r\\n' '+OK'
    elif [ "${FAKE_SAVE_NO_REPLACE:-0}" = 1 ]; then
      printf '%s\\r\\n' '+OK' '+OK'
    else
      printf '%s' 'REDIS0011fresh-snapshot' > "$FALKORDB_DATA_DIR/saving.rdb"
      mv "$FALKORDB_DATA_DIR/saving.rdb" "$FALKORDB_DATA_DIR/dump.rdb"
      printf '%s\\r\\n' '+OK' '+OK'
    fi
    ;;
  *) printf '%s\\r\\n' '-ERR unexpected test command' '+OK';;
esac
""",
    )
    # Bound the old infinite-loop implementation as well as the fixed one-shot path.
    command("sleep", "exit 91\n")
    env = os.environ.copy()
    env.update(
        {
            "PATH": str(commands) + os.pathsep + env.get("PATH", ""),
            "BACKUP_ROOT": root.name,
            "BACKUP_STACK_NAME": "unit",
            "BACKUP_KEEP_COUNT": "2",
            "BACKUP_RUN_ONCE": "1",
            "BACKUP_LOCK_TIMEOUT_SECONDS": "0",
            "FALKORDB_DATA_DIR": data.name,
            "FALKORDB_HOST": "never-connect.invalid",
            "FALKORDB_PORT": "6379",
            "FALKORDB_PASSWORD": "",
            "PGHOST": "never-connect.invalid",
            "PGPASSWORD": "test-only",
            "LC_ALL": "C",
        }
    )
    return {
        "cwd": tmp_path, "root": root, "auth": auth, "graph": graph,
        "data": data, "env": env, "command": command,
    }


def run_backup(shell: str, runtime: dict, **variables: str) -> subprocess.CompletedProcess:
    env = {**runtime["env"], "BACKUP_TEST_SCRIPT": str(SCRIPT), **variables}
    return subprocess.run(
        # Git for Windows prepends its own utilities during shell startup.
        # Put the isolated command doubles first after startup, before sourcing.
        [shell, "-c", 'PATH="$PWD/commands:$PATH"; export PATH; . "$BACKUP_TEST_SCRIPT"'],
        cwd=runtime["cwd"],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def assert_previous_auth_backup(runtime: dict) -> None:
    assert gzip.decompress((runtime["auth"] / "auth-latest.sql.gz").read_bytes()) == b"-- previous good"
    assert len(list(runtime["auth"].glob("auth-*.sql.gz"))) == 2


def assert_previous_graph_backup(runtime: dict) -> None:
    assert (runtime["graph"] / "falkordb-latest.tgz").read_bytes() == b"previous good archive"
    assert len(list(runtime["graph"].glob("falkordb-*.tgz"))) == 2


def test_pg_dump_failure_is_visible_and_keeps_previous_good_and_latest(shell: str, runtime: dict) -> None:
    result = run_backup(shell, runtime, FAKE_DUMP_FAIL="1")
    assert result.returncode != 0
    assert "injected pg_dump connection failure" in result.stderr
    assert_previous_auth_backup(runtime)
    assert not (runtime["auth"] / ".auth.lock").exists()


def test_gzip_failure_keeps_previous_good_and_latest(shell: str, runtime: dict) -> None:
    runtime["command"]("gzip", "printf broken; echo 'injected gzip failure' >&2; exit 23\n")
    result = run_backup(shell, runtime)
    assert result.returncode != 0
    assert "injected gzip failure" in result.stderr
    assert_previous_auth_backup(runtime)


def test_successful_backup_counts_only_immutable_snapshots_for_retention(shell: str, runtime: dict) -> None:
    result = run_backup(shell, runtime)
    assert result.returncode == 0, result.stdout + result.stderr
    assert gzip.decompress((runtime["auth"] / "auth-latest.sql.gz").read_bytes()) == b"-- new good SQL\n"
    assert (runtime["auth"] / "auth-20000101T000000Z.sql.gz").is_file()
    assert len(list(runtime["auth"].glob("auth-*.sql.gz"))) == 3
    assert not (runtime["auth"] / ".auth.lock").exists()


def test_graph_backup_requests_save_and_archives_only_frozen_rdb(shell: str, runtime: dict) -> None:
    result = run_backup(shell, runtime)
    assert result.returncode == 0, result.stdout + result.stderr
    with tarfile.open(runtime["graph"] / "falkordb-latest.tgz", "r:gz") as archive:
        assert archive.getnames() == ["dump.rdb"]
        dump = archive.extractfile("dump.rdb")
        assert dump is not None
        assert dump.read() == b"REDIS0011fresh-snapshot"
    assert not (runtime["graph"] / ".falkordb.lock").exists()


def test_first_graph_snapshot_does_not_require_a_previous_rdb(shell: str, runtime: dict) -> None:
    (runtime["data"] / "dump.rdb").unlink()
    result = run_backup(shell, runtime)
    assert result.returncode == 0, result.stdout + result.stderr
    with tarfile.open(runtime["graph"] / "falkordb-latest.tgz", "r:gz") as archive:
        assert archive.getnames() == ["dump.rdb"]


def test_graph_snapshot_can_authenticate_without_printing_password(shell: str, runtime: dict) -> None:
    password = "only-a-test secret with spaces"
    result = run_backup(shell, runtime, FALKORDB_PASSWORD=password)
    assert result.returncode == 0, result.stdout + result.stderr
    assert password not in result.stdout + result.stderr


def test_graph_network_failure_retains_previous_archive(shell: str, runtime: dict) -> None:
    runtime["command"]("nc", "echo 'injected connection failure' >&2; exit 25\n")
    result = run_backup(shell, runtime)
    assert result.returncode != 0
    assert "injected connection failure" in result.stderr
    assert_previous_graph_backup(runtime)


@pytest.mark.parametrize("variable", ["FAKE_SAVE_FAIL", "FAKE_SAVE_INCOMPLETE"])
def test_graph_save_failure_never_publishes_stale_or_partial_archive(
    shell: str, runtime: dict, variable: str
) -> None:
    result = run_backup(shell, runtime, **{variable: "1"})
    assert result.returncode != 0
    assert "FalkorDB" in result.stderr
    assert_previous_graph_backup(runtime)


@pytest.mark.parametrize(
    "variables",
    [{"FAKE_SERVER_DIR": "/wrong-volume"}, {"FAKE_RDB_NAME": "another.rdb"}],
)
def test_graph_backup_rejects_server_volume_or_filename_mismatch(
    shell: str, runtime: dict, variables: dict
) -> None:
    result = run_backup(shell, runtime, **variables)
    assert result.returncode != 0
    assert "FalkorDB" in result.stderr
    assert_previous_graph_backup(runtime)


def test_save_ok_without_replacing_mounted_rdb_is_not_treated_as_fresh(shell: str, runtime: dict) -> None:
    result = run_backup(shell, runtime, FAKE_SAVE_NO_REPLACE="1")
    assert result.returncode != 0
    assert "FalkorDB" in result.stderr
    assert_previous_graph_backup(runtime)


def test_tar_failure_keeps_previous_graph_backup(shell: str, runtime: dict) -> None:
    runtime["command"]("tar", "echo 'injected tar failure' >&2; exit 29\n")
    result = run_backup(shell, runtime)
    assert result.returncode != 0
    assert "injected tar failure" in result.stderr
    assert_previous_graph_backup(runtime)


def test_existing_restore_lock_is_not_stolen_by_sidecar(shell: str, runtime: dict) -> None:
    lock = runtime["auth"] / ".auth.lock"
    lock.mkdir()
    result = run_backup(shell, runtime)
    assert result.returncode != 0
    assert "lock" in result.stderr
    assert lock.is_dir()
    assert_previous_auth_backup(runtime)


def test_failed_latest_rename_keeps_good_backup_without_pruning(shell: str, runtime: dict) -> None:
    runtime["command"](
        "mv",
        """case "$2" in
  */auth-latest.sql.gz) echo 'injected latest rename failure' >&2; exit 31;;
esac
exec /bin/mv "$@"
""",
    )
    result = run_backup(shell, runtime, BACKUP_KEEP_COUNT="1")
    assert result.returncode != 0
    assert "injected latest rename failure" in result.stderr
    assert gzip.decompress((runtime["auth"] / "auth-latest.sql.gz").read_bytes()) == b"-- previous good"
    assert (runtime["auth"] / "auth-20000101T000000Z.sql.gz").is_file()
    assert len(list(runtime["auth"].glob("auth-*.sql.gz"))) == 3


@pytest.mark.parametrize("keep", ["0", "-1", "not-a-number"])
def test_invalid_retention_fails_without_removing_previous_good(
    shell: str, runtime: dict, keep: str
) -> None:
    result = run_backup(shell, runtime, BACKUP_KEEP_COUNT=keep)
    assert result.returncode != 0
    assert_previous_auth_backup(runtime)
    assert_previous_graph_backup(runtime)
