"""Indexer consumer – reads jobs from the Redis Stream and drives the pipeline.

Runs as a long-lived async loop inside the FastAPI lifespan.
Uses consumer group semantics:
- A renewable message lease prevents replay of an active long-running job.
- Durable bounded retries precede a visible failed/dead-letter terminal state.
- Pipeline commits own graph-generation/cache invalidation; only committed,
  error-free statistics may be finalized as done.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
import structlog
from redis.exceptions import RedisError

from backend.queue.streams import JobConsumer, LeaseLostError, require_success_stats
from backend.queue.models import JobType, IndexJob, validate_job_paths
from backend.graph.registry import GraphRegistry
from backend.indexer.pipeline import IndexPipeline

log = structlog.get_logger()

_BASE_SLEEP = 1.0
_MAX_SLEEP = 30.0


class IndexerConsumer:
    def __init__(self, redis_url: str, registry: GraphRegistry) -> None:
        self._consumer = JobConsumer(redis_url=redis_url)
        self._registry = registry
        self._running = False
        self._stopping = False
        self._sleep = _BASE_SLEEP
        self._idle = asyncio.Event()
        self._idle.set()

    async def start(self) -> None:
        if self._stopping:
            return
        self._running = True
        connected = False
        try:
            while self._running:
                try:
                    if not connected:
                        await self._consumer.connect()
                        connected = True
                        log.info("indexer.consumer.started")
                    if not self._running:
                        break
                    jobs = await self._consumer.consume(count=1, block_ms=3_000)
                    for msg_id, job in jobs:
                        if not self._running:
                            break
                        await self._process(msg_id, job)
                    self._sleep = _BASE_SLEEP
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    log.error("indexer.consumer.loop_error", error=str(exc))
                    connected = False
                    try:
                        await self._consumer.close()
                    except (RedisError, OSError) as close_error:
                        log.warning("indexer.consumer.close_failed", error=str(close_error))
                    if self._running:
                        await asyncio.sleep(self._sleep)
                        self._sleep = min(self._sleep * 2, _MAX_SLEEP)
        finally:
            self._running = False
            await self._consumer.close()

    async def stop(self) -> None:
        self._stopping = True
        self._running = False
        await self._idle.wait()
        await self._consumer.close()
        log.info("indexer.consumer.stopped")

    # Delegate job status queries to JobConsumer
    async def get_jobs_by_repo(self, repo_path: str) -> list[dict]:
        """Get all job statuses for a given repo path, most recent first."""
        return await self._consumer.get_jobs_by_repo(repo_path)

    async def get_queue_snapshot(self) -> dict:
        """Get active queue snapshot and historical average job duration."""
        return await self._consumer.get_queue_snapshot()

    async def recover_stale_jobs_by_repo(self, repo_paths: list[str], stale_after_sec: int) -> list[dict]:
        """Recover stale jobs for a set of repo path variants."""
        return await self._consumer.recover_stale_jobs_by_repo(repo_paths, stale_after_sec)

    async def _process(self, msg_id: str, job: IndexJob) -> None:
        self._idle.clear()
        heartbeat: asyncio.Task | None = None
        token: str | None = None
        cancelled = False
        lease_lost = asyncio.Event()
        log.info("indexer.job.start", job_id=job.job_id, type=job.job_type)
        try:
            token = await self._consumer.set_job_processing(job, msg_id)
            if token is None:
                return
            heartbeat = asyncio.create_task(
                self._heartbeat(job, msg_id, token, lease_lost)
            )
            job = await validate_job_paths(job)
            work = asyncio.create_task(asyncio.to_thread(self._run_index, job))
            while True:
                try:
                    stats = await asyncio.shield(work)
                    break
                except asyncio.CancelledError:
                    cancelled = True
                    # Cancelling to_thread does not stop its thread. Keep the
                    # heartbeat until the real work exits, including shutdown.
                    if work.cancelled():
                        raise
            if lease_lost.is_set():
                raise LeaseLostError("Message heartbeat lost while indexing")
            require_success_stats(stats)
            await self._consumer.set_job_done(job, stats, msg_id, token)
            log.info("indexer.job.done", job_id=job.job_id, **stats)
        except asyncio.CancelledError:
            cancelled = True
        except LeaseLostError as exc:
            log.error("indexer.job.lease_lost", job_id=job.job_id, error=str(exc))
        except Exception as exc:
            if token is not None and not lease_lost.is_set():
                try:
                    await self._consumer.set_job_failed(job, str(exc), msg_id, token)
                except LeaseLostError:
                    log.warning("indexer.job.failure_lease_lost", job_id=job.job_id)
            log.error("indexer.job.failed", job_id=job.job_id, error=str(exc))
        finally:
            try:
                if heartbeat is not None:
                    heartbeat.cancel()
                    with suppress(asyncio.CancelledError):
                        await heartbeat
            finally:
                self._idle.set()
        if cancelled:
            raise asyncio.CancelledError

    async def _heartbeat(
        self, job: IndexJob, msg_id: str, token: str, lost: asyncio.Event
    ) -> None:
        while True:
            await asyncio.sleep(self._consumer.heartbeat_interval)
            try:
                if not await self._consumer.heartbeat(job, msg_id, token):
                    lost.set()
                    return
            except Exception as exc:
                # A timeout is an uncertain renewal: never finalize success,
                # but keep trying to protect a still-running thread's lease.
                lost.set()
                log.error("indexer.job.heartbeat_error", job_id=job.job_id, error=str(exc))

    def _run_index(self, job: IndexJob) -> dict:
        if not job.project_name:
            raise ValueError("Index job has no authorized project graph")
        graph = self._registry.get(job.project_name)
        pipeline = IndexPipeline(graph=graph)
        if job.job_type == JobType.INDEX_FULL:
            return pipeline.index_full(job.repo_path)
        if job.job_type == JobType.INDEX_INCREMENTAL:
            return pipeline.index_incremental(job.repo_path, job.changed_paths or [])
        raise ValueError(f"Unsupported index job type: {job.job_type}")
