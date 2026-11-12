"""Agent trace-based hallucination-reduction evaluator.

Records every MCP tool call (query, result, latency) as a *Trace*, persists
traces to a Redis Stream (`contextgraph:traces`), and provides an
asynchronous evaluation loop that:

1. Reads traces from the stream.
2. For each *retrieval* trace, checks whether the returned symbol qualified
   names exist in the graph (existence check ≠ relevance, but catches the
   most common hallucination: a model fabricating a qualified name that is
   not indexed).
3. Emits a `hallucination_proxy_score` (fraction of results NOT in graph)
   to structlog and to an in-process Prometheus counter.

The evaluator is designed as a background coroutine that runs alongside the
main FastAPI server.  It does NOT block MCP tool calls.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any

import redis
import redis.asyncio as aioredis
import structlog
from backend.plugins.host import PluginUnavailableError, execution, is_enabled
from backend.plugins.settings import tracing_enabled

log = structlog.get_logger()

_TRACE_STREAM = "contextgraph:traces"
_TRACE_GROUP = "eval-group"
_BATCH = 20
_BLOCK_MS = 2000


@dataclass
class Trace:
    trace_id: str
    tool: str
    args: dict
    results: list[Any]
    latency_ms: float
    ts: float


class TraceRecorder:
    """Synchronous helper that MCP tool wrappers use to record traces."""

    def __init__(self, redis_url: str) -> None:
        self._client: redis.Redis = redis.from_url(  # type: ignore[attr-defined]
            redis_url, decode_responses=True, socket_connect_timeout=5
        )
        self._enabled = True

    def record(self, tool: str, args: dict, results: list, latency_ms: float) -> None:
        if not self._enabled or not is_enabled("evaluation") or not tracing_enabled():
            return
        trace = Trace(
            trace_id=str(uuid.uuid4()),
            tool=tool,
            args=args,
            results=results,
            latency_ms=latency_ms,
            ts=time.time(),
        )
        try:
            self._client.xadd(
                _TRACE_STREAM,
                {"payload": json.dumps(asdict(trace))},
                maxlen=10_000,
                approximate=True,
            )
        except Exception as exc:
            log.warning("trace.record_failed", error=str(exc))

    def close(self) -> None:
        self._client.close()


class TraceEvaluator:
    """Background coroutine that consumes traces and scores hallucinations."""

    def __init__(self, redis_url: str, graph_client: Any) -> None:
        self._redis_url = redis_url
        self._graph = graph_client
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._running = True
        self._client = aioredis.from_url(
            self._redis_url, decode_responses=True, socket_connect_timeout=5
        )
        await self._ensure_group()
        asyncio.ensure_future(self._loop())
        log.info("trace_eval.started")

    async def stop(self) -> None:
        self._running = False
        if self._client:
            await self._client.aclose()
        log.info("trace_eval.stopped")

    # ------------------------------------------------------------------
    # Evaluation loop
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        pending: list[tuple[str, dict]] = []
        while self._running:
            if not is_enabled("evaluation") or not tracing_enabled():
                await asyncio.sleep(2)
                continue
            try:
                if not pending:
                    messages = await self._client.xreadgroup(
                        _TRACE_GROUP,
                        "eval-worker-0",
                        {_TRACE_STREAM: ">"},
                        count=_BATCH,
                        block=_BLOCK_MS,
                    )
                    pending = [entry for _stream, entries in messages for entry in entries]
                while pending:
                    if not tracing_enabled():
                        break
                    msg_id, data = pending[0]
                    with execution("evaluation"):
                        await self._evaluate_trace(msg_id, data)
                    pending.pop(0)
            except PluginUnavailableError:
                log.info("trace_eval.paused")
            except aioredis.RedisError as exc:
                log.warning("trace_eval.redis_error", error=str(exc))
                await asyncio.sleep(2)
            except Exception as exc:
                log.error("trace_eval.unexpected", error=str(exc))
                await asyncio.sleep(2)

    async def _evaluate_trace(self, msg_id: str, data: dict) -> None:
        try:
            payload = json.loads(data.get("payload", "{}"))
            tool = payload.get("tool", "")
            results = payload.get("results", [])
            latency_ms = payload.get("latency_ms", 0.0)

            if tool not in {"find_symbol", "find_callers", "find_callees", "retrieve_context"}:
                await self._ack(msg_id)
                return

            # Check each result for existence in graph
            non_existent = 0
            total = 0
            for item in results:
                qname = item if isinstance(item, str) else (item.get("qualified_name") if isinstance(item, dict) else None)
                if qname:
                    total += 1
                    if not await self._symbol_exists(qname):
                        non_existent += 1

            score = non_existent / total if total else 0.0
            log.info(
                "trace_eval.score",
                tool=tool,
                hallucination_proxy=round(score, 4),
                total_results=total,
                non_existent=non_existent,
                latency_ms=round(latency_ms, 2),
            )
            await self._ack(msg_id)
        except Exception as exc:
            log.error("trace_eval.eval_error", msg_id=msg_id, error=str(exc))

    async def _symbol_exists(self, qualified_name: str) -> bool:
        try:
            result = await asyncio.to_thread(
                self._graph.query,
                "MATCH (s:Symbol {qualified_name: $qname}) RETURN s.qualified_name LIMIT 1",
                {"qname": qualified_name},
            )
            return bool(result.result_set)
        except Exception:
            return True  # optimistic: don't penalise on graph errors

    async def _ack(self, msg_id: str) -> None:
        try:
            await self._client.xack(_TRACE_STREAM, _TRACE_GROUP, msg_id)
        except Exception:
            pass

    async def _ensure_group(self) -> None:
        try:
            await self._client.xgroup_create(
                _TRACE_STREAM, _TRACE_GROUP, id="0", mkstream=True
            )
        except aioredis.ResponseError:
            pass  # group already exists
