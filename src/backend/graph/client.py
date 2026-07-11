"""FalkorDB connection wrapper.

Provides a thin synchronous client that:
- Manages a single FalkorDB connection.
- Exposes a .query() method for Cypher execution.
- Creates property indexes on first connect for query performance.
"""

from __future__ import annotations

import structlog
import falkordb

log = structlog.get_logger()

GRAPH_NAME = "contextgraph"


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

    def connect(self) -> None:
        self._db = falkordb.FalkorDB(host=self._host, port=self._port)
        self._graph = self._db.select_graph(self._graph_name)
        log.info("graph.connected", host=self._host, port=self._port, graph=self._graph_name)

    def close(self) -> None:
        if self._db:
            try:
                self._db.connection.close()
            except Exception:
                pass

    def delete(self) -> None:
        """Delete the connected FalkorDB graph."""
        if not self._graph:
            raise RuntimeError("GraphClient not connected - call connect() first")
        self._graph.delete()

    def query(self, cypher: str, params: dict | None = None, timeout: int | None = None):
        if not self._graph:
            raise RuntimeError("GraphClient not connected – call connect() first")
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
            except Exception:
                pass  # Index already exists – safe to ignore
