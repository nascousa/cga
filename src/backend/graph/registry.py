"""Per-project GraphClient registry with ContextVar-based routing.

Each project gets its own FalkorDB graph named after the project_name
(e.g. 'osagent', 'browseragent').  The registry lazily connects and
caches one GraphClient per project_name.

The ContextVar ``_current_project_name`` is set by ProjectTokenMiddleware
on each /mcp request, allowing tool functions to obtain the right graph
for the authenticated project without thread-locals or explicit passing.
"""

from __future__ import annotations

from contextvars import ContextVar

import structlog

from backend.graph.client import GraphClient

log = structlog.get_logger()

# Default matches the legacy graph name so old data is not lost
_current_project_name: ContextVar[str] = ContextVar(
    "current_project_name", default="contextgraph"
)


class GraphRegistry:
    """Maintains one FalkorDB GraphClient per project_name."""

    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        self._graphs: dict[str, GraphClient] = {}

    def get(self, project_name: str) -> GraphClient:
        """Return (and lazily connect) the GraphClient for *project_name*."""
        project_name = project_name.strip().lower()
        if project_name not in self._graphs:
            g = GraphClient(
                host=self._host,
                port=self._port,
                graph_name=project_name,
            )
            g.connect()
            g.ensure_indexes()
            log.info("graph.registry.connected", project_name=project_name)
            self._graphs[project_name] = g
        return self._graphs[project_name]

    def current(self) -> GraphClient:
        """Return the GraphClient for the project active in the current context."""
        return self.get(_current_project_name.get())

    def delete(self, project_name: str) -> None:
        """Delete a graph and evict its cached client."""
        project_name = project_name.strip().lower()
        graph = self.get(project_name)
        graph.delete()
        graph.close()
        self._graphs.pop(project_name, None)
        log.info("graph.registry.deleted", project_name=project_name)

    def close_all(self) -> None:
        for g in self._graphs.values():
            g.close()
        self._graphs.clear()
        log.info("graph.registry.closed_all")
