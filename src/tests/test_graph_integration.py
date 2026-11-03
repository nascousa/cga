"""Live FalkorDB integration tests for indexing and graph queries."""

from __future__ import annotations

import os
import textwrap
import uuid
from pathlib import Path

import pytest

from backend.graph.client import GraphClient
from backend.graph import schema as S
from backend.indexer.parser import path_to_module
from backend.indexer.pipeline import IndexPipeline


pytestmark = [pytest.mark.live_graph, pytest.mark.live_graph_smoke]


def _connect_live_graph() -> GraphClient:
    client = GraphClient(
        host=os.getenv("FALKORDB_HOST", "localhost"),
        port=int(os.getenv("FALKORDB_PORT", "16379")),
        graph_name=f"graph_integration_{uuid.uuid4().hex}",
    )
    try:
        client.connect()
        client.ensure_indexes()
        client.delete()
    except Exception as exc:
        client.close()
        pytest.skip(f"live FalkorDB unavailable: {exc}")
    return client


def _write_sample_repo(repo_root: Path) -> Path:
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
    return file_path


def test_scope_parameter_lookup_uses_symbol_index_and_preserves_argument_order():
    client = _connect_live_graph()
    try:
        client.ensure_indexes()
        client.query("""
            UNWIND range(1, 10000) AS i
            CREATE (:Variable {qualified_name: 'noise:' + toString(i), role: 'local'})
        """)
        client.query("""
            UNWIND range(1, 100) AS i
            CREATE (s:Symbol {qualified_name: 'scope.' + toString(i)})
            CREATE (first:Variable {qualified_name: 'arg.' + toString(i) + ':first',
                role: 'parameter', parameter_index: 0, line_number: 7, name: 'z'})
            CREATE (second:Variable {qualified_name: 'arg.' + toString(i) + ':second',
                role: 'parameter', parameter_index: 1, line_number: 7, name: 'a'})
            CREATE (local:Variable {qualified_name: 'local.' + toString(i), role: 'local'})
            CREATE (s)-[:USES_VARIABLE]->(first)
            CREATE (s)-[:USES_VARIABLE]->(second)
            CREATE (s)-[:USES_VARIABLE]->(local)
        """)
        scopes = [f"scope.{i}" for i in range(1, 51)] + ["missing"]
        plan = client._graph.explain(
            S.BATCH_QUERY_SCOPE_PARAMETERS, {"scope_qnames": scopes}
        )
        assert "Node By Index Scan" in str(plan)
        assert "Node By Label Scan" not in str(plan)
        rows = client.query(
            S.BATCH_QUERY_SCOPE_PARAMETERS, {"scope_qnames": scopes}, timeout=1000
        ).result_set
        expected = {
            scope: [f"arg.{scope.split('.')[1]}:first", f"arg.{scope.split('.')[1]}:second"]
            for scope in scopes if scope != "missing"
        }
        actual = {}
        for scope, qualified_name, _, _ in rows:
            actual.setdefault(scope, []).append(qualified_name)
        assert actual == expected
        assert IndexPipeline(client)._get_scope_parameters_batch(scopes) == expected
        assert IndexPipeline(client)._get_scope_parameters_batch([]) == {}
    finally:
        client.delete()
        client.close()


def test_live_graph_captures_cross_function_variable_propagation(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    file_path = _write_sample_repo(repo_root)
    client = _connect_live_graph()

    try:
        pipeline = IndexPipeline(client)
        stats = pipeline.index_full(str(repo_root))
        module_qname = path_to_module(str(file_path))
        render_scope = f"{module_qname}.render"
        cleaned_variable = f"{render_scope}:cleaned"
        normalize_return = f"{module_qname}.normalize:__return__"
        normalize_param = f"{module_qname}.normalize:raw"

        assert stats["files"] == 1
        assert stats["variable_flows"] >= 4

        lineage = client.query(S.QUERY_VARIABLE_LINEAGE, {"qualified_name": cleaned_variable}).result_set
        assert lineage
        upstream = lineage[0][0] or []
        assert normalize_return in upstream

        flows = client.query(S.QUERY_VARIABLE_FLOWS_FOR_SCOPE, {"scope_qname": render_scope, "limit": 20}).result_set
        assert any(
            row[0] == f"{render_scope}:input_text" and row[1] == normalize_param and row[2] == "argument"
            for row in flows
        )
        assert any(
            row[0] == normalize_return and row[1] == cleaned_variable and row[2] == "call_return"
            for row in flows
        )

        influences = client.query(S.QUERY_RETURN_INFLUENCE, {"scope_qname": render_scope, "limit": 10}).result_set
        assert influences
        assert any(row[0] == f"{render_scope}:input_text" for row in influences)
    finally:
        try:
            client.query("MATCH (n) DETACH DELETE n")
        except Exception:
            pass
        client.close()