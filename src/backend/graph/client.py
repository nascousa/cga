"""FalkorDB connection wrapper.

Provides a thin synchronous client that:
- Manages a single FalkorDB connection.
- Exposes a .query() method for Cypher execution.
- Creates property indexes on first connect for query performance.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import threading
import time
import uuid

import redis
import structlog
import falkordb

log = structlog.get_logger()

GRAPH_NAME = "contextgraph"
_WRITE_LEASE_SECONDS = 120
_PUBLISH_GRAPH = """
if redis.call('GET', KEYS[3]) ~= ARGV[1] then
    return redis.error_reply('graph write lease lost')
end
redis.call('PERSIST', KEYS[1])
redis.call('RENAME', KEYS[1], KEYS[2])
redis.call('SET', KEYS[4], ARGV[2])
return 1
"""
_DELETE_PROMOTED_GRAPH = """
if redis.call('GET', KEYS[5]) ~= ARGV[1] then
    return redis.error_reply('graph write lease lost')
end
if (redis.call('GET', KEYS[2]) or '0') ~= ARGV[2]
    or (redis.call('GET', KEYS[4]) or '0') ~= ARGV[3]
    or redis.call('EXISTS', KEYS[1]) == 0
    or redis.call('EXISTS', KEYS[3]) == 0 then
    return 0
