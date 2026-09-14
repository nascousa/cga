"""Queue recovery tests: no network services or shared persistent state."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from fnmatch import fnmatch
import json
from pathlib import Path
import shutil
import threading
from unittest.mock import AsyncMock, MagicMock
import uuid

from fastapi import HTTPException
import pytest
from redis.exceptions import WatchError

from backend.auth import access
from backend.auth.context import ProjectScope, bind_project_scope, branch_graph_name, require_project_scope
from backend.indexer import consumer as indexer_module
from backend.indexer import paths as indexer_paths
from backend.queue.models import IndexJob, JobType
from backend.queue import streams
from backend.queue.streams import JobConsumer, JobProducer, LeaseLostError
from backend.tools.producer import MCPProducer

_REAL_AUTHORIZED_JOB_ROOT = access.authorized_job_repo_root
_AUTHORIZED_ROOT = Path(r"D:\repos\authorized")


class FakeRedis:
    """In-memory Redis subset with clock, PEL, WATCH and EXEC error semantics."""

    def __init__(self):
        self.hashes = {}
        self.values = {}
        self.sets = defaultdict(set)
        self.expires = {}
        self.versions = defaultdict(int)
        self.messages = {}
        self.delivered = set()
        self.pending = {}
        self.now_ms = 100_000
        self.sequence = 0
        self.calls = []
        self.fail_next = {}
        self.before_execute = None
        self.closed = False

    def record(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        if name in self.fail_next:
            raise self.fail_next.pop(name)

    def advance(self, milliseconds):
        self.now_ms += milliseconds
        for key, expires_at in list(self.expires.items()):
            if expires_at <= self.now_ms:
                self.values.pop(key, None)
                self.hashes.pop(key, None)
                self.expires.pop(key, None)
                self.versions[key] += 1

    def pipeline(self, transaction=True):
        assert transaction
        return FakePipeline(self)

    async def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    async def hset(self, key, mapping):
        self.record("hset", key, mapping=dict(mapping))
        self.hashes.setdefault(key, {}).update({name: str(value) for name, value in mapping.items()})
        self.versions[key] += 1
        return len(mapping)

    async def hsetnx(self, key, field, value):
        self.record("hsetnx", key, field, value)
        if field in self.hashes.get(key, {}):
            return 0
        return await self.hset(key, {field: value})

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, px):
        self.record("set", key, value, px=px)
        self.values[key] = value
        self.expires[key] = self.now_ms + px
        self.versions[key] += 1
        return True

    async def delete(self, key):
        self.record("delete", key)
        self.values.pop(key, None)
        self.hashes.pop(key, None)
        self.expires.pop(key, None)
        self.versions[key] += 1
        return 1

    async def pexpire(self, key, milliseconds):
        self.record("pexpire", key, milliseconds)
        self.expires[key] = self.now_ms + milliseconds
        self.versions[key] += 1
        return True

    async def expire(self, key, seconds):
        self.record("expire", key, seconds)
        self.expires[key] = self.now_ms + seconds * 1000
        self.versions[key] += 1
        return True

    async def persist(self, key):
        self.record("persist", key)
        self.expires.pop(key, None)
        self.versions[key] += 1
        return True

    async def time(self):
        return self.now_ms // 1000, (self.now_ms % 1000) * 1000

    async def sadd(self, key, member):
        self.record("sadd", key, member)
        self.sets[key].add(member)
        return 1

    async def srem(self, key, member):
        self.record("srem", key, member)
        self.sets[key].discard(member)
        return 1

    async def sscan(self, key, cursor, count):
        members = sorted(self.sets[key])
        end = cursor + count
        return (end if end < len(members) else 0), members[cursor:end]

    async def scan(self, cursor, match, count):
        keys = sorted(key for key in self.hashes if fnmatch(key, match))
        end = cursor + count
        return (end if end < len(keys) else 0), keys[cursor:end]

    async def xadd(self, key, data, **kwargs):
        self.record("xadd", key, data, **kwargs)
        self.sequence += 1
        message_id = f"{self.now_ms}-{self.sequence}"
        self.messages[message_id] = dict(data)
        return message_id

    @staticmethod
    def _id(message_id):
        return tuple(int(part) for part in message_id.split("-"))

    async def xreadgroup(self, group, consumer, stream_ids, count, block):
        self.record("xreadgroup", group, consumer, stream_ids, count=count, block=block)
        messages = []
        for message_id in sorted(self.messages, key=self._id):
            if message_id in self.delivered:
                continue
            self.delivered.add(message_id)
            self.pending[message_id] = {
                "message_id": message_id, "consumer": consumer,
                "delivered_at": self.now_ms, "times_delivered": 1,
            }
            messages.append((message_id, dict(self.messages[message_id])))
            if len(messages) == count:
                break
        return [(streams.STREAM_KEY, messages)] if messages else []

    async def xpending_range(self, stream, group, start, end, count):
        entries = []
        for message_id in sorted(self.pending, key=self._id):
            if start.startswith("(") and self._id(message_id) <= self._id(start[1:]):
                continue
            if start not in {"-", "+"} and not start.startswith("(") and self._id(message_id) < self._id(start):
                continue
            if end != "+" and self._id(message_id) > self._id(end):
                continue
            entry = dict(self.pending[message_id])
            entry["time_since_delivered"] = self.now_ms - entry.pop("delivered_at")
            entries.append(entry)
            if len(entries) == count:
                break
        return entries

    async def xrange(self, stream, start, end, count=1):
        return [(start, dict(self.messages[start]))] if start in self.messages else []

    async def xclaim(self, stream, group, consumer, min_idle, message_ids, justid):
        self.record("xclaim", stream, group, consumer, min_idle, message_ids, justid=justid)
        claimed = []
        for message_id in message_ids:
            if message_id not in self.pending or message_id not in self.messages:
                continue
            entry = self.pending[message_id]
            if self.now_ms - entry["delivered_at"] < min_idle:
                continue
            entry.update(consumer=consumer, delivered_at=self.now_ms)
            if not justid:
                entry["times_delivered"] += 1
            claimed.append(message_id)
        return claimed

    async def xack(self, stream, group, message_id):
        self.record("xack", stream, group, message_id)
        return int(self.pending.pop(message_id, None) is not None)

    async def xdel(self, stream, message_id):
        self.record("xdel", stream, message_id)
        return int(self.messages.pop(message_id, None) is not None)

    async def aclose(self):
        self.closed = True


class FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.watched = {}
        self.commands = []
        self.in_multi = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def watch(self, *keys):
        self.watched = {key: self.redis.versions[key] for key in keys}

    def multi(self):
        self.in_multi = True

    def __getattr__(self, method):
        def command(*args, **kwargs):
            if self.in_multi:
                self.commands.append((method, args, kwargs))
                return self
            return getattr(self.redis, method)(*args, **kwargs)
        return command

    async def execute(self):
        if self.redis.before_execute:
            callback, self.redis.before_execute = self.redis.before_execute, None
            callback()
        if any(self.redis.versions[key] != version for key, version in self.watched.items()):
            raise WatchError("Watched key changed or expired")
        results, errors = [], []
        for method, args, kwargs in self.commands:
            try:
                results.append(await getattr(self.redis, method)(*args, **kwargs))
            except Exception as exc:
                errors.append(exc)
                results.append(exc)
        if errors:
            raise errors[0]
        return results


@pytest.fixture(autouse=True)
def no_real_redis(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Queue tests must not open real Redis/Postgres connections")
    monkeypatch.setattr(streams.aioredis, "from_url", forbidden)
    monkeypatch.setattr(access.pgshim, "get_pool", forbidden)


@pytest.fixture(autouse=True)
def isolated_authorization(monkeypatch):
    async def registered_root(repo_path, graph_name):
        if graph_name != "authorized" or Path(repo_path) != _AUTHORIZED_ROOT:
            raise HTTPException(403, "Unregistered project or repository path")
        return _AUTHORIZED_ROOT

    validator = AsyncMock(side_effect=registered_root)
    monkeypatch.setattr(access, "authorized_job_repo_root", validator)
    return validator


@pytest.fixture
def registered_checkout(monkeypatch):
    # Keep every filesystem fixture under this checkout, not the OS temp folder.
    directory = Path(__file__).resolve().parents[2] / ".pytest_cache" / "queue-paths" / uuid.uuid4().hex
    root, outside = directory / "repo", directory / "outside"
    root.mkdir(parents=True)
    outside.mkdir()
    (root / "source.py").write_text("value = 1\n", encoding="utf-8")
    records = [{
        "id": 1, "project_id": "project-id", "project_name": "authorized", "repo_path": str(root),
    }]
    cursor = AsyncMock()
    cursor.fetchall.side_effect = lambda: list(records)
    cursor_context = AsyncMock()
    cursor_context.__aenter__.return_value = cursor
    db = MagicMock()
    db.execute.return_value = cursor_context
    connection = AsyncMock()
    connection.__aenter__.return_value = db
    pool = MagicMock()
    pool.acquire.return_value = connection
    monkeypatch.setattr(access.pgshim, "get_pool", lambda: pool)
    monkeypatch.setattr(access, "authorized_job_repo_root", _REAL_AUTHORIZED_JOB_ROOT)
    try:
        yield root, outside, records
    finally:
        shutil.rmtree(directory)


def make_consumer(redis, **kwargs):
    consumer = JobConsumer("redis://unused.invalid", **kwargs)
    consumer._client = redis
    return consumer


async def enqueue(redis, **kwargs):
    job = IndexJob(
        job_type=JobType.INDEX_FULL,
        repo_path=r"D:\repos\authorized",
        project_name="authorized",
        **kwargs,
    )
    producer = JobProducer("redis://unused.invalid")
    producer._client = redis
    message_id = await producer.publish(job)
    return message_id, job


def status(redis, job):
    return redis.hashes[streams._status_key(job.job_id)]


async def processing(redis, **kwargs):
    message_id, job = await enqueue(redis, **kwargs)
    consumer = make_consumer(redis)
    assert await consumer.consume(block_ms=1) == [(message_id, job)]
    token = await consumer.set_job_processing(job, message_id)
    assert token
    return consumer, message_id, job, token


@pytest.mark.asyncio
@pytest.mark.parametrize("errors", [1, ["parse failed"]])
async def test_error_stats_are_not_acknowledged_as_success(monkeypatch, errors):
    redis = FakeRedis()
    message_id, job = await enqueue(redis)
    worker = indexer_module.IndexerConsumer("redis://unused.invalid", MagicMock())
    worker._consumer = make_consumer(redis)
    await worker._consumer.consume(block_ms=1)
    pipeline = MagicMock()
    pipeline.index_full.return_value = {"files": 2, "errors": errors}
    monkeypatch.setattr(indexer_module, "IndexPipeline", lambda **kwargs: pipeline)

    await worker._process(message_id, job)
    assert status(redis, job)["status"] == "retrying"
    assert status(redis, job)["attempts"] == "1"
    assert message_id in redis.pending
    assert not any(call[0] == "xack" for call in redis.calls)


@pytest.mark.asyncio
async def test_publish_does_not_trim_unfinished_messages():
    redis = FakeRedis()
    message_id, job = await enqueue(redis)
    assert all("maxlen" not in kwargs for name, _, kwargs in redis.calls if name == "xadd")
    assert streams._status_key(job.job_id) not in redis.expires
    redis.advance(88 * 24 * 3600 * 1000)
    assert message_id in redis.messages
    assert status(redis, job)["status"] == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [RuntimeError("atomic commit failed"), {"errors": 0, "committed": False}])
async def test_commit_failure_is_retried_not_done(monkeypatch, result):
    redis = FakeRedis()
    message_id, job = await enqueue(redis)
    worker = indexer_module.IndexerConsumer("redis://unused.invalid", MagicMock())
    worker._consumer = make_consumer(redis)
    await worker._consumer.consume(block_ms=1)
    pipeline = MagicMock()
    if isinstance(result, Exception):
        pipeline.index_full.side_effect = result
    else:
        pipeline.index_full.return_value = result
    monkeypatch.setattr(indexer_module, "IndexPipeline", lambda **kwargs: pipeline)

    await worker._process(message_id, job)

    assert status(redis, job)["status"] == "retrying"
    assert "commit" in status(redis, job)["error"]
    assert message_id in redis.pending


@pytest.mark.asyncio
async def test_done_is_durable_before_ack_and_only_own_message_deleted():
    redis = FakeRedis()
    consumer, message_id, job, token = await processing(redis)
    other_id, _ = await enqueue(redis)

    await consumer.set_job_done(job, {"files": 2, "errors": 0}, message_id, token)

    done_write = next(i for i, call in enumerate(redis.calls)
                      if call[0] == "hset" and call[2]["mapping"].get("status") == "done")
    ack = next(i for i, call in enumerate(redis.calls) if call[0] == "xack")
    assert done_write < ack
    assert status(redis, job)["files"] == "2"
    assert status(redis, job)["status"] == "done"
    assert message_id not in redis.pending and message_id not in redis.messages
    assert other_id in redis.messages
    assert streams._status_key(job.job_id) in redis.expires


@pytest.mark.asyncio
@pytest.mark.parametrize("errors", [0, []])
async def test_success_status_persists_owner_and_json_errors(errors):
    redis = FakeRedis()
    consumer, message_id, job, token = await processing(redis)
    assert (await consumer.get_job_status(job.job_id))["project_name"] == job.project_name

    await consumer.set_job_done(job, {
        "files": 2, "errors": errors, "project_name": "forged-owner", "status": "forged-state",
    }, message_id, token)

    completed = await consumer.get_job_status(job.job_id)
    assert completed["project_name"] == job.project_name
    assert completed["repo_path"] == job.repo_path
    assert completed["status"] == "done"
    assert json.loads(completed["payload"])["project_name"] == job.project_name
    assert json.loads(completed["errors"]) == errors
    assert message_id not in redis.pending


@pytest.mark.asyncio
async def test_success_without_error_field_does_not_keep_stale_error_metadata():
    redis = FakeRedis()
    consumer, message_id, job, token = await processing(redis)
    await redis.hset(streams._status_key(job.job_id), {
        "errors": "['old failure']", "error": "old exception",
        "next_retry_at_ms": "1000", "recovery_action": "pending_replay",
    })
    await consumer.set_job_done(job, {"files": 1}, message_id, token)
    assert json.loads(status(redis, job)["errors"]) == 0
    assert not status(redis, job)["error"]
    assert status(redis, job)["next_retry_at_ms"] == "0"
    assert not status(redis, job)["recovery_action"]
    history = json.loads(status(redis, job)["attempt_history"])
    assert history[-1]["previous_reported_errors"] == "['old failure']"


@pytest.mark.asyncio
async def test_branch_job_status_keeps_its_logical_graph_owner(registered_checkout):
    root, _, _ = registered_checkout
    redis = FakeRedis()
    graph_name = branch_graph_name("project-id", "feature/recovery")
    job = IndexJob(job_type=JobType.INDEX_FULL, repo_path=str(root), project_name=graph_name)
    producer = JobProducer("redis://unused.invalid")
    producer._client = redis
    message_id = await producer.publish(job)
    assert (await producer.get_job_status(job.job_id))["project_name"] == graph_name
    consumer = make_consumer(redis)
    await consumer.consume(block_ms=1)
    token = await consumer.set_job_processing(job, message_id)
    await consumer.set_job_done(job, {"errors": 0}, message_id, token)
    completed = await producer.get_job_status(job.job_id)
    assert completed["project_name"] == graph_name
    assert access.project_owns_graph(ProjectScope("project-id", 1, "authorized", str(root)), graph_name)


@pytest.mark.asyncio
async def test_cannot_ack_nonterminal_or_claim_foreign_terminal_record():
    redis = FakeRedis()
    consumer, message_id, job, token = await processing(redis)
    with pytest.raises(RuntimeError, match="Refusing to ACK"):
        await consumer.ack(message_id, job.job_id)
    with pytest.raises(RuntimeError, match="commit successfully"):
        await consumer.set_job_done(job, {"errors": 1}, message_id, token)
    assert message_id in redis.pending


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_command", ["xack", "xdel", "expire"])
async def test_terminal_cleanup_survives_disconnect_without_reindexing(failed_command):
    redis = FakeRedis()
    consumer, message_id, job, token = await processing(redis)
    redis.fail_next[failed_command] = ConnectionError("interrupted cleanup")
    with pytest.raises(ConnectionError):
        await consumer.set_job_done(job, {"errors": 0}, message_id, token)
    assert status(redis, job)["status"] == "done"
    assert streams._status_key(job.job_id) not in redis.expires
    assert job.job_id in redis.sets[streams.CLEANUP_KEY]

    replacement = make_consumer(redis)
    assert await replacement.consume(block_ms=1) == []
    assert status(redis, job)["attempts"] == "1"
    assert message_id not in redis.pending and message_id not in redis.messages
    assert job.job_id not in redis.sets[streams.CLEANUP_KEY]


@pytest.mark.asyncio
async def test_status_write_failure_never_acks_even_when_exec_keeps_executing():
    redis = FakeRedis()
    consumer, message_id, job, token = await processing(redis)
    redis.fail_next["hset"] = RuntimeError("status write failed")
    with pytest.raises(RuntimeError, match="status write failed"):
        await consumer.set_job_done(job, {"errors": 0}, message_id, token)
    assert status(redis, job)["status"] == "processing"
    assert message_id in redis.pending
    assert not any(call[0] == "xack" for call in redis.calls)

    redis.advance(121_000)
    assert await make_consumer(redis).consume(block_ms=1) == [(message_id, job)]


@pytest.mark.asyncio
async def test_failure_retries_are_bounded_and_history_persists():
    redis = FakeRedis()
    consumer, message_id, job, token = await processing(redis)
    for attempt in range(1, 4):
        state = await consumer.set_job_failed(job, f"error-{attempt}", message_id, token)
        assert status(redis, job)["attempts"] == str(attempt)
        assert status(redis, job)["project_name"] == job.project_name
        if attempt == 3:
            assert state == "failed"
            break
        assert state == "retrying"
        consumer = make_consumer(redis)
        assert await consumer.consume(block_ms=1) == []
        redis.advance(30_000)
        assert await consumer.consume(block_ms=1) == [(message_id, job)]
        token = await consumer.set_job_processing(job, message_id)
    assert message_id not in redis.pending and message_id not in redis.messages
    assert status(redis, job)["terminal"] == "1"
    assert status(redis, job)["dead_letter"] == "1"
    assert [item["error"] for item in json.loads(status(redis, job)["attempt_history"])] == [
        "error-1", "error-2", "error-3"
    ]
    redis.advance(88 * 24 * 3600 * 1000)
    assert status(redis, job)["status"] == "failed"
    assert json.loads(status(redis, job)["payload"])["job_id"] == job.job_id


@pytest.mark.asyncio
async def test_crashed_worker_attempts_are_not_reset_by_restarts():
    redis = FakeRedis()
    consumer, message_id, job, _ = await processing(redis)
    for attempt in (2, 3):
        redis.advance(121_000)
        consumer = make_consumer(redis)
        assert await consumer.consume(block_ms=1) == [(message_id, job)]
        assert await consumer.set_job_processing(job, message_id)
        assert status(redis, job)["attempts"] == str(attempt)
    redis.advance(121_000)
    assert await make_consumer(redis).consume(block_ms=1) == []
    assert status(redis, job)["status"] == "failed"
    assert message_id not in redis.pending
    assert len(json.loads(status(redis, job)["attempt_history"])) == 3


@pytest.mark.asyncio
async def test_heartbeat_protects_long_active_jobs_from_automatic_and_admin_recovery():
    redis = FakeRedis()
    consumer, message_id, job, token = await processing(redis)
    replacement = make_consumer(redis)
    assert consumer._consumer_name != replacement._consumer_name
    for _ in range(5):
        redis.advance(80_000)
        assert await consumer.heartbeat(job, message_id, token)
        assert await replacement.consume(block_ms=1) == []
        assert await replacement.recover_stale_jobs_by_repo([job.repo_path], 0) == []
    assert status(redis, job)["attempts"] == "1"
    assert status(redis, job)["status"] == "processing"


@pytest.mark.asyncio
async def test_stale_token_cannot_renew_or_finish_after_takeover():
    redis = FakeRedis()
    old, message_id, job, old_token = await processing(redis)
    redis.advance(121_000)
    replacement = make_consumer(redis)
    assert await replacement.consume(block_ms=1) == [(message_id, job)]
    new_token = await replacement.set_job_processing(job, message_id)
    assert new_token != old_token
    assert not await old.heartbeat(job, message_id, old_token)
    with pytest.raises(LeaseLostError):
        await old.set_job_done(job, {"errors": 0}, message_id, old_token)
    with pytest.raises(LeaseLostError):
        await old.set_job_failed(job, "old error", message_id, old_token)
    assert status(redis, job)["status"] == "processing"
    assert status(redis, job)["attempts"] == "2"


@pytest.mark.asyncio
async def test_watch_detects_expiration_during_finalization():
    redis = FakeRedis()
    consumer, message_id, job, token = await processing(redis)
    redis.before_execute = lambda: redis.advance(121_000)
    with pytest.raises(LeaseLostError):
        await consumer.set_job_done(job, {"errors": 0}, message_id, token)
    assert message_id in redis.pending
    assert status(redis, job)["status"] == "processing"


@pytest.mark.asyncio
async def test_admin_recovery_replays_original_message_and_preserves_errors():
    redis = FakeRedis()
    consumer, message_id, job, token = await processing(redis)
    await consumer.set_job_failed(job, "original parse error", message_id, token)
    redis.advance(30_000)
    assert await consumer.consume(block_ms=1) == [(message_id, job)]
    await consumer.set_job_processing(job, message_id)
    redis.advance(121_000)

    recovered = await make_consumer(redis).recover_stale_jobs_by_repo([job.repo_path.upper()], 0)

    assert len(recovered) == 1
    assert recovered[0]["status"] == "retrying"
    assert recovered[0]["recovery_action"] == "pending_replay"
    assert recovered[0]["attempts"] == "2"
    assert message_id in redis.pending and message_id in redis.messages
    assert not any(call[0] == "xack" for call in redis.calls)
    history = json.loads(recovered[0]["attempt_history"])
    assert history[0]["error"] == "original parse error"
    assert await consumer.recover_stale_jobs_by_repo([job.repo_path], 0) == []
    assert await consumer.consume(block_ms=1) == [(message_id, job)]


@pytest.mark.asyncio
async def test_eighty_eight_day_legacy_pending_without_status_is_recoverable():
    redis = FakeRedis()
    message_id, job = await enqueue(redis)
    consumer = make_consumer(redis)
    await consumer.consume(block_ms=1)
    await redis.expire(streams._status_key(job.job_id), streams.STATUS_TTL_SEC)
    redis.advance(88 * 24 * 3600 * 1000)
    assert await consumer.get_job_status(job.job_id) is None

    recovered = await consumer.recover_stale_jobs_by_repo([job.repo_path], 900)
    assert len(recovered) == 1
    assert recovered[0]["attempts"] == "1"
    assert message_id in redis.pending
    assert await consumer.consume(block_ms=1) == [(message_id, job)]
    assert await consumer.set_job_processing(job, message_id)
    assert status(redis, job)["attempts"] == "2"


@pytest.mark.asyncio
async def test_legacy_failed_status_is_retriable_not_mistaken_for_dead_letter():
    redis = FakeRedis()
    message_id, job = await enqueue(redis)
    consumer = make_consumer(redis)
    await consumer.consume(block_ms=1)
    await redis.hset(streams._status_key(job.job_id), {
        "status": "failed", "error": "legacy exception", "attempts": "1",
    })
    redis.advance(121_000)
    assert await consumer.consume(block_ms=1) == [(message_id, job)]
    assert status(redis, job)["status"] == "retrying"
    assert status(redis, job)["error"] == "legacy exception"


@pytest.mark.asyncio
async def test_recovery_cursor_does_not_starve_jobs_behind_live_leases():
    redis = FakeRedis()
    active = [await processing(redis) for _ in range(streams.RECOVERY_BATCH_SIZE)]
    message_id, job = await enqueue(redis)
    await make_consumer(redis).consume(block_ms=1)
    redis.advance(60_000)
    for consumer, active_id, active_job, token in active:
        assert await consumer.heartbeat(active_job, active_id, token)
    redis.advance(70_000)
    replacement = make_consumer(redis)
    assert await replacement.consume(block_ms=1) == []
    assert await replacement.consume(block_ms=1) == [(message_id, job)]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", ["not-json", "{}", '{"job_type":"unknown","repo_path":"anything"}'])
async def test_poison_payloads_have_visible_failed_records_without_poisoning_loop(payload):
    redis = FakeRedis()
    message_id = await redis.xadd(streams.STREAM_KEY, {"payload": payload})
    consumer = make_consumer(redis)
    assert await consumer.consume(block_ms=1) == []
    failure = await consumer.get_job_status(f"unreadable-{message_id}")
    assert failure["status"] == "failed"
    assert failure["payload"] == payload
    assert message_id not in redis.pending and message_id not in redis.messages


@pytest.mark.asyncio
async def test_missing_legacy_stream_payload_is_explicit_failed_not_infinite_pending():
    redis = FakeRedis()
    message_id, job = await enqueue(redis)
    consumer = make_consumer(redis)
    await consumer.consume(block_ms=1)
    redis.messages.pop(message_id)
    redis.advance(121_000)
    assert await consumer.consume(block_ms=1) == []
    failure = await consumer.get_job_status(job.job_id)
    assert failure["status"] == "failed"
    assert "missing" in failure["error"]
    assert message_id not in redis.pending


@pytest.mark.asyncio
async def test_cancellation_and_stop_keep_lease_until_thread_actually_exits(monkeypatch):
    redis = FakeRedis()
    message_id, job = await enqueue(redis)
    worker = indexer_module.IndexerConsumer("redis://unused.invalid", MagicMock())
    worker._consumer = make_consumer(redis)
    worker._consumer.heartbeat_interval = 0.005
    await worker._consumer.consume(block_ms=1)
    started, release = threading.Event(), threading.Event()

    def slow_index(_job):
        started.set()
        assert release.wait(timeout=5)
        return {"files": 1, "errors": 0}

    monkeypatch.setattr(worker, "_run_index", slow_index)
    process = asyncio.create_task(worker._process(message_id, job))
    try:
        async def wait_started():
            while not started.is_set():
                await asyncio.sleep(0.001)
        await asyncio.wait_for(wait_started(), 2)
        process.cancel()
        stop = asyncio.create_task(worker.stop())
        await asyncio.sleep(0.03)
        assert not process.done() and not stop.done()
        assert not redis.closed
        assert sum(call[0] == "pexpire" for call in redis.calls) >= 1
        assert await make_consumer(redis).consume(block_ms=1) == []
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(process, 2)
        await asyncio.wait_for(stop, 2)
        assert redis.closed
        assert status(redis, job)["status"] == "done"
    finally:
        release.set()
        if not process.done():
            await asyncio.gather(process, return_exceptions=True)


@pytest.mark.asyncio
async def test_publish_never_overwrites_status_from_fast_consumer(monkeypatch):
    redis = FakeRedis()
    hsetnx = redis.hsetnx

    async def finish_before_publish_returns(key, field, message_id):
        consumer = make_consumer(redis)
        [(received_id, job)] = await consumer.consume(block_ms=1)
        token = await consumer.set_job_processing(job, received_id)
        await consumer.set_job_done(job, {"errors": 0}, message_id, token)
        return await hsetnx(key, field, message_id)

    monkeypatch.setattr(redis, "hsetnx", finish_before_publish_returns)
    message_id, job = await enqueue(redis)
    assert status(redis, job)["status"] == "done"
    assert status(redis, job)["attempts"] == "1"
    assert message_id not in redis.messages


@pytest.mark.asyncio
async def test_republishing_same_job_id_is_idempotent():
    redis = FakeRedis()
    message_id, job = await enqueue(redis)
    producer = JobProducer("redis://unused.invalid")
    producer._client = redis
    assert await producer.publish(job) == message_id
    assert len(redis.messages) == 1
    with pytest.raises(ValueError, match="different payload"):
        await producer.publish(job.model_copy(update={"max_attempts": 2}))


@pytest.mark.asyncio
async def test_duplicate_stream_payload_cannot_overwrite_original_job_status():
    redis = FakeRedis()
    consumer, message_id, job, token = await processing(redis)
    duplicate = await redis.xadd(streams.STREAM_KEY, {"payload": job.model_dump_json()})
    await make_consumer(redis).consume(block_ms=1)
    assert await consumer.heartbeat(job, message_id, token)
    replacement = make_consumer(redis)
    assert await replacement.consume(block_ms=1) == []
    assert duplicate not in redis.pending
    assert status(redis, job)["status"] == "processing"
    assert status(redis, job)["attempts"] == "1"
    assert message_id in redis.pending


@pytest.mark.asyncio
async def test_start_does_not_bypass_retry_backoff_or_process_undelivered_job():
    redis = FakeRedis()
    message_id, job = await enqueue(redis)
    consumer = make_consumer(redis)
    assert await consumer.set_job_processing(job, message_id) is None
    assert status(redis, job)["attempts"] == "0"
    await consumer.consume(block_ms=1)
    token = await consumer.set_job_processing(job, message_id)
    await consumer.set_job_failed(job, "retry later", message_id, token)
    assert await make_consumer(redis).set_job_processing(job, message_id) is None
    assert status(redis, job)["attempts"] == "1"


@pytest.mark.asyncio
async def test_empty_exception_details_still_produce_failed_history():
    redis = FakeRedis()
    consumer, message_id, job, token = await processing(redis, max_attempts=1)
    assert await consumer.set_job_failed(job, "", message_id, token) == "failed"
    assert status(redis, job)["dead_letter"] == "1"
    assert json.loads(status(redis, job)["attempt_history"])[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_poison_message_does_not_discard_other_jobs_from_batch():
    redis = FakeRedis()
    await redis.xadd(streams.STREAM_KEY, {"payload": "invalid"})
    message_id, job = await enqueue(redis)
    assert await make_consumer(redis).consume(count=2, block_ms=1) == [(message_id, job)]
    assert message_id in redis.pending


@pytest.mark.asyncio
async def test_missing_legacy_payload_and_status_get_synthetic_failed_record():
    redis = FakeRedis()
    message_id, job = await enqueue(redis)
    consumer = make_consumer(redis)
    await consumer.consume(block_ms=1)
    redis.messages.pop(message_id)
    redis.hashes.pop(streams._status_key(job.job_id))
    redis.advance(121_000)
    assert await consumer.consume(block_ms=1) == []
    assert (await consumer.get_job_status(f"unreadable-{message_id}"))["status"] == "failed"
    assert message_id not in redis.pending


@pytest.mark.asyncio
async def test_producer_authorization_failure_precedes_any_queue_write():
    redis = FakeRedis()
    producer = JobProducer("redis://unused.invalid")
    producer._client = redis
    for project_name, repo_path in [(None, str(_AUTHORIZED_ROOT)), ("other", str(_AUTHORIZED_ROOT)),
                                    ("authorized", r"D:\repos\outside")]:
        with pytest.raises(HTTPException):
            await producer.publish(IndexJob(
                job_type=JobType.INDEX_FULL, repo_path=repo_path, project_name=project_name,
            ))
    assert redis.calls == []
    assert redis.hashes == {} and redis.messages == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["root_changed", "deactivated"])
async def test_consumer_revalidates_project_before_any_graph_access(
    monkeypatch, registered_checkout, change
):
    root, outside, records = registered_checkout
    redis = FakeRedis()
    producer = JobProducer("redis://unused.invalid")
    producer._client = redis
    job = IndexJob(
        job_type=JobType.INDEX_FULL, repo_path=str(root), project_name="authorized", max_attempts=1,
    )
    message_id = await producer.publish(job)
    if change == "root_changed":
        records[0]["repo_path"] = str(outside)
    else:
        records.clear()
    registry = MagicMock()
    worker = indexer_module.IndexerConsumer("redis://unused.invalid", registry)
    worker._consumer = make_consumer(redis)
    await worker._consumer.consume(block_ms=1)
    pipeline = MagicMock()
    monkeypatch.setattr(indexer_module, "IndexPipeline", pipeline)

    await worker._process(message_id, job)

    registry.get.assert_not_called()
    pipeline.assert_not_called()
    assert status(redis, job)["status"] == "failed"
    assert status(redis, job)["dead_letter"] == "1"
    assert "403" in status(redis, job)["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("relative_paths", [False, True])
async def test_incremental_paths_are_canonicalized_and_revalidated(
    registered_checkout, monkeypatch, relative_paths
):
    root, _, _ = registered_checkout
    resolver = indexer_paths.resolve_changed_path
    if relative_paths:
        def resolve_relative(repo_path, resolved_root, changed_path):
            canonical = Path(resolver(repo_path, resolved_root, changed_path))
            if canonical.is_absolute():
                canonical = canonical.relative_to(resolved_root)
            return canonical.as_posix()
        monkeypatch.setattr(indexer_paths, "resolve_changed_path", resolve_relative)
        monkeypatch.setattr(access, "resolve_changed_path", resolve_relative)
    redis = FakeRedis()
    producer = JobProducer("redis://unused.invalid")
    producer._client = redis
    job = IndexJob(
        job_type=JobType.INDEX_INCREMENTAL, repo_path=str(root), project_name="authorized",
        changed_paths=["source.py", r"nested\deleted.py"],
    )
    message_id = await producer.publish(job)
    stored = IndexJob.model_validate_json(redis.messages[message_id]["payload"])
    expected = [
        indexer_paths.resolve_changed_path(str(root), root, path)
        for path in job.changed_paths
    ]
    if relative_paths:
        assert expected == ["source.py", "nested/deleted.py"]
    assert stored.changed_paths == expected
    worker = indexer_module.IndexerConsumer("redis://unused.invalid", MagicMock())
    worker._consumer = make_consumer(redis)
    [(message_id, stored)] = await worker._consumer.consume(block_ms=1)
    pipeline = MagicMock()
    pipeline.index_incremental.return_value = {"files": 1, "errors": 0}
    monkeypatch.setattr(indexer_module, "IndexPipeline", lambda **kwargs: pipeline)
    await worker._process(message_id, stored)
    pipeline.index_incremental.assert_called_once_with(str(root), expected)
    assert status(redis, job)["status"] == "done"


@pytest.mark.asyncio
@pytest.mark.parametrize("job_type", [JobType.INDEX_FULL, JobType.INDEX_INCREMENTAL])
async def test_producer_rejects_escaping_changed_paths_even_for_full_jobs(registered_checkout, job_type):
    root, outside, _ = registered_checkout
    redis = FakeRedis()
    producer = JobProducer("redis://unused.invalid")
    producer._client = redis
    for path in [str(outside / "private.py"), r"..\outside\private.py", "source.py:secret"]:
        with pytest.raises(HTTPException) as error:
            await producer.publish(IndexJob(
                job_type=job_type, repo_path=str(root), project_name="authorized",
                changed_paths=["source.py", path],
            ))
        assert error.value.status_code == 403
    assert redis.messages == {}


@pytest.mark.asyncio
async def test_forged_queued_path_is_rejected_without_graph_access(registered_checkout):
    root, outside, _ = registered_checkout
    redis = FakeRedis()
    job = IndexJob(
        job_type=JobType.INDEX_INCREMENTAL, repo_path=str(root), project_name="authorized",
        changed_paths=["source.py", str(outside / "private.py")], max_attempts=1,
    )
    message_id = await redis.xadd(streams.STREAM_KEY, {"payload": job.model_dump_json()})
    registry = MagicMock()
    worker = indexer_module.IndexerConsumer("redis://unused.invalid", registry)
    worker._consumer = make_consumer(redis)
    await worker._consumer.consume(block_ms=1)
    await worker._process(message_id, job)
    registry.get.assert_not_called()
    assert status(redis, job)["status"] == "failed"


@pytest.mark.asyncio
async def test_symlink_swapped_after_publication_is_revalidated(registered_checkout):
    root, outside, _ = registered_checkout
    redis = FakeRedis()
    producer = JobProducer("redis://unused.invalid")
    producer._client = redis
    job = IndexJob(
        job_type=JobType.INDEX_INCREMENTAL, repo_path=str(root), project_name="authorized",
        changed_paths=["source.py"], max_attempts=1,
    )
    message_id = await producer.publish(job)
    source = root / "source.py"
    source.unlink()
    try:
        source.symlink_to(outside / "private.py")
    except OSError:
        pytest.skip("Creating symlinks is not permitted on this Windows host")
    registry = MagicMock()
    worker = indexer_module.IndexerConsumer("redis://unused.invalid", registry)
    worker._consumer = make_consumer(redis)
    [(message_id, delivered)] = await worker._consumer.consume(block_ms=1)
    await worker._process(message_id, delivered)
    registry.get.assert_not_called()
    assert status(redis, job)["status"] == "failed"


@pytest.mark.asyncio
async def test_mcp_producer_validates_registration_without_request_context(registered_checkout):
    root, outside, _ = registered_checkout
    redis = FakeRedis()
    producer = MCPProducer("redis://unused.invalid")
    producer._producer._client = redis
    with pytest.raises(HTTPException):
        await producer.submit_full_index(str(root))
    with pytest.raises(HTTPException):
        await producer.submit_full_index(str(root), "other-project")
    with pytest.raises(HTTPException):
        await producer.submit_incremental_index(str(root), ["source.py"], "other-project")
    with pytest.raises(HTTPException):
        await producer.submit_full_index(str(outside), "authorized")
    full = await producer.submit_full_index(str(root), "authorized")
    result = await producer.submit_incremental_index(str(root), ["source.py"], "authorized")
    assert len(redis.messages) == 2
    assert full["stream_id"] in redis.messages
    queued = IndexJob.model_validate_json(redis.messages[result["stream_id"]]["payload"])
    assert queued.project_name == "authorized"
    assert queued.changed_paths == [indexer_paths.resolve_changed_path(str(root), root, "source.py")]


@pytest.mark.asyncio
async def test_producer_and_worker_do_not_route_using_ambient_context(registered_checkout, monkeypatch):
    root, outside, _ = registered_checkout
    redis = FakeRedis()
    producer = MCPProducer("redis://unused.invalid")
    producer._producer._client = redis
    registry = MagicMock()
    worker = indexer_module.IndexerConsumer("redis://unused.invalid", registry)
    worker._consumer = make_consumer(redis)
    pipeline = MagicMock()
    pipeline.index_full.return_value = {"files": 1, "errors": 0}
    monkeypatch.setattr(indexer_module, "IndexPipeline", lambda **kwargs: pipeline)
    unrelated = ProjectScope("other-project-id", 2, "unrelated", str(outside))

    # The request handler authorizes enqueueing; queued work is bound to its
    # explicit DB-validated graph, never an inherited request ContextVar.
    with bind_project_scope(unrelated):
        await producer.submit_full_index(str(root), "authorized")
        assert require_project_scope() is unrelated
        [(message_id, job)] = await worker._consumer.consume(block_ms=1)
        await worker._process(message_id, job)
        assert require_project_scope() is unrelated

    registry.get.assert_called_once_with("authorized")
    pipeline.index_full.assert_called_once_with(str(root))
    assert status(redis, job)["status"] == "done"


@pytest.mark.asyncio
async def test_worker_retries_initial_broker_connection_failure():
    worker = indexer_module.IndexerConsumer("redis://unused.invalid", MagicMock())
    worker._consumer = MagicMock()
    worker._consumer.connect = AsyncMock(side_effect=[ConnectionError("unavailable"), None])
    worker._consumer.close = AsyncMock()
    worker._sleep = 0.001

    async def consume(**kwargs):
        worker._running = False
        return []

    worker._consumer.consume = AsyncMock(side_effect=consume)
    await asyncio.wait_for(worker.start(), 2)
    assert worker._consumer.connect.await_count == 2
    worker._consumer.consume.assert_awaited_once()


@pytest.mark.asyncio
async def test_stop_never_starts_a_job_delivered_during_shutdown(monkeypatch):
    redis = FakeRedis()
    message_id, job = await enqueue(redis)
    worker = indexer_module.IndexerConsumer("redis://unused.invalid", MagicMock())
    worker._consumer = make_consumer(redis)
    worker._consumer.connect = AsyncMock()
    reading, release = asyncio.Event(), asyncio.Event()

    async def consume(**kwargs):
        reading.set()
        await release.wait()
        return [(message_id, job)]

    monkeypatch.setattr(worker._consumer, "consume", consume)
    process = AsyncMock()
    monkeypatch.setattr(worker, "_process", process)
    run = asyncio.create_task(worker.start())
    await asyncio.wait_for(reading.wait(), 2)
    await worker.stop()
    release.set()
    await asyncio.wait_for(run, 2)
    process.assert_not_awaited()
    assert message_id in redis.messages


@pytest.mark.asyncio
@pytest.mark.parametrize("renewal_error", [False, True])
async def test_failed_heartbeat_prevents_done_even_if_pipeline_returns_success(monkeypatch, renewal_error):
    redis = FakeRedis()
    message_id, job = await enqueue(redis)
    worker = indexer_module.IndexerConsumer("redis://unused.invalid", MagicMock())
    worker._consumer = make_consumer(redis)
    worker._consumer.heartbeat_interval = 0.001
    await worker._consumer.consume(block_ms=1)
    release = threading.Event()
    heartbeat_seen = asyncio.Event()

    def slow_index(_job):
        assert release.wait(timeout=5)
        return {"errors": 0}

    async def renew(*args):
        heartbeat_seen.set()
        if renewal_error:
            raise ConnectionError("Uncertain renewal")
        return False

    monkeypatch.setattr(worker, "_run_index", slow_index)
    monkeypatch.setattr(worker._consumer, "heartbeat", renew)
    process = asyncio.create_task(worker._process(message_id, job))
    try:
        await asyncio.wait_for(heartbeat_seen.wait(), 2)
        release.set()
        await asyncio.wait_for(process, 2)
        assert status(redis, job)["status"] == "processing"
        assert message_id in redis.pending
        assert not any(call[0] == "xack" for call in redis.calls)
        redis.advance(121_000)
        assert await make_consumer(redis).consume(block_ms=1) == [(message_id, job)]
        assert status(redis, job)["attempts"] == "1"
    finally:
        release.set()
        if not process.done():
            await asyncio.gather(process, return_exceptions=True)


@pytest.mark.asyncio
async def test_legacy_unscoped_payload_never_falls_back_to_default_graph():
    redis = FakeRedis()
    job = IndexJob(job_type=JobType.INDEX_FULL, repo_path=str(_AUTHORIZED_ROOT), max_attempts=1)
    message_id = await redis.xadd(streams.STREAM_KEY, {"payload": job.model_dump_json()})
    registry = MagicMock()
    worker = indexer_module.IndexerConsumer("redis://unused.invalid", registry)
    worker._consumer = make_consumer(redis)
    await worker._consumer.consume(block_ms=1)
    await worker._process(message_id, job)
    registry.get.assert_not_called()
    assert status(redis, job)["status"] == "failed"


@pytest.mark.parametrize("stats", [
    None, {"errors": None}, {"errors": "0"}, {"errors": -1},
    {"errors": 0, "committed": 0}, {"errors": 0, "committed": "false"},
])
def test_invalid_success_statistics_fail_closed(stats):
    with pytest.raises(RuntimeError):
        streams.require_success_stats(stats)
