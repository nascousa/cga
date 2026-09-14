"""At-least-once indexing queue with fenced leases and durable retry history.

Unfinished messages are never length-trimmed. Terminal state is persisted before
ACK, then the acknowledged entry is XDEL'd. A durable cleanup set retries this
sequence after disconnects. Only cleaned-up successes expire after seven days;
active jobs and failed/dead-letter payloads remain available for inspection.

WATCH fences lease changes (including expiry; Redis >= 6.0.9 is required).
ACK is deliberately outside EXEC: Redis transactions do not roll back or stop
executing commands after a runtime error in a preceding status write.
"""

from __future__ import annotations

import json
import math
import uuid
from datetime import datetime, timezone

import redis.asyncio as aioredis
from redis.exceptions import WatchError
import structlog

from backend.queue.models import IndexJob, validate_job_paths

log = structlog.get_logger()

STREAM_KEY = "contextgraph:jobs"
CONSUMER_GROUP = "indexer-group"
CONSUMER_NAME = "indexer-worker"
STATUS_KEY_PREFIX = "contextgraph:job:status:"
LEASE_KEY_PREFIX = "contextgraph:job:lease:"
CLEANUP_KEY = "contextgraph:jobs:terminal-cleanup"
STATUS_TTL_SEC = 7 * 24 * 60 * 60
LEASE_SECONDS = 120
RECOVERY_BATCH_SIZE = 100
_WATCH_RETRIES = 5


class LeaseLostError(RuntimeError):
    """The worker no longer owns the message and may not finalize its status."""


def _status_key(job_id: str) -> str:
    return f"{STATUS_KEY_PREFIX}{job_id}"


def _lease_key(message_id: str) -> str:
    return f"{LEASE_KEY_PREFIX}{message_id}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _terminal(status: dict) -> bool:
    # Old workers used "failed" for an unacknowledged, recoverable exception.
    return status.get("status") == "done" or (
        status.get("status") == "failed" and status.get("terminal") == "1"
    )


def _base_status(job: IndexJob, status: str, stream_id: str) -> dict[str, str]:
    return {
        "job_id": job.job_id,
        "job_type": job.job_type.value,
        "repo_path": job.repo_path,
        "project_name": job.project_name or "",
        "status": status,
        "stream_id": stream_id,
        "created_at": job.created_at,
        "updated_at": _now(),
        "payload": job.model_dump_json(),
        "max_attempts": str(job.max_attempts),
    }


def _attempt_count(status: dict, fallback: int = 0) -> int:
    try:
        return max(0, int(status.get("attempts", fallback)))
    except (ValueError, TypeError):
        # Corrupt retry metadata must never reset the retry budget.
        return 10


def _history(status: dict) -> list[dict]:
    try:
        value = json.loads(status.get("attempt_history", "[]"))
        if isinstance(value, list) and all(isinstance(item, dict) for item in value):
            return value
    except (ValueError, TypeError):
        pass
    return [{"error": "Unreadable prior attempt history", "raw": status.get("attempt_history")}]


def _finish_history(status: dict, attempt: int, error: str | None) -> str:
    history = _history(status)
    if not history or history[-1].get("attempt") != attempt:
        history.append({"attempt": attempt})
    history[-1].update({"finished_at": _now(), "status": "failed" if error is not None else "done"})
    reported = status.get("errors")
    if reported not in (None, "", "0", "[]", "null", "false"):
        history[-1].setdefault("previous_reported_errors", reported)
    if error is not None:
        previous = history[-1].get("error")
        if previous and previous != error:
            history[-1].setdefault("previous_errors", []).append(previous)
        history[-1]["error"] = error
    return json.dumps(history)


def require_success_stats(stats: dict) -> None:
    if not isinstance(stats, dict):
        raise RuntimeError("Index pipeline did not return statistics")
    errors = stats.get("errors", 0)
    if (errors != 0 and errors != []) or ("committed" in stats and stats["committed"] is not True):
        raise RuntimeError(f"Index pipeline did not commit successfully: {stats}")


async def _server_time_ms(client) -> int:
    seconds, microseconds = await client.time()
    return int(seconds) * 1000 + int(microseconds) // 1000


def _retry_due(status: dict, now_ms: int) -> bool:
    try:
        return int(status.get("next_retry_at_ms", "0")) <= now_ms
    except (ValueError, TypeError):
        # Invalid scheduling metadata must not strand an otherwise bounded job.
        return True


class JobProducer:
    """Publish without trimming pending jobs or overwriting a worker's status."""

    def __init__(self, redis_url: str) -> None:
        self._redis_url = redis_url
        self._client: aioredis.Redis | None = None

    async def connect(self) -> None:
        self._client = aioredis.from_url(
            self._redis_url, decode_responses=True, socket_connect_timeout=5,
            socket_timeout=10, encoding_errors="replace",
        )

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()

    async def publish(self, job: IndexJob) -> str:
        if not self._client:
            raise RuntimeError("JobProducer not connected")
        job = await validate_job_paths(job)
        key = _status_key(job.job_id)
        payload = job.model_dump_json()
        for _ in range(_WATCH_RETRIES):
            try:
                async with self._client.pipeline(transaction=True) as pipe:
                    await pipe.watch(key)
                    previous = await pipe.hgetall(key)
                    if previous:
                        if previous.get("payload") != payload:
                            raise ValueError("Job ID already belongs to a different payload")
                        if previous.get("stream_id"):
                            return previous["stream_id"]
                    initial = _base_status(job, "pending", "")
                    initial.pop("stream_id")
                    initial.update({"attempts": "0", "attempt_history": "[]", "terminal": "0"})
                    pipe.multi()
                    pipe.hset(key, mapping=initial)
                    pipe.persist(key)
                    pipe.xadd(STREAM_KEY, {"payload": payload})
                    results = await pipe.execute()
                stream_id = results[-1]
                # A fast consumer may already have advanced the state.
                await self._client.hsetnx(key, "stream_id", stream_id)
                log.info("mq.published", job_id=job.job_id, stream_id=stream_id)
                return stream_id
            except WatchError:
                continue
        raise RuntimeError("Concurrent modification while publishing job")

    async def get_job_status(self, job_id: str) -> dict | None:
        if not self._client:
            raise RuntimeError("JobProducer not connected")
        return await self._client.hgetall(_status_key(job_id)) or None


class JobConsumer:
    """Recover PEL entries fairly and fence processing with a per-message lease."""

    def __init__(
        self,
        redis_url: str,
        consumer_name: str | None = None,
        *,
        lease_seconds: float = LEASE_SECONDS,
    ) -> None:
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive and finite")
        self._redis_url = redis_url
        self._consumer_name = consumer_name or f"{CONSUMER_NAME}-{uuid.uuid4().hex}"
        self._client: aioredis.Redis | None = None
        self._lease_ms = max(1, math.ceil(lease_seconds * 1000))
        self.heartbeat_interval = min(20.0, lease_seconds / 3)
        self._pending_cursor = "-"
        self._cleanup_cursor = 0

    def _connected(self) -> aioredis.Redis:
        if self._client is None:
            raise RuntimeError("JobConsumer not connected")
        return self._client

    async def connect(self) -> None:
        self._client = aioredis.from_url(
            self._redis_url, decode_responses=True, socket_connect_timeout=5,
            socket_timeout=10, encoding_errors="replace",
        )
        await self._ensure_group()

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()

    async def consume(
        self, count: int = 1, block_ms: int = 3_000
    ) -> list[tuple[str, IndexJob]]:
        """Replay eligible pending jobs before requesting new deliveries.

        Pending scans have a cursor so many live leases cannot hide an abandoned
        message at the end of the PEL. XCLAIM only transfers delivery ownership;
        set_job_processing must acquire the lease before doing any work.
        """
        if count < 1 or block_ms < 1:
            raise ValueError("count and block_ms must be positive")
        client = self._connected()
        await self._cleanup_finished()
        pending = await client.xpending_range(
            STREAM_KEY, CONSUMER_GROUP, self._pending_cursor, "+",
            count=RECOVERY_BATCH_SIZE,
        )
        jobs: list[tuple[str, IndexJob]] = []
        for entry in pending:
            message_id = entry["message_id"]
            self._pending_cursor = f"({message_id}"
            messages = await client.xrange(STREAM_KEY, message_id, message_id, count=1)
            if not messages:
                if entry["time_since_delivered"] >= self._lease_ms:
                    known = next(
                        (state for state in await self._all_statuses()
                         if state.get("stream_id") == message_id),
                        None,
                    )
                    await self._quarantine(
                        message_id, "", "Pending stream payload is missing",
                        known_job_id=known["job_id"] if known else None,
                    )
                continue
            job = await self._decode(message_id, messages[0][1])
            if job is not None and await self._recover(
                message_id, job, entry, self._lease_ms, claim=True
            ):
                jobs.append((message_id, job))
                if len(jobs) >= count:
                    if message_id == pending[-1]["message_id"] and len(pending) < RECOVERY_BATCH_SIZE:
                        self._pending_cursor = "-"
                    return jobs
        if len(pending) < RECOVERY_BATCH_SIZE:
            self._pending_cursor = "-"
        results = await client.xreadgroup(
            CONSUMER_GROUP, self._consumer_name, {STREAM_KEY: ">"},
            count=count, block=block_ms,
        )
        for _stream, messages in results or []:
            for message_id, data in messages:
                job = await self._decode(message_id, data)
                if job is not None:
                    jobs.append((message_id, job))
        return jobs

    async def _decode(self, message_id: str, data: dict) -> IndexJob | None:
        try:
            return IndexJob.model_validate_json(data["payload"])
        except (ValueError, TypeError, KeyError) as exc:
            await self._quarantine(message_id, data.get("payload", ""), f"Invalid payload: {exc}")
            return None

    async def _quarantine(
        self, message_id: str, payload: str, error: str, *, known_job_id: str | None = None
    ) -> None:
        client = self._connected()
        job_id = known_job_id or f"unreadable-{message_id}"
        key, lease = _status_key(job_id), _lease_key(message_id)
        for _ in range(_WATCH_RETRIES):
            try:
                async with client.pipeline(transaction=True) as pipe:
                    await pipe.watch(key, lease)
                    if await pipe.get(lease):
                        return
                    previous = await pipe.hgetall(key)
                    if previous.get("stream_id") not in (None, "", message_id):
                        return
                    if not _terminal(previous):
                        pipe.multi()
                        pipe.hset(key, mapping={
                            "job_id": job_id, "stream_id": message_id,
                            "payload": previous.get("payload") or payload,
                            "status": "failed", "terminal": "1", "dead_letter": "1",
                            "error": error, "attempts": str(_attempt_count(previous)),
                            "attempt_history": _finish_history(previous, _attempt_count(previous), error),
                            "updated_at": _now(),
                        })
                        pipe.persist(key)
                        pipe.sadd(CLEANUP_KEY, job_id)
                        await pipe.execute()
                await self.ack(message_id, job_id)
                return
            except WatchError:
                continue
        raise RuntimeError("Concurrent modification while quarantining message")

    async def set_job_processing(self, job: IndexJob, message_id: str) -> str | None:
        client = self._connected()
        key, lease = _status_key(job.job_id), _lease_key(message_id)
        token = uuid.uuid4().hex
        for _ in range(_WATCH_RETRIES):
            try:
                async with client.pipeline(transaction=True) as pipe:
                    await pipe.watch(key, lease)
                    status = await pipe.hgetall(key)
                    if status.get("stream_id") not in (None, "", message_id):
                        raise ValueError("Duplicate job ID on a different stream entry")
                    if _terminal(status):
                        await self.ack(message_id, job.job_id)
                        return None
                    if await pipe.get(lease):
                        return None
                    if status.get("status") == "retrying" and not _retry_due(
                        status, await _server_time_ms(pipe)
                    ):
                        return None
                    if not await pipe.xpending_range(
                        STREAM_KEY, CONSUMER_GROUP, message_id, message_id, count=1
                    ):
                        return None
                    attempts = _attempt_count(status)
                    if attempts >= job.max_attempts:
                        failed = _base_status(job, "failed", message_id)
                        failed.update({
                            "terminal": "1", "dead_letter": "1",
                            "error": status.get("error") or "Retry budget exhausted",
                        })
                        pipe.multi()
                        pipe.hset(key, mapping=failed)
                        pipe.persist(key)
                        pipe.sadd(CLEANUP_KEY, job.job_id)
                        await pipe.execute()
                    else:
                        history = _history(status)
                        history.append({"attempt": attempts + 1, "started_at": _now()})
                        processing = _base_status(job, "processing", message_id)
                        processing.update({
                            "attempts": str(attempts + 1), "terminal": "0",
                            "attempt_history": json.dumps(history), "heartbeat_at": _now(),
                            "consumer": self._consumer_name,
                        })
                        pipe.multi()
                        pipe.hset(key, mapping=processing)
                        pipe.persist(key)
                        pipe.set(lease, token, px=self._lease_ms)
                        pipe.xclaim(STREAM_KEY, CONSUMER_GROUP, self._consumer_name,
                                    0, [message_id], justid=True)
                        await pipe.execute()
                        return token
                await self.ack(message_id, job.job_id)
                return None
            except WatchError:
                continue
        raise RuntimeError("Concurrent modification while acquiring job lease")

    async def heartbeat(self, job: IndexJob, message_id: str, token: str) -> bool:
        client = self._connected()
        key, lease = _status_key(job.job_id), _lease_key(message_id)
        for _ in range(_WATCH_RETRIES):
            try:
                async with client.pipeline(transaction=True) as pipe:
                    await pipe.watch(lease, key)
                    if await pipe.get(lease) != token:
                        return False
                    pipe.multi()
                    pipe.pexpire(lease, self._lease_ms)
                    pipe.hset(key, mapping={"heartbeat_at": _now(), "updated_at": _now()})
                    pipe.persist(key)
                    pipe.xclaim(STREAM_KEY, CONSUMER_GROUP, self._consumer_name,
                                0, [message_id], justid=True)
                    await pipe.execute()
                    return True
            except WatchError:
                continue
        return False

    async def set_job_done(
        self, job: IndexJob, stats: dict, message_id: str, lease_token: str
    ) -> None:
        require_success_stats(stats)
        await self._finish(job, message_id, lease_token, stats=stats)

    async def set_job_failed(
        self, job: IndexJob, error: str, message_id: str, lease_token: str
    ) -> str:
        return await self._finish(job, message_id, lease_token, error=error)

    async def _finish(
        self, job: IndexJob, message_id: str, token: str,
        *, stats: dict | None = None, error: str | None = None,
    ) -> str:
        client = self._connected()
        key, lease = _status_key(job.job_id), _lease_key(message_id)
        if error is not None:
            error = error or "Indexing failed without error details"
        for _ in range(_WATCH_RETRIES):
            try:
                async with client.pipeline(transaction=True) as pipe:
                    await pipe.watch(key, lease)
                    if await pipe.get(lease) != token:
                        raise LeaseLostError("Job lease expired or changed before finalization")
                    previous = await pipe.hgetall(key)
                    attempts = _attempt_count(previous)
                    state = "done" if error is None else (
                        "failed" if attempts >= job.max_attempts else "retrying"
                    )
                    finished = _base_status(job, state, message_id)
                    finished["attempt_history"] = _finish_history(previous, attempts, error)
                    if error is not None:
                        finished["error"] = error
                    else:
                        finished.update({"error": "", "next_retry_at_ms": "0", "recovery_action": ""})
                    if stats is not None:
                        finished["stats"] = json.dumps(stats)
                        finished["errors"] = json.dumps(stats.get("errors", 0))
                        reserved = set(finished) | {
                            "attempts", "terminal", "dead_letter", "next_retry_at_ms",
                        }
                        finished.update({k: str(v) for k, v in stats.items() if k not in reserved})
                    if state == "retrying":
                        delay_ms = min(30, 2 ** max(0, attempts - 1)) * 1000
                        finished["next_retry_at_ms"] = str(await _server_time_ms(pipe) + delay_ms)
                    else:
                        finished.update({"terminal": "1", "dead_letter": "1" if error else "0"})
                    pipe.multi()
                    pipe.hset(key, mapping=finished)
                    pipe.persist(key)
                    if state != "retrying":
                        pipe.sadd(CLEANUP_KEY, job.job_id)
                    pipe.delete(lease)
                    await pipe.execute()
                if state != "retrying":
                    await self.ack(message_id, job.job_id)
                return state
            except WatchError:
                continue
        raise LeaseLostError("Concurrent modification while finalizing job")

    async def ack(self, message_id: str, job_id: str) -> None:
        """ACK only a durable terminal record, then delete only this entry."""
        client = self._connected()
        key = _status_key(job_id)
        status = await client.hgetall(key)
        if not _terminal(status) or status.get("stream_id") != message_id:
            raise RuntimeError("Refusing to ACK a job without matching durable terminal state")
        await client.persist(key)
        if not _terminal(await client.hgetall(key)):
            raise RuntimeError("Terminal state expired before ACK")
        await client.sadd(CLEANUP_KEY, job_id)
        await client.xack(STREAM_KEY, CONSUMER_GROUP, message_id)
        await client.xdel(STREAM_KEY, message_id)
        if status["status"] == "done":
            await client.expire(key, STATUS_TTL_SEC)
        await client.srem(CLEANUP_KEY, job_id)
        log.info("mq.terminal_cleaned", stream_id=message_id, status=status["status"])

    async def _cleanup_finished(self) -> None:
        client = self._connected()
        self._cleanup_cursor, job_ids = await client.sscan(
            CLEANUP_KEY, self._cleanup_cursor, count=RECOVERY_BATCH_SIZE
        )
        for job_id in job_ids:
            status = await client.hgetall(_status_key(job_id))
            if _terminal(status) and status.get("stream_id"):
                await self.ack(status["stream_id"], job_id)
            elif not status:
                await client.srem(CLEANUP_KEY, job_id)

    async def _recover(
        self, message_id: str, job: IndexJob, entry: dict, min_idle_ms: int, *, claim: bool
    ) -> bool:
        client = self._connected()
        key, lease = _status_key(job.job_id), _lease_key(message_id)
        for _ in range(_WATCH_RETRIES):
            try:
                async with client.pipeline(transaction=True) as pipe:
                    await pipe.watch(key, lease)
                    if await pipe.get(lease):
                        return False
                    previous = await pipe.hgetall(key)
                    if previous.get("stream_id") not in (None, "", message_id):
                        await self._quarantine(
                            message_id, job.model_dump_json(),
                            "Duplicate job ID on a different stream entry",
                        )
                        return False
                    if _terminal(previous):
                        await self.ack(message_id, job.job_id)
                        return False
                    now_ms = await _server_time_ms(pipe)
                    if previous.get("status") == "retrying":
                        if not claim or not _retry_due(previous, now_ms):
                            return False
                        recovered = None
                    else:
                        if entry["time_since_delivered"] < min_idle_ms:
                            return False
                        attempts = _attempt_count(previous, max(1, entry["times_delivered"]))
                        error = (
                            previous.get("error") if previous.get("status") == "failed" else None
                        ) or "Recovered abandoned delivery without a live lease"
                        recovered = _base_status(job, "retrying", message_id)
                        recovered.update({
                            "attempts": str(attempts), "error": error, "next_retry_at_ms": str(now_ms),
                            "attempt_history": (
                                _finish_history(previous, attempts, error)
                                if attempts else json.dumps(_history(previous))
                            ),
                            "recovery_action": "pending_replay",
                        })
                        if attempts >= job.max_attempts:
                            recovered.update({"status": "failed", "terminal": "1", "dead_letter": "1"})
                    pipe.multi()
                    if recovered is not None:
                        pipe.hset(key, mapping=recovered)
                        pipe.persist(key)
                    terminal = recovered is not None and _terminal(recovered)
                    if terminal:
                        pipe.sadd(CLEANUP_KEY, job.job_id)
                    elif claim:
                        pipe.xclaim(STREAM_KEY, CONSUMER_GROUP, self._consumer_name,
                                    0, [message_id], justid=True)
                    results = await pipe.execute()
                if terminal:
                    await self.ack(message_id, job.job_id)
                    return False
                return bool(results[-1]) if claim else True
            except WatchError:
                continue
        return False

    async def get_job_status(self, job_id: str) -> dict | None:
        return await self._connected().hgetall(_status_key(job_id)) or None

    async def _all_statuses(self) -> list[dict]:
        client = self._connected()
        jobs: list[dict] = []
        cursor = 0
        while True:
            cursor, keys = await client.scan(cursor, match=f"{STATUS_KEY_PREFIX}*", count=100)
            for key in keys:
                status = await client.hgetall(key)
                if status:
                    status.setdefault("job_id", key[len(STATUS_KEY_PREFIX):])
                    jobs.append(dict(status))
            if cursor == 0:
                return jobs

    async def get_jobs_by_repo(self, repo_path: str) -> list[dict]:
        jobs = [
            job for job in await self._all_statuses()
            if job.get("repo_path", "").lower() == repo_path.lower()
        ]
        return sorted(jobs, key=lambda job: job.get("updated_at", ""), reverse=True)

    async def recover_stale_jobs_by_repo(
        self, repo_paths: list[str], stale_after_sec: int
    ) -> list[dict]:
        """Make abandoned PEL entries eligible for replay; never reenter a lease.

        Scan stream payloads rather than expiring status keys, including legacy
        entries whose seven-day status TTL elapsed. Keep the original stream ID
        and attempt history; only exhausted jobs move to durable failed state.
        """
        client = self._connected()
        repos = {path.lower() for path in repo_paths if path}
        cursor = "-"
        recovered = []
        while True:
            entries = await client.xpending_range(
                STREAM_KEY, CONSUMER_GROUP, cursor, "+", count=RECOVERY_BATCH_SIZE
            )
            for entry in entries:
                message_id = entry["message_id"]
                cursor = f"({message_id}"
                messages = await client.xrange(STREAM_KEY, message_id, message_id, count=1)
                if not messages:
                    continue
                try:
                    job = IndexJob.model_validate_json(messages[0][1]["payload"])
                except (ValueError, TypeError, KeyError):
                    continue
                if job.repo_path.lower() not in repos:
                    continue
                if await self._recover(
                    message_id, job, entry, max(self._lease_ms, stale_after_sec * 1000),
                    claim=False,
                ):
                    recovered.append(await self.get_job_status(job.job_id))
            if len(entries) < RECOVERY_BATCH_SIZE:
                return recovered

    async def get_queue_snapshot(self) -> dict:
        jobs = await self._all_statuses()
        pending = [job for job in jobs if job.get("status") in {"pending", "retrying"}]
        processing = [job for job in jobs if job.get("status") == "processing"]
        durations = []
        for job in jobs:
            if job.get("status") != "done":
                continue
            created, updated = _parse_iso_utc(job.get("created_at")), _parse_iso_utc(job.get("updated_at"))
            if created and updated:
                seconds = (updated - created).total_seconds()
                if 0 < seconds < 7200:
                    durations.append(seconds)
        return {
            "pending_jobs": sorted(pending, key=lambda job: job.get("created_at", "")),
            "processing_jobs": sorted(processing, key=lambda job: job.get("created_at", "")),
            "failed_jobs": [job for job in jobs if job.get("status") == "failed"],
            "avg_duration_sec": int(sum(durations) / len(durations)) if durations else 30,
        }

    async def _ensure_group(self) -> None:
        try:
            await self._connected().xgroup_create(STREAM_KEY, CONSUMER_GROUP, id="0", mkstream=True)
        except aioredis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
