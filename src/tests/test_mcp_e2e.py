"""End-to-end MCP toolflow test against a live FalkorDB graph."""

from __future__ import annotations

import os
import textwrap
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from backend.graph.client import GraphClient
from backend.indexer.pipeline import IndexPipeline
from backend.indexer.parser import path_to_module
import backend.tools.server as mcp_srv
from backend.auth.context import ProjectScope, bind_project_scope


pytestmark = [pytest.mark.live_graph, pytest.mark.live_graph_e2e]


def _connect_live_graph() -> GraphClient:
    client = GraphClient(
        host=os.getenv("FALKORDB_HOST", "localhost"),
        port=int(os.getenv("FALKORDB_PORT", "16379")),
        graph_name=f"mcp_e2e_{uuid.uuid4().hex}",
    )
    try:
        client.connect()
        client.ensure_indexes()
        client.query("MATCH (n) DETACH DELETE n")
    except Exception as exc:
        client.close()
        pytest.skip(f"live FalkorDB unavailable: {exc}")
    return client


def test_mcp_tools_work_on_real_indexed_graph(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)
    file_path = repo_root / "service.py"
    file_path.write_text(
        textwrap.dedent(
            """
            def normalize(raw):
                value = raw.strip()
                return value

            def render(input_text):
                cleaned = normalize(input_text)
                return cleaned
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    graph = _connect_live_graph()
    old_state = (mcp_srv._registry, mcp_srv._graph, mcp_srv._producer, mcp_srv._cache, mcp_srv._recorder)

    try:
        pipeline = IndexPipeline(graph)
        stats = pipeline.index_full(str(repo_root))
        assert stats["files"] == 1

        registry = MagicMock()
        registry.current.return_value = graph
        registry.get.return_value = graph
        mcp_srv.init(registry=registry, producer=MagicMock(), cache=None, recorder=None)
        module_qname = path_to_module(str(file_path))
        render_scope = f"{module_qname}.render"

        with bind_project_scope(ProjectScope("E2E", 1, graph._graph_name, str(repo_root))):
            flows = mcp_srv.get_variable_flows(render_scope, limit=20)
            assert any(item["flow_type"] == "argument" for item in flows)
            assert any(item["flow_type"] == "call_return" for item in flows)

            explanation = mcp_srv.explain_data_flow(render_scope, limit=20)
            assert explanation["scope_qname"] == render_scope
            assert "narrative" in explanation and explanation["narrative"]
            assert "Return value is influenced by these inputs" in explanation["narrative"]
            assert explanation["narrative"].isascii()

            influence = mcp_srv.analyze_return_influence(render_scope, limit=10)
            assert any(param.endswith(":input_text") for param in influence["influenced_by_parameters"])
    finally:
        try:
            graph.delete()
        except Exception:
            pass
        graph.close()
        mcp_srv._registry, mcp_srv._graph, mcp_srv._producer, mcp_srv._cache, mcp_srv._recorder = old_state