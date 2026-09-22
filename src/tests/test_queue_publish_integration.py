"""Publication protocol tests using only uniquely namespaced real Redis keys."""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
import pytest_asyncio
import redis.asyncio as redis
from redis.exceptions import ConnectionError as RedisConnectionError, RedisError, ResponseError

from backend.queue import streams
from backend.queue.models import IndexJob, JobType
from backend.tools.producer import MCPProducer


pytestmark = [pytest.mark.live_graph, pytest.mark.live_graph_e2e]


@pytest_asyncio.fixture
async def isolated_redis_publication(monkeypatch):
    url = os.getenv("QUEUE_REDIS_URL", "redis://127.0.0.1:6380/1")
    client = redis.from_url(url, decode_responses=True, socket_connect_timeout=2, socket_timeout=5)
    try:
        await client.ping()
    except RedisError as exc:
        await client.aclose()
        pytest.skip(f"Redis unavailable for isolated publication test: {exc}")
    prefix = f"cga:test:publish:{uuid.uuid4().hex}:"
    monkeypatch.setattr(streams, "STREAM_KEY", prefix + "jobs")
    monkeypatch.setattr(streams, "STATUS_KEY_PREFIX", prefix + "status:")
    monkeypatch.setattr(streams, "LEASE_KEY_PREFIX", prefix + "lease:")
    monkeypatch.setattr(streams, "CLEANUP_KEY", prefix + "cleanup")
    # This suite tests the Redis protocol; DB-bound authorization has separate tests.
    monkeypatch.setattr(streams, "validate_job_paths", AsyncMock(side_effect=lambda job: job))
    producer = streams.JobProducer(url)
    producer._client = client
    job = IndexJob(job_type=JobType.INDEX_FULL, repo_path=r"D:\unused", project_name="isolated")
    try:
        yield SimpleNamespace(client=client, producer=producer, job=job, url=url)
    finally:
        keys = [key async for key in client.scan_iter(match=prefix + "*")]
        if keys:
            await client.delete(*keys)
        await client.aclose()


async def test_real_concurrent_same_job_has_one_atomic_publication(isolated_redis_publication):
    runtime = isolated_redis_publication
    ids = await asyncio.gather(*(runtime.producer.publish(runtime.job) for _ in range(20)))
    assert len(set(ids)) == 1
    assert await runtime.client.xlen(streams.STREAM_KEY) == 1
    status = await runtime.producer.get_job_status(runtime.job.job_id)
    assert status["stream_id"] == ids[0]
    assert status["status"] == "pending" and status["attempts"] == "0"
    assert await runtime.client.ttl(streams._status_key(runtime.job.job_id)) == -1
    with pytest.raises(ValueError, match="different payload"):
        await runtime.producer.publish(runtime.job.model_copy(update={"max_attempts": 4}))
    assert await runtime.producer.get_job_status(runtime.job.job_id) == status


async def test_real_response_loss_recovers_existing_stream_id(isolated_redis_publication, monkeypatch):
    runtime = isolated_redis_publication
    evaluate = runtime.client.eval

    async def lose_response(*args):
        await evaluate(*args)
        raise RedisConnectionError("Injected lost script response")

    with monkeypatch.context() as patcher:
        patcher.setattr(runtime.client, "eval", lose_response)
        with pytest.raises(RedisConnectionError):
            await runtime.producer.publish(runtime.job)
    status = await runtime.producer.get_job_status(runtime.job.job_id)
    assert await runtime.producer.publish(runtime.job) == status["stream_id"]
    assert await runtime.client.xlen(streams.STREAM_KEY) == 1


async def test_real_done_generation_round_trips_to_waiter(isolated_redis_publication):
    runtime = isolated_redis_publication
    message_id = await runtime.producer.publish(runtime.job)
    consumer = streams.JobConsumer(runtime.url)
    await consumer.connect()
    try:
        assert await consumer.consume(block_ms=1) == [(message_id, runtime.job)]
        token = await consumer.set_job_processing(runtime.job, message_id)
        assert token
        generation = uuid.uuid4().hex
        await consumer.set_job_done(runtime.job, {
            "errors": 0, "files": 1, "published_generation": generation,
        }, message_id, token)
        waiter = MCPProducer(runtime.url)
        waiter._producer = runtime.producer
        result = await waiter.wait_for_job_status(runtime.job.job_id, timeout_sec=0)
        assert result["ready"] is True and result["timeout"] is False
        assert result["status"] == "done"
        assert result["published_generation"] == generation
        assert json.loads(result["stats"])["published_generation"] == generation
        assert result["project_name"] == runtime.job.project_name
        assert await runtime.client.xlen(streams.STREAM_KEY) == 0
    finally:
        await consumer.close()


