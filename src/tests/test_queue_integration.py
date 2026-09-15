"""Real Redis queue tests with unique keys and an isolated PostgreSQL schema."""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
import uuid

import pytest
import pytest_asyncio
import redis.asyncio as redis
from redis.exceptions import RedisError

from backend.graph.registry import GraphRegistry
from backend.indexer.consumer import IndexerConsumer
from backend.queue import streams
from backend.queue.models import IndexJob, JobType


pytestmark = [pytest.mark.live_graph, pytest.mark.live_graph_e2e]


@pytest_asyncio.fixture
async def queue_runtime(auth_pg_pool, tmp_path, monkeypatch):
    url = os.getenv("QUEUE_REDIS_URL", "redis://127.0.0.1:6380/1")
    client = redis.from_url(url, decode_responses=True, socket_connect_timeout=2)
    try:
        await client.ping()
    except RedisError as exc:
        await client.aclose()
        pytest.skip(f"Isolated Redis service unavailable: {exc}")
    identity = uuid.uuid4().hex
    prefix = f"cga:test:{identity}:"
    monkeypatch.setattr(streams, "STREAM_KEY", prefix + "jobs")
    monkeypatch.setattr(streams, "STATUS_KEY_PREFIX", prefix + "status:")
    monkeypatch.setattr(streams, "LEASE_KEY_PREFIX", prefix + "lease:")
    monkeypatch.setattr(streams, "CLEANUP_KEY", prefix + "cleanup")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "service.py").write_text("def ready():\n    return 1\n", encoding="utf-8")
    name = f"queue_{identity}"
    async with auth_pg_pool.acquire() as db:
        await db.execute(
            "INSERT INTO projects(project_name, project_id, repo_path, is_active) VALUES (?, ?, ?, 1)",
            (name, identity, str(repo)),
        )
    try:
        yield SimpleNamespace(url=url, client=client, root=repo, name=name)
    finally:
        keys = [key async for key in client.scan_iter(match=prefix + "*")]
        if keys:
            await client.delete(*keys)
        await client.aclose()


async def _publish(runtime):
    job = IndexJob(
        job_type=JobType.INDEX_FULL,
        repo_path=str(runtime.root),
        project_name=runtime.name,
    )
    producer = streams.JobProducer(runtime.url)
    await producer.connect()
    try:
        message = await producer.publish(job)
    finally:
        await producer.close()
    return message, job


async def test_real_queue_worker_publishes_graph_before_done(queue_runtime):
    runtime = queue_runtime
    message, job = await _publish(runtime)
    registry = GraphRegistry(
        os.getenv("FALKORDB_HOST", "127.0.0.1"),
        int(os.getenv("FALKORDB_PORT", "16379")),
    )
    worker = IndexerConsumer(runtime.url, registry)
    await worker._consumer.connect()
    try:
        delivered = await worker._consumer.consume(block_ms=1)
        assert delivered == [(message, job)]
        await worker._process(message, job)
        status = await worker._consumer.get_job_status(job.job_id)
        assert status["status"] == "done"
        assert status["errors"] == "0"
        assert not status["error"]
        assert await runtime.client.xlen(streams.STREAM_KEY) == 0
        assert (await runtime.client.xpending(streams.STREAM_KEY, streams.CONSUMER_GROUP))["pending"] == 0
        graph = registry.get(runtime.name)
        assert graph.cache_generation() != "0"
        assert graph.query("MATCH(f:File) RETURN count(f)").result_set == [[1]]
    finally:
        graph = registry.get(runtime.name)
        graph.delete()
        graph._db.connection.delete(graph._generation_key)
        registry.close_all()
        await worker.stop()


async def test_real_pending_takeover_fences_previous_worker(queue_runtime):
    runtime = queue_runtime
    message, job = await _publish(runtime)
    old = streams.JobConsumer(runtime.url, lease_seconds=0.1)
    new = streams.JobConsumer(runtime.url, lease_seconds=0.1)
    await old.connect()
    await new.connect()
    try:
        assert await old.consume(block_ms=1) == [(message, job)]
        old_token = await old.set_job_processing(job, message)
        await asyncio.sleep(0.2)
        assert await new.consume(block_ms=1) == [(message, job)]
        new_token = await new.set_job_processing(job, message)
        assert new_token and new_token != old_token
        with pytest.raises(streams.LeaseLostError):
            await old.set_job_done(job, {"errors": 0}, message, old_token)
        await new.set_job_done(job, {"errors": 0, "files": 1}, message, new_token)
        status = await new.get_job_status(job.job_id)
        assert status["status"] == "done"
        assert status["attempts"] == "2"
        assert not status["error"]
        assert await runtime.client.xlen(streams.STREAM_KEY) == 0
    finally:
        await old.close()
        await new.close()
