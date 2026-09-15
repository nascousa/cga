"""Relay receipts must represent a complete committed PostgreSQL batch.

Set RELAY_SYNC_TEST_POSTGRES_DSN to a task-owned PostgreSQL instance. These
tests deliberately never fall back to the shared/default auth database.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from copy import deepcopy

import asyncpg
import pytest
from fastapi import HTTPException

from backend.cga_relay.storage import (
    MAX_SYNC_BYTES,
    _canonical_payload,
    get_sync_batch,
    list_sync_batches,
    load_sync_batch,
    save_sync_batch,
)


@pytest.fixture(scope="session")
def auth_pg_dsn():
    dsn = os.getenv("RELAY_SYNC_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set RELAY_SYNC_TEST_POSTGRES_DSN to a task-owned PostgreSQL instance")
    return dsn


def batch():
    content = "print('你好')\n"
    return {
        "agent_id": "relay-test",
        "project_id": "external-project",
        "namespace": "workspace",
        "root": "D:\\client\\checkout",
        "counts": {"files": 1},
        "snapshots": [{
            "path": "src/main.py",
            "content": content,
            "bytes": len(content.encode("utf-8")),
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "future_metadata": {"retain": True},
        }],
        "tombstones": ["src/deleted.py"],
    }


async def projects(pool):
    async with pool.acquire() as db:
        await db.execute(
            """INSERT INTO projects(id, project_name, project_id)
               VALUES (7, 'alpha', 'external-project'), (8, 'beta', 'other-project')"""
        )


@pytest.mark.parametrize("path", [
    "", ".", "..", "../outside", "src/../../outside", "/etc/passwd",
    "\\\\server\\share", "C:\\secret", "C:secret", "file:stream",
    "src\\..\\outside", "src//file", "src/./file", "file\x00.py",
    "src/\nfile", "src/.. /file", "src./file",
])
@pytest.mark.parametrize("kind", ["snapshots", "tombstones"])
async def test_rejects_unsafe_paths_before_database_access(path, kind):
    payload = batch()
    if kind == "snapshots":
        payload[kind][0]["path"] = path
    else:
        payload[kind] = [path]
    with pytest.raises(HTTPException) as error:
        await save_sync_batch(object(), 7, payload)
    assert error.value.status_code == 400


@pytest.mark.parametrize(("field", "value"), [
    ("bytes", -1), ("bytes", 0), ("bytes", True), ("bytes", "16"),
    ("sha256", "0" * 64), ("sha256", None), ("sha256", "xyz"),
    ("content", None), ("content", "\ud800"),
])
async def test_rejects_invalid_snapshot_integrity_before_storage(field, value):
    payload = batch()
    payload["snapshots"][0][field] = value
    with pytest.raises(HTTPException) as error:
        await save_sync_batch(object(), 7, payload)
    assert error.value.status_code == 400


@pytest.mark.parametrize("payload", [
    None, [], {"snapshots": None}, {"tombstones": {}}, {"snapshots": ["file"]},
    {"snapshots": [{}]}, {"tombstones": [42]}, {"metadata": float("nan")},
    {"metadata": object()},
])
async def test_rejects_malformed_payload(payload):
    with pytest.raises(HTTPException) as error:
        await save_sync_batch(object(), 7, payload)
    assert error.value.status_code == 400


@pytest.mark.parametrize("project_id", [None, 0, -1, True, "7"])
async def test_requires_internal_positive_project_id(project_id):
    with pytest.raises(HTTPException) as error:
        await save_sync_batch(object(), project_id, batch())
    assert error.value.status_code == 400


@pytest.mark.parametrize("conflict", ["snapshot", "tombstone", "both", "separator-alias"])
async def test_conflicting_paths_are_explicit_errors(conflict):
    payload = batch()
    if conflict == "snapshot":
        payload["snapshots"].append(deepcopy(payload["snapshots"][0]))
    elif conflict == "tombstone":
        payload["tombstones"].append(payload["tombstones"][0])
    else:
        payload["tombstones"].append(
            "src\\main.py" if conflict == "separator-alias" else "src/main.py"
        )
    with pytest.raises(HTTPException) as error:
        await save_sync_batch(object(), 7, payload)
    assert error.value.status_code == 409


async def test_combined_item_limit_is_independent_of_reported_counts():
    payload = batch()
    payload["counts"] = {"files": 0}
    payload["tombstones"] = [f"gone/{i}.py" for i in range(499)]
    assert _canonical_payload(payload)
    payload["tombstones"].append("gone/500.py")
    with pytest.raises(HTTPException) as error:
        await save_sync_batch(object(), 7, payload)
    assert error.value.status_code == 413


async def test_utf8_json_limit_includes_metadata_and_escaping():
    payload = {"root": ""}
    overhead = len(_canonical_payload(payload).encode("utf-8"))
    payload["root"] = "a" * (MAX_SYNC_BYTES - overhead)
    assert len(_canonical_payload(payload).encode("utf-8")) == MAX_SYNC_BYTES
    for suffix in ["a", "你", "\n"]:
        oversized = {"root": payload["root"] + suffix}
        with pytest.raises(HTTPException) as error:
            await save_sync_batch(object(), 7, oversized)
        assert error.value.status_code == 413


async def test_complete_payload_survives_release_and_retry(auth_pg_pool):
    await projects(auth_pg_pool)
    payload = batch()
    payload["batch_id"] = "untrusted-client-id"
    async with auth_pg_pool.acquire() as db:
        receipt = await save_sync_batch(db, 7, payload)
        assert not db.raw.is_in_transaction()
        async with auth_pg_pool.acquire() as observer:
            async with observer.execute(
                "SELECT payload_json FROM relay_sync_batches WHERE batch_id = ?",
                (receipt["batch_id"],),
            ) as cur:
                assert json.loads((await cur.fetchone())["payload_json"]) == payload
    async with auth_pg_pool.acquire() as db:
        retry = await save_sync_batch(db, 7, dict(reversed(list(payload.items()))))
        async with db.execute("SELECT * FROM relay_sync_batches") as cur:
            rows = await cur.fetchall()
    assert receipt == retry
    assert receipt["accepted"] is True and receipt["durable"] is True
    assert len(receipt["batch_id"]) == 64
    assert receipt["batch_id"] != payload["batch_id"]
    assert len(rows) == 1 and rows[0]["project_db_id"] == 7
    assert json.loads(rows[0]["payload_json"]) == payload


@pytest.mark.parametrize("payload", [
    {"agent_id": "relay", "project_id": "external-project"},
    {"agent_id": "relay", "project_id": "external-project", "tombstones": ["gone.py"]},
])
async def test_empty_and_tombstone_only_batches_are_recoverable(auth_pg_pool, payload):
    await projects(auth_pg_pool)
    async with auth_pg_pool.acquire() as db:
        receipt = await save_sync_batch(db, 7, payload)
        async with db.execute(
            "SELECT payload_json FROM relay_sync_batches WHERE project_db_id = ? AND batch_id = ?",
            (7, receipt["batch_id"]),
        ) as cur:
            assert json.loads((await cur.fetchone())["payload_json"]) == payload


async def test_startup_schema_upgrade_is_idempotent_and_preserves_batches(auth_pg_pool):
    from backend.auth.database import _CREATE_TABLES

    await projects(auth_pg_pool)
    async with auth_pg_pool.acquire() as db:
        receipt = await save_sync_batch(db, 7, batch())
        await db.executescript(_CREATE_TABLES)
        assert await save_sync_batch(db, 7, batch()) == receipt


async def test_project_isolation_and_changed_payload_have_distinct_receipts(auth_pg_pool):
    await projects(auth_pg_pool)
    payload = batch()
    async with auth_pg_pool.acquire() as db:
        first = await save_sync_batch(db, 7, payload)
        other_project = await save_sync_batch(db, 8, payload)
        payload["tombstones"].append("another.py")
        changed = await save_sync_batch(db, 7, payload)
        async with db.execute(
            "SELECT count(*) AS n FROM relay_sync_batches WHERE project_db_id = ?", (7,),
        ) as cur:
            assert (await cur.fetchone())["n"] == 2
    assert len({first["batch_id"], other_project["batch_id"], changed["batch_id"]}) == 3


async def test_concurrent_duplicate_batches_commit_once(auth_pg_pool):
    await projects(auth_pg_pool)

    async def store():
        async with auth_pg_pool.acquire() as db:
            return await save_sync_batch(db, 7, batch())

    receipts = await asyncio.gather(*(store() for _ in range(6)))
    assert all(receipt == receipts[0] for receipt in receipts)
    async with auth_pg_pool.acquire() as db:
        async with db.execute("SELECT count(*) AS n FROM relay_sync_batches") as cur:
            assert (await cur.fetchone())["n"] == 1


async def test_stored_hash_collision_is_explicit_conflict(auth_pg_pool):
    await projects(auth_pg_pool)
    async with auth_pg_pool.acquire() as db:
        receipt = await save_sync_batch(db, 7, batch())
        await db.execute(
            "UPDATE relay_sync_batches SET payload_json = ? WHERE batch_id = ?",
            ('{"different":"content"}', receipt["batch_id"]),
        )
        with pytest.raises(HTTPException) as error:
            await save_sync_batch(db, 7, batch())
        assert error.value.status_code == 409
        async with db.execute("SELECT payload_json FROM relay_sync_batches") as cur:
            assert (await cur.fetchone())["payload_json"] == '{"different":"content"}'


async def test_insert_failure_is_not_acknowledged(auth_pg_pool):
    async with auth_pg_pool.acquire() as db:
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await save_sync_batch(db, 7, batch())
        assert not db.raw.is_in_transaction()
        async with db.execute("SELECT count(*) AS n FROM relay_sync_batches") as cur:
            assert (await cur.fetchone())["n"] == 0


async def test_actual_commit_failure_rolls_back_without_receipt(auth_pg_pool):
    await projects(auth_pg_pool)
    async with auth_pg_pool.acquire() as db:
        await db.executescript("""
            CREATE FUNCTION reject_relay_commit() RETURNS trigger AS $$
            BEGIN RAISE EXCEPTION 'injected relay commit failure'; END;
            $$ LANGUAGE plpgsql;
            CREATE CONSTRAINT TRIGGER reject_relay_commit
            AFTER INSERT ON relay_sync_batches DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION reject_relay_commit();
        """)
        with pytest.raises(asyncpg.RaiseError, match="injected relay commit failure"):
            await save_sync_batch(db, 7, batch())
        assert not db.raw.is_in_transaction()
        async with db.execute("SELECT count(*) AS n FROM relay_sync_batches") as cur:
            assert (await cur.fetchone())["n"] == 0
        await db.execute("DROP TRIGGER reject_relay_commit ON relay_sync_batches")
        assert (await save_sync_batch(db, 7, batch()))["durable"] is True


async def test_nested_transaction_cannot_claim_durability(auth_pg_pool):
    await projects(auth_pg_pool)
    async with auth_pg_pool.acquire() as db:
        async with db.raw.transaction():
            with pytest.raises(RuntimeError, match="own committed transaction"):
                await save_sync_batch(db, 7, batch())
            async with db.execute("SELECT count(*) AS n FROM relay_sync_batches") as cur:
                assert (await cur.fetchone())["n"] == 0


async def test_synchronous_commit_is_enabled_only_for_sync_transaction(auth_pg_pool):
    await projects(auth_pg_pool)
    async with auth_pg_pool.acquire() as db:
        await db.execute("SET synchronous_commit = off")
        await db.executescript("""
            CREATE FUNCTION require_sync_commit() RETURNS trigger AS $$
            BEGIN
                IF current_setting('synchronous_commit') <> 'on' THEN
                    RAISE EXCEPTION 'relay commit must be synchronous';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER require_sync_commit BEFORE INSERT ON relay_sync_batches
            FOR EACH ROW EXECUTE FUNCTION require_sync_commit();
        """)
        assert (await save_sync_batch(db, 7, batch()))["durable"] is True
        async with db.execute("SHOW synchronous_commit") as cur:
            assert (await cur.fetchone())["synchronous_commit"] == "off"
        await db.execute("RESET synchronous_commit")


async def test_replay_is_project_scoped_paginated_and_nonconsuming(auth_pg_pool):
    await projects(auth_pg_pool)
    async with auth_pg_pool.acquire() as db:
        first = await save_sync_batch(db, 7, batch())
        foreign = await save_sync_batch(db, 8, batch())
        second = await save_sync_batch(db, 7, {"tombstones": ["gone.py"]})
        page = await list_sync_batches(db, 7, limit=1)
        assert len(page) == 1 and page[0]["batch_id"] == first["batch_id"]
        assert "payload" not in page[0] and "payload_json" not in page[0]
        next_page = await list_sync_batches(db, 7, after_id=page[0]["id"])
        assert [row["batch_id"] for row in next_page] == [second["batch_id"]]
        assert await list_sync_batches(db, 7, after_id=next_page[0]["id"]) == []
        assert await load_sync_batch(db, 7, foreign["batch_id"]) is None
        assert await load_sync_batch(db, 8, first["batch_id"]) is None
        assert await load_sync_batch(db, 7, "0" * 64) is None
        loaded = await load_sync_batch(db, 7, first["batch_id"])
        assert loaded == {**page[0], "payload": batch()}
        assert await get_sync_batch(db, 7, first["batch_id"]) == loaded
        assert await load_sync_batch(db, 7, first["batch_id"]) == loaded
        assert len(await list_sync_batches(db, 7)) == 2
        assert (await load_sync_batch(db, 7, second["batch_id"]))["payload"] == {"tombstones": ["gone.py"]}


@pytest.mark.parametrize("batch_id", ["", "a", "z" * 64, "A" * 64, None, 7])
async def test_replay_rejects_invalid_batch_ids(batch_id):
    with pytest.raises(HTTPException) as error:
        await load_sync_batch(object(), 7, batch_id)
    assert error.value.status_code == 400


@pytest.mark.parametrize(("after_id", "limit"), [
    (-1, 1), (True, 1), ("1", 1), (0, 0), (0, 101), (0, True), (0, "1"),
])
async def test_replay_rejects_invalid_pagination(after_id, limit):
    with pytest.raises(HTTPException) as error:
        await list_sync_batches(object(), 7, after_id=after_id, limit=limit)
    assert error.value.status_code == 400


async def test_replay_refuses_corrupt_stored_content(auth_pg_pool):
    await projects(auth_pg_pool)
    async with auth_pg_pool.acquire() as db:
        receipt = await save_sync_batch(db, 7, batch())
        await db.execute("UPDATE relay_sync_batches SET payload_json = '{}'")
        with pytest.raises(RuntimeError, match="does not match its receipt"):
            await load_sync_batch(db, 7, receipt["batch_id"])


async def test_replay_database_failures_propagate(auth_pg_pool):
    async with auth_pg_pool.acquire() as db:
        await db.execute("DROP TABLE relay_sync_batches")
        with pytest.raises(asyncpg.UndefinedTableError):
            await list_sync_batches(db, 7)
        with pytest.raises(asyncpg.UndefinedTableError):
            await load_sync_batch(db, 7, "0" * 64)


async def test_schema_upgrades_initial_batches_for_replay(auth_pg_pool):
    from backend.auth.database import _CREATE_TABLES

    await projects(auth_pg_pool)
    async with auth_pg_pool.acquire() as db:
        receipt = await save_sync_batch(db, 7, batch())
        await db.execute("ALTER TABLE relay_sync_batches DROP COLUMN id CASCADE")
        await db.executescript(_CREATE_TABLES)
        loaded = await load_sync_batch(db, 7, receipt["batch_id"])
        assert loaded["id"] > 0 and loaded["payload"] == batch()


async def test_project_write_lock_prevents_out_of_order_replay_ids(auth_pg_pool):
    await projects(auth_pg_pool)
    async with auth_pg_pool.acquire() as blocker:
        async with blocker.raw.transaction():
            await blocker.execute("SELECT id FROM projects WHERE id = 7 FOR NO KEY UPDATE")
            async with auth_pg_pool.acquire() as writer:
                await writer.execute("SET lock_timeout = '100ms'")
                try:
                    with pytest.raises(asyncpg.LockNotAvailableError):
                        await save_sync_batch(writer, 7, batch())
                    # Another project's durability is not blocked by this lock.
                    assert (await save_sync_batch(writer, 8, batch()))["durable"] is True
                finally:
                    await writer.execute("RESET lock_timeout")
        async with blocker.execute("SELECT count(*) AS n FROM relay_sync_batches WHERE project_db_id = 7") as cur:
            assert (await cur.fetchone())["n"] == 0
        first = await save_sync_batch(blocker, 7, batch())
        second = await save_sync_batch(blocker, 7, {"tombstones": ["later.py"]})
        assert [row["batch_id"] for row in await list_sync_batches(blocker, 7)] == [
            first["batch_id"], second["batch_id"],
        ]