@pytest.mark.parametrize("key_kind", ["status", "stream"])
async def test_real_wrong_key_type_is_rejected_before_writes(isolated_redis_publication, key_kind):
    runtime = isolated_redis_publication
    status_key = streams._status_key(runtime.job.job_id)
    key = status_key if key_kind == "status" else streams.STREAM_KEY
    await runtime.client.set(key, "preserve-me")
    with pytest.raises(ResponseError, match="must be"):
        await runtime.producer.publish(runtime.job)
    assert await runtime.client.get(key) == "preserve-me"
    other = streams.STREAM_KEY if key_kind == "status" else status_key
    assert not await runtime.client.exists(other)


@pytest.mark.parametrize("phase", ["before_xadd", "after_xadd"])
async def test_real_lua_runtime_error_does_not_allow_duplicate_retry(isolated_redis_publication, monkeypatch, phase):
    runtime = isolated_redis_publication
    command = "local id = redis.call('XADD', KEYS[2], '*', 'payload', ARGV[1])"
    fail = "redis.call('INCR', KEYS[1])"  # Deliberate WRONGTYPE after writing the status hash.
    replacement = f"{fail}\n{command}" if phase == "before_xadd" else f"{command}\n{fail}"
    with monkeypatch.context() as patcher:
        patcher.setattr(streams, "_PUBLISH_JOB", streams._PUBLISH_JOB.replace(command, replacement))
        with pytest.raises(ResponseError, match="WRONGTYPE"):
            await runtime.producer.publish(runtime.job)
    status = await runtime.producer.get_job_status(runtime.job.job_id)
    assert status["status"] == "publishing" and status["stream_id"] == ""
    count = await runtime.client.xlen(streams.STREAM_KEY)
    assert count == (1 if phase == "after_xadd" else 0)
    with pytest.raises(RuntimeError, match="recovery is required"):
        await runtime.producer.publish(runtime.job)
    assert await runtime.client.xlen(streams.STREAM_KEY) == count
    assert await runtime.producer.get_job_status(runtime.job.job_id) == status
    if phase == "after_xadd":
        consumer = streams.JobConsumer(runtime.url)
        await consumer.connect()
        try:
            [(message_id, job)] = await consumer.consume(block_ms=1)
            token = await consumer.set_job_processing(job, message_id)
            assert token
            await consumer.set_job_done(job, {"errors": 0}, message_id, token)
            assert await runtime.producer.publish(job) == message_id
            assert await runtime.client.xlen(streams.STREAM_KEY) == 0
        finally:
            await consumer.close()


@pytest.mark.parametrize("mutation", ["missing_id", "unknown_state", "missing_stream", "different_payload"])
async def test_real_anomalous_existing_status_requires_recovery(isolated_redis_publication, mutation):
    runtime = isolated_redis_publication
    message_id = await runtime.producer.publish(runtime.job)
    key = streams._status_key(runtime.job.job_id)
    if mutation == "missing_id":
        await runtime.client.hdel(key, "stream_id")
    elif mutation == "unknown_state":
        await runtime.client.hset(key, "status", "unknown")
    elif mutation == "missing_stream":
        await runtime.client.xdel(streams.STREAM_KEY, message_id)
    else:
        await runtime.client.hset(key, "payload", "different")
    status = await runtime.producer.get_job_status(runtime.job.job_id)
    count = await runtime.client.xlen(streams.STREAM_KEY)
    with pytest.raises((RuntimeError, ValueError)):
        await runtime.producer.publish(runtime.job)
    assert await runtime.client.xlen(streams.STREAM_KEY) == count
    assert await runtime.producer.get_job_status(runtime.job.job_id) == status