end
redis.call('UNLINK', KEYS[1])
redis.call('SET', KEYS[2], ARGV[4])
return 1
"""

class GraphGenerationChanged(RuntimeError):
    """A newer committed source must not be deleted by an older promotion."""


class GraphClient:
    def __init__(
        self,
        host: str = "localhost",
        port: int = 6379,
        graph_name: str = GRAPH_NAME,
    ) -> None:
        self._host = host
        self._port = port
        self._graph_name = graph_name
        self._db: falkordb.FalkorDB | None = None
        self._graph: falkordb.Graph | None = None
        self._query_lock = threading.RLock()
        self._generation: str | None = None
        self._staging = False
        self._discarded = False
        self._published_generation: str | None = None

    def connect(self) -> None:
        self._db = falkordb.FalkorDB(host=self._host, port=self._port)
        self._graph = self._db.select_graph(self._graph_name)
        log.info("graph.connected", host=self._host, port=self._port, graph=self._graph_name)

    def close(self) -> None:
        if self._db:
            try:
                self._db.connection.close()
            except (redis.RedisError, OSError) as exc:
                log.warning("graph.close_failed", graph=self._graph_name, error=str(exc))

    def delete(
        self, *, expected_generation: str | None = None,
        expected_target_graph: str | None = None,
        expected_target_generation: str | None = None,
    ) -> None:
        """Delete the connected FalkorDB graph."""
        guarded_target = expected_target_graph is not None or expected_target_generation is not None
        if guarded_target and (
            not expected_generation or not expected_target_graph or not expected_target_generation
            or expected_target_graph == self._graph_name
        ):
            raise ValueError("Promotion deletion requires distinct graphs and both generations")
        if not self._graph:
            raise RuntimeError("GraphClient not connected - call connect() first")
        if self._db is None:
            if expected_generation is not None or guarded_target:
                raise RuntimeError("Cannot verify the graph generation without a connection")
            self._graph.delete()
            return
        with self._db.connection.lock(
            self._lease_key, timeout=_WRITE_LEASE_SECONDS, blocking_timeout=30
        ) as lock:
            if guarded_target:
                deleted = self._db.connection.eval(
                    _DELETE_PROMOTED_GRAPH, 5,
                    self._graph_name, self._generation_key,
                    expected_target_graph, f"cga:graph:generation:{expected_target_graph}",
                    self._lease_key, lock.local.token,
                    expected_generation, expected_target_generation, uuid.uuid4().hex,
                )
                if deleted != 1:
                    raise GraphGenerationChanged("Source or target changed during promotion; source retained")
                return
            if expected_generation is not None and self.cache_generation() != expected_generation:
                raise GraphGenerationChanged("Graph changed during promotion; source graph was retained")
            self._graph.delete()
            self._db.connection.set(self._generation_key, uuid.uuid4().hex)

    @property
    def _lease_key(self) -> str:
        return f"cga:graph:write:{self._graph_name}"

    @property
    def _generation_key(self) -> str:
        return f"cga:graph:generation:{self._graph_name}"

    def cache_generation(self) -> str:
        if self._db is None:
            raise RuntimeError("GraphClient not connected - call connect() first")
        value = self._db.connection.get(self._generation_key)
        return value.decode("ascii") if isinstance(value, bytes) else str(value or "0")

    @property
    def published_generation(self) -> str | None:
        return self._published_generation

    def discard_update(self) -> None:
        if not self._staging:
            raise RuntimeError("Only a staged generation can be discarded")
        self._discarded = True

    @contextmanager
    def atomic_update(self) -> Iterator[GraphClient]:
        """Build privately, then publish the complete generation under a write lease."""
        if self._db is None or self._graph is None:
            raise RuntimeError("GraphClient not connected - call connect() first")
        connection = self._db.connection
        lock = connection.lock(
            self._lease_key,
            timeout=_WRITE_LEASE_SECONDS,
            blocking_timeout=30,
            thread_local=False,
        )
        if not lock.acquire():
            raise TimeoutError("Timed out acquiring graph write lease")
        name = f"__cga_stage__{uuid.uuid4().hex}"
        stop = threading.Event()
        lease_errors: list[redis.RedisError] = []

        def renew() -> None:
            while not stop.wait(_WRITE_LEASE_SECONDS / 3):
                try:
                    lock.extend(_WRITE_LEASE_SECONDS, replace_ttl=True)
                    connection.expire(name, _WRITE_LEASE_SECONDS * 3)
                except redis.RedisError as exc:
                    lease_errors.append(exc)
                    log.error("graph.write_lease_lost", graph=self._graph_name, error=str(exc))
                    return

        heartbeat = threading.Thread(target=renew, name="cga-graph-lease", daemon=True)
        heartbeat.start()
        try:
            stage = GraphClient(self._host, self._port, name)
            stage._db = self._db
            stage._staging = True
            if connection.exists(self._graph_name):
                deadline = time.monotonic() + 30
                delay = 0.05
                while True:
                    try:
                        stage._graph = self._graph.copy(name)
                        break
                    except redis.ResponseError as exc:
                        if (
                            "GRAPH.COPY failed, could not fork" not in str(exc)
                            or time.monotonic() >= deadline
                            or connection.exists(name)
                        ):
                            raise
                        log.warning("graph.copy_fork_busy", graph=self._graph_name, retry_in=delay)
                        time.sleep(delay)
                        delay = min(delay * 2, 1.0)
            else:
                stage._graph = self._db.select_graph(name)
                stage.ensure_indexes()
            connection.expire(name, _WRITE_LEASE_SECONDS * 3)
            yield stage
            if stage._discarded:
                return
            if lease_errors:
                raise RuntimeError("Graph write lease was lost; generation not published") from lease_errors[0]
            generation = uuid.uuid4().hex
            connection.eval(
                _PUBLISH_GRAPH,
                4,
                name,
                self._graph_name,
                self._lease_key,
                self._generation_key,
                lock.local.token,
                generation,
            )
            stage._published_generation = generation
        finally:
            stop.set()
            heartbeat.join(timeout=_WRITE_LEASE_SECONDS)
            try:
                connection.unlink(name)
            except redis.RedisError as exc:
                log.error("graph.stage_cleanup_failed", graph=name, error=str(exc))
            try:
                lock.release()
            except redis.RedisError as exc:
                log.error("graph.write_lease_release_failed", graph=self._graph_name, error=str(exc))

    def query(self, cypher: str, params: dict | None = None, timeout: int | None = None):
        if not self._graph:
            raise RuntimeError("GraphClient not connected – call connect() first")
        with self._query_lock:
            if self._db is not None and not self._staging:
                generation = self.cache_generation()
                if generation != self._generation:
                    self._graph.schema.clear()
                    self._generation = generation
            return self._graph.query(cypher, params or {}, timeout=timeout)

    def ensure_indexes(self) -> None:
        """Idempotently create FalkorDB property indexes."""
        stmts = [
            "CREATE INDEX FOR (n:File) ON (n.path)",
            "CREATE INDEX FOR (n:Symbol) ON (n.name)",
            "CREATE INDEX FOR (n:Symbol) ON (n.qualified_name)",
            "CREATE INDEX FOR (n:Variable) ON (n.name)",
            "CREATE INDEX FOR (n:Variable) ON (n.qualified_name)",
            "CREATE INDEX FOR (n:Variable) ON (n.scope_qname)",
            "CREATE INDEX FOR (n:Repository) ON (n.path)",
        ]
        for stmt in stmts:
            try:
                self.query(stmt)
            except (redis.ResponseError, RuntimeError) as exc:
                if "already exists" not in str(exc).lower():
                    raise
