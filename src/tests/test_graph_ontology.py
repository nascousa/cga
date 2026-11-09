from __future__ import annotations

import json
import os
import uuid
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from backend.auth.context import ProjectScope, bind_project_ref, bind_project_scope
from backend.graph import schema
from backend.graph.client import GraphClient
from backend.graph.ontology import RELATIONS, compile_relation_query, graph_ontology
from backend.indexer.pipeline import IndexPipeline
from backend.tools import server


@pytest.fixture
def scoped_server(tmp_path, monkeypatch):
    graph = MagicMock()
    graph.query.return_value.result_set = []
    registry = MagicMock()
    registry.get.return_value = graph
    monkeypatch.setattr(server, "_registry", registry)
    monkeypatch.setattr(server, "_graph", server._GraphProxy())
    monkeypatch.setattr(server, "_cache", None)
    monkeypatch.setattr(server, "_recorder", None)
    with bind_project_scope(ProjectScope("ONTOLOGY", 1, "ontology", str(tmp_path))):
        yield graph, registry


def test_contract_matches_indexer_writes():
    writes = "\n".join(
        value for name, value in vars(schema).items()
        if ("MERGE" in name or "EDGE" in name) and isinstance(value, str)
    )
    import re

    written = set(re.findall(r"MERGE .*?\[:([A-Z_]+)", writes))
    assert {item.name for item in RELATIONS} == written
    contract = graph_ontology()
    assert {item["label"] for item in contract["entities"]} == {
        "Repository", "File", "Symbol", "Variable"
    }
    assert "REFERENCES" not in json.dumps(contract)


@pytest.mark.parametrize("relation", RELATIONS)
@pytest.mark.parametrize("direction", ["outbound", "inbound"])
def test_compiler_uses_contract_identifiers_and_parameter_values(relation, direction):
    identity = "'}) MATCH (secret) DETACH DELETE secret //"
    _, cypher, params = compile_relation_query(relation.name, identity, direction, 100)
    assert identity not in cypher
    assert params == {"identity": identity, "fetch_limit": 101}
    entity = relation.source if direction == "outbound" else relation.target
    anchor = "source" if direction == "outbound" else "target"
    assert f"WHERE {anchor}.{entity.identity_property} = $identity" in cypher


@pytest.mark.parametrize("overrides", [
    {"relation": "REFERENCES"}, {"relation": "CALLS]->() DELETE source //"},
    {"identity": ""}, {"identity": " "}, {"identity": "a" * 4097},
    {"direction": "both"}, {"limit": 0}, {"limit": 101},
    {"limit": True}, {"limit": 1.5},
])
def test_invalid_arguments_rejected_before_graph_query(scoped_server, overrides):
    graph, _ = scoped_server
    arguments = {"relation": "CALLS", "identity": "module.run", **overrides}
    with pytest.raises(ValueError):
        server.query_graph_relation(**arguments)
    graph.query.assert_not_called()


def test_discovery_and_empty_result(scoped_server):
    graph, _ = scoped_server
    assert server.get_graph_ontology() == graph_ontology()
    graph.query.assert_not_called()
    result = server.query_graph_relation("CALLS", "module.run")
    assert result["status"] == "no_matches"
    assert result["edges"] == []
    assert result["has_more"] is False
    assert result["provenance"]["source"] == "indexed_graph"
    assert result["limitations"]


def test_provenance_limit_and_branch_scope(scoped_server):
    graph, registry = scoped_server
    row = ["mod.a", "a.py", 1, 3, "mod.b", "b.py", 5, 9, None, None, None]
    graph.query.return_value.result_set = [row, row]
    with bind_project_ref("feature/ontology") as graph_name:
        result = server.query_graph_relation("CALLS", "mod.a", limit=1)
    assert result["has_more"] is True
    assert len(result["edges"]) == 1
    assert result["edges"][0]["target"]["line_start"] == 5
    assert result["provenance"]["graph_name"] == graph_name
    assert result["provenance"]["parameters"]["fetch_limit"] == 2
    registry.get.assert_called_with(graph_name)


def test_unscoped_access_denied():
    for operation in (server.get_graph_ontology, lambda: server.query_graph_relation("CALLS", "x")):
        with pytest.raises(HTTPException) as exc:
            operation()
        assert exc.value.status_code == 403


def test_graph_failure_is_not_an_empty_success(scoped_server):
    graph, _ = scoped_server
    graph.query.side_effect = RuntimeError("graph unavailable")
    with pytest.raises(RuntimeError, match="graph unavailable"):
        server.query_graph_relation("CALLS", "x")


def test_cache_is_generation_project_and_ref_scoped(scoped_server, monkeypatch, tmp_path):
    graph, _ = scoped_server
    entries = {}
    cache = MagicMock()
    cache.get.side_effect = lambda tool, args: entries.get((tool, json.dumps(args, sort_keys=True)))
    cache.set.side_effect = lambda tool, args, value: entries.update(
        {(tool, json.dumps(args, sort_keys=True)): value}
    )
    graph.cache_generation.return_value = "generation-1"
    monkeypatch.setattr(server, "_cache", cache)
    server.query_graph_relation("CALLS", "x")
    server.query_graph_relation("CALLS", "x")
    assert graph.query.call_count == 1
    graph.cache_generation.return_value = "generation-2"
    server.query_graph_relation("CALLS", "x")
    assert graph.query.call_count == 2
    with bind_project_scope(ProjectScope("OTHER", 2, "other", str(tmp_path))):
        server.query_graph_relation("CALLS", "x")
    assert graph.query.call_count == 3
    with bind_project_ref("feature/cache"):
        server.query_graph_relation("CALLS", "x")
    assert graph.query.call_count == 4
    assert len(entries) == 4


@pytest.mark.asyncio
async def test_mcp_discovery_and_call(scoped_server):
    tools = {tool.name: tool for tool in await server.mcp.list_tools()}
    assert set(tools["query_graph_relation"].inputSchema["properties"]["relation"]["enum"]) == {
        item.name for item in RELATIONS
    }
    assert "get_graph_ontology" in tools
    response = await server.mcp.call_tool(
        "query_graph_relation", {"relation": "CALLS", "identity": "module.run"}
    )
    assert "no_matches" in str(response)
    assert "get_graph_ontology" in server.mcp.instructions


@pytest.mark.live_graph
@pytest.mark.live_graph_e2e
def test_all_contract_relations_on_real_indexed_graph(tmp_path, monkeypatch):
    from backend import runtime_config

    monkeypatch.setattr(runtime_config, "RUNTIME_CONFIG_PATH", tmp_path / "policy.json")
    (tmp_path / "helper.py").write_text(
        "def clean(raw):\n    value = raw.strip()\n    return value\n", encoding="utf-8"
    )
    (tmp_path / "service.py").write_text(
        "from helper import clean\n\ndef render(raw):\n    result = clean(raw)\n    return result\n",
        encoding="utf-8",
    )
    graph = GraphClient(
        host=os.getenv("FALKORDB_HOST", "localhost"),
        port=int(os.getenv("FALKORDB_PORT", "16379")),
        graph_name=f"ontology_test_{uuid.uuid4().hex}",
    )
    graph.connect()
    registry = MagicMock()
    registry.get.return_value = graph
    monkeypatch.setattr(server, "_registry", registry)
    monkeypatch.setattr(server, "_graph", server._GraphProxy())
    monkeypatch.setattr(server, "_cache", None)
    monkeypatch.setattr(server, "_recorder", None)
    try:
        graph.ensure_indexes()
        IndexPipeline(graph).index_full(str(tmp_path))
        with bind_project_scope(ProjectScope("ONTOLOGY", 1, graph._graph_name, str(tmp_path))):
            for relation in RELATIONS:
                pair = graph.query(
                    f"MATCH (s:{relation.source.label})-[:{relation.name}]->"
                    f"(t:{relation.target.label}) RETURN "
                    f"s.{relation.source.identity_property}, t.{relation.target.identity_property} LIMIT 1"
                ).result_set
                assert pair, relation.name
                for direction, identity in zip(("outbound", "inbound"), pair[0]):
                    result = server.query_graph_relation(relation.name, identity, direction)
                    assert result["status"] == "ok", (relation.name, direction)
                    assert any(
                        edge["source"]["identity"] == pair[0][0]
                        and edge["target"]["identity"] == pair[0][1]
                        for edge in result["edges"]
                    )
                    assert result["provenance"]["graph_name"] == graph._graph_name
                    routed = server.query_graph(
                        relation.name, identity, direction, requires_evidence=True
                    )
                    assert routed["result"]["edges"] == result["edges"]
                    assert routed["decision"]["selected_tool"] == "query_graph_relation"
                    assert routed["decision"]["response_bytes"] <= 16384
                    if relation.name in {"CALLS", "IMPORTS"}:
                        legacy = server.query_graph(relation.name, identity, direction)
                        key = (
                            ("callee" if direction == "outbound" else "caller") if relation.name == "CALLS"
                            else "target_file" if direction == "outbound"
                            else "dependent_file"
                        )
                        assert pair[0][1 if direction == "outbound" else 0] in {
                            row[key] for row in legacy["result"]
                        }
    finally:
        graph.delete()
        graph.close()
