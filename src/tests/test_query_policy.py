from __future__ import annotations

from collections import deque
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import runtime_config
from backend.auth.context import ProjectScope, bind_project_ref, bind_project_scope
from backend.auth.dependencies import get_current_user, require_admin
from backend.graph import query_policy
from backend.graph.ontology import RELATIONS
from backend.main import app
from backend.runtime_config import QueryMode, QueryPolicy
from backend.tools import server


@pytest.fixture
def policy_server(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_config, "RUNTIME_CONFIG_PATH", tmp_path / "runtime-config.json")
    monkeypatch.setattr(query_policy, "_recent", deque(maxlen=100))
    graph = MagicMock()
    graph.query.return_value.result_set = []
    registry = MagicMock()
    registry.get.return_value = graph
    monkeypatch.setattr(server, "_registry", registry)
    monkeypatch.setattr(server, "_graph", server._GraphProxy())
    monkeypatch.setattr(server, "_cache", None)
    monkeypatch.setattr(server, "_recorder", None)
    with bind_project_scope(ProjectScope("POLICY", 1, "policy", str(tmp_path))):
        yield graph


def test_policy_defaults_and_partial_persistence_preserve_other_settings(policy_server):
    assert runtime_config.get_query_policy() == QueryPolicy()
    runtime_config.update_runtime_config({"indexing": {"default_token_budget": 4096}})
    runtime_config.update_runtime_config(
        {"query_policy": {"mode": "auto", "max_response_bytes": 4096}}
    )
    saved = runtime_config.update_runtime_config({"query_policy": {"evidence_detail": "full"}})
    assert saved["query_policy"] == {
        "mode": "auto", "max_response_bytes": 4096, "evidence_detail": "full",
    }
    assert saved["indexing"]["default_token_budget"] == 4096
    assert runtime_config.get_runtime_config()["query_policy"] == saved["query_policy"]
    assert json.loads(runtime_config.RUNTIME_CONFIG_PATH.read_text())["query_policy"] == saved["query_policy"]


@pytest.mark.parametrize("value", [
    None, [], "auto", {"mode": None}, {"mode": "unknown"}, {"mode": []},
    {"evidence_detail": "none"}, {"evidence_detail": {}},
    {"max_response_bytes": True}, {"max_response_bytes": 2048.0},
    {"max_response_bytes": 2047}, {"max_response_bytes": 262145},
    {"max_response_bytes": "4096"}, {"unexpected": True},
])
def test_invalid_policy_is_rejected_without_writing(policy_server, value):
    with pytest.raises(runtime_config.RuntimeConfigError):
        runtime_config.update_runtime_config({"query_policy": value})
    assert not runtime_config.RUNTIME_CONFIG_PATH.exists()


def test_corrupt_saved_policy_is_not_silently_defaulted(policy_server):
    runtime_config.RUNTIME_CONFIG_PATH.write_text('{"query_policy": {"mode": "typo"}}')
    with pytest.raises(runtime_config.RuntimeConfigError):
        server.query_graph("CALLS", "private.identity")
    policy_server.query.assert_not_called()


@pytest.mark.parametrize("mode", list(QueryMode))
@pytest.mark.parametrize("intent", ["lookup", "impact", "audit"])
@pytest.mark.parametrize("required", [False, True])
@pytest.mark.parametrize("relation", RELATIONS)
@pytest.mark.parametrize("direction", ["inbound", "outbound"])
def test_routing_matrix(mode, intent, required, relation, direction):
    selected, reason = query_policy.select_query_tool(
        QueryPolicy(mode=mode), relation.name, direction, intent, required
    )
    evidence = (
        required or intent == "audit" or mode == QueryMode.EVIDENCE_FIRST
        or (mode == QueryMode.AUTO and intent == "impact")
        or relation.name not in {"CALLS", "IMPORTS"}
    )
    if evidence:
        assert selected == "query_graph_relation"
    else:
        assert selected == {
            ("CALLS", "inbound"): "find_callers", ("CALLS", "outbound"): "find_callees",
            ("IMPORTS", "inbound"): "get_file_dependents", ("IMPORTS", "outbound"): "get_file_imports",
        }[(relation.name, direction)]
    assert reason


@pytest.mark.parametrize("relation,direction", [
    ("CALLS", "inbound"), ("CALLS", "outbound"),
    ("IMPORTS", "inbound"), ("IMPORTS", "outbound"),
])
def test_lightweight_queries_are_bounded_and_do_not_retry_empty_results(policy_server, relation, direction):
    response = server.query_graph(relation, "x", direction, limit=7)
    assert response["result"] == []
    assert response["decision"]["status"] == "no_matches"
    assert response["decision"]["cache_status"] == "disabled"
    policy_server.query.assert_called_once()
    cypher, params = policy_server.query.call_args.args
    assert "LIMIT $limit" in cypher
    assert params["limit"] == 8
    if relation == "IMPORTS":
        assert "ORDER BY" in cypher


def test_policy_changes_apply_next_call_and_audit_cannot_lose_provenance(policy_server):
    assert server.query_graph("CALLS", "x")["decision"]["selected_tool"] == "find_callees"
    runtime_config.update_runtime_config({"query_policy": {"mode": "auto"}})
    impact = server.query_graph("CALLS", "x", intent="impact")
    assert impact["decision"]["reason"] == "auto_impact_requires_evidence"
    assert "cypher" not in impact["result"]["provenance"]
    audit = server.query_graph("CALLS", "x", intent="audit")
    assert audit["decision"]["evidence_detail"] == "full"
    assert "cypher" in audit["result"]["provenance"]
    assert audit["result"]["provenance"]["parameters"]["identity"] == "x"
    runtime_config.update_runtime_config({"query_policy": {"mode": "evidence_first"}})
    assert server.query_graph("CALLS", "x")["decision"]["reason"] == "admin_evidence_first"


@pytest.mark.parametrize("overrides", [
    {"relation": "NO_SUCH_EDGE"}, {"identity": " "}, {"direction": "both"},
    {"limit": True}, {"limit": 101}, {"intent": "guess"},
    {"requires_evidence": "false"}, {"max_response_bytes": True},
    {"max_response_bytes": 2047}, {"max_response_bytes": 16385},
])
def test_invalid_request_never_queries_graph(policy_server, overrides):
    with pytest.raises(ValueError):
        server.query_graph(**{"relation": "CALLS", "identity": "x", **overrides})
    policy_server.query.assert_not_called()


def test_request_cannot_raise_saved_budget(policy_server):
    runtime_config.update_runtime_config({"query_policy": {"max_response_bytes": 4096}})
    with pytest.raises(ValueError, match="admin budget 4096"):
        server.query_graph("CALLS", "x", max_response_bytes=4097)
    response = server.query_graph("CALLS", "x", max_response_bytes=2048)
    assert response["decision"]["budget_bytes"] == 2048


def test_item_limit_and_byte_cap_are_explicit_and_do_not_mutate_cache(policy_server, monkeypatch):
    entries = {}
    cache = MagicMock()
    cache.get.side_effect = lambda tool, args: entries.get((tool, json.dumps(args, sort_keys=True)))
    cache.set.side_effect = lambda tool, args, value: entries.update(
        {(tool, json.dumps(args, sort_keys=True)): value}
    )
    policy_server.cache_generation.return_value = "generation-1"
    monkeypatch.setattr(server, "_cache", cache)
    row = ["mod.a", "a.py", 1, 3, "mod.b", "b.py", 5, 9, None, None, None]
    policy_server.query.return_value.result_set = [row] * 21
    first = server.query_graph("CALLS", "x", requires_evidence=True)
    assert first["decision"]["cache_status"] == "miss"
    assert first["decision"]["truncation_reason"] == "item_limit"
    saved_cache = deepcopy(entries)
    bounded = server.query_graph("CALLS", "x", requires_evidence=True, max_response_bytes=2048)
    assert bounded["decision"]["cache_status"] == "hit"
    assert bounded["decision"]["truncation_reason"] == "byte_budget"
    assert bounded["decision"]["budget_omitted_items"] > 0
    assert 0 < bounded["decision"]["returned_items"] < 20
    assert query_policy.encoded_response_bytes(bounded) == bounded["decision"]["response_bytes"] <= 2048
    assert entries == saved_cache
    assert len(server.query_graph_relation("CALLS", "x")["edges"]) == 20


@pytest.mark.parametrize("typed", [False, True])
def test_canonical_utf8_budget_exact_boundary_and_one_byte_under(typed):
    items = [{"name": "\u4e2d" * 50} for _ in range(3)]
    base = {
        "result": {"edges": items, "has_more": False, "status": "ok"} if typed else items,
        "decision": {"status": "ok", "truncated": False, "has_more": False},
    }
    original = query_policy.fit_query_response(deepcopy(base), 10000)
    exact = original["decision"]["response_bytes"]
    same = query_policy.fit_query_response(deepcopy(base), exact)
    assert same["decision"]["returned_items"] == 3
    assert same["decision"]["response_bytes"] == exact
    smaller = query_policy.fit_query_response(deepcopy(base), exact - 1)
    assert smaller["decision"]["returned_items"] == 2
    assert query_policy.encoded_response_bytes(smaller) <= exact - 1
    assert smaller["decision"]["budget_omitted_items"] == 1
    assert smaller["decision"]["truncated"] is True


def test_one_oversized_edge_is_not_reported_as_no_matches(policy_server):
    row = ["a" * 4000, "a.py", 1, 3, "b" * 4000, "b.py", 5, 9, None, None, None]
    policy_server.query.return_value.result_set = [row]
    response = server.query_graph("CALLS", "x", requires_evidence=True, max_response_bytes=2048)
    assert response["result"]["edges"] == []
    assert response["result"]["status"] == "budget_exhausted"
    assert response["decision"]["status"] == "budget_exhausted"
    assert response["decision"]["has_more"] is True
    assert response["decision"]["response_bytes"] <= 2048


def test_required_metadata_too_large_is_an_error_not_silent_downgrade(policy_server):
    with pytest.raises(ValueError, match="cannot fit required metadata"):
        server.query_graph("CALLS", "x" * 4000, intent="audit", max_response_bytes=2048)
    assert query_policy.recent_query_decisions()["records"] == []


def test_cache_policy_metadata_is_fresh_and_project_ref_generation_scoped(policy_server, monkeypatch, tmp_path):
    entries = {}
    cache = MagicMock()
    cache.get.side_effect = lambda tool, args: entries.get((tool, json.dumps(args, sort_keys=True)))
    cache.set.side_effect = lambda tool, args, value: entries.update(
        {(tool, json.dumps(args, sort_keys=True)): value}
    )
    monkeypatch.setattr(server, "_cache", cache)
    policy_server.cache_generation.return_value = "v1"
    one = server.query_graph("CALLS", "x", requires_evidence=True)
    runtime_config.update_runtime_config({"query_policy": {"mode": "evidence_first", "evidence_detail": "full"}})
    two = server.query_graph("CALLS", "x")
    assert one["decision"]["cache_status"] == "miss"
    assert two["decision"]["cache_status"] == "hit"
    assert two["decision"]["reason"] == "admin_evidence_first"
    assert "cypher" in two["result"]["provenance"]
    with bind_project_ref("feature/policy"):
        assert server.query_graph("CALLS", "x")["decision"]["cache_status"] == "miss"
    with bind_project_scope(ProjectScope("OTHER", 2, "other", str(tmp_path))):
        assert server.query_graph("CALLS", "x")["decision"]["cache_status"] == "miss"
    policy_server.cache_generation.return_value = "v2"
    assert server.query_graph("CALLS", "x")["decision"]["cache_status"] == "miss"
    assert policy_server.query.call_count == 4
    assert len(entries) == 4


def test_concurrent_observations_do_not_cross_calls(policy_server, monkeypatch, tmp_path):
    cache = MagicMock()
    cache.get.side_effect = lambda tool, args: [] if args["qualified_name"] == "cached" else None
    policy_server.cache_generation.return_value = "v1"
    monkeypatch.setattr(server, "_cache", cache)

    def run(identity):
        with bind_project_scope(ProjectScope("POLICY", 1, "policy", str(tmp_path))):
            return server.query_graph("CALLS", identity)["decision"]["cache_status"]

    names = ["cached", "uncached"] * 20
    with ThreadPoolExecutor(max_workers=8) as executor:
        statuses = list(executor.map(run, names))
    assert statuses == ["hit", "miss"] * 20
    assert server._read_observation.get() is None


def test_graph_failure_propagates_and_context_is_restored(policy_server):
    policy_server.query.side_effect = RuntimeError("graph unavailable")
    with pytest.raises(RuntimeError, match="graph unavailable"):
        server.query_graph("CALLS", "x")
    assert server._read_observation.get() is None
    assert query_policy.recent_query_decisions()["records"] == []


def test_unscoped_query_is_forbidden():
    with pytest.raises(HTTPException) as exc:
        server.query_graph("CALLS", "x")
    assert exc.value.status_code == 403


def test_recent_decisions_are_bounded_and_do_not_expose_identity(policy_server):
    for _ in range(105):
        server.query_graph("CALLS", "private.identity")
    recent = query_policy.recent_query_decisions()
    assert recent["scope"] == "current_process"
    assert len(recent["records"]) == 100
    assert "private.identity" not in json.dumps(recent)
    assert recent["records"][0]["project_id"] == "POLICY"


def test_admin_policy_endpoints_and_non_admin_denial(policy_server):
    client = TestClient(app)
    app.dependency_overrides[require_admin] = lambda: {"role": "admin"}
    try:
        saved = client.patch("/api/admin/runtime-config", json={"query_policy": {"mode": "auto"}})
        assert saved.status_code == 200
        assert saved.json()["query_policy"]["mode"] == "auto"
        assert client.get("/api/admin/query-routing/recent").status_code == 200
        assert client.patch("/api/admin/runtime-config", json={"query_policy": {"mode": "bad"}}).status_code == 400
    finally:
        app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user] = lambda: {"role": "developer"}
    try:
        assert client.get("/api/admin/query-routing/recent").status_code == 403
        assert client.patch("/api/admin/runtime-config", json={"query_policy": {"mode": "evidence_first"}}).status_code == 403
    finally:
        app.dependency_overrides.clear()
    assert runtime_config.get_query_policy().mode == QueryMode.AUTO


@pytest.mark.asyncio
async def test_mcp_discovery_and_routed_call(policy_server):
    tools = {tool.name: tool for tool in await server.mcp.list_tools()}
    schema = tools["query_graph"].inputSchema
    assert set(schema["properties"]["intent"]["enum"]) == {"lookup", "impact", "audit"}
    response = await server.mcp.call_tool("query_graph", {"relation": "CALLS", "identity": "x"})
    assert "admin_lightweight_first" in str(response)
    assert "query_graph" in server.mcp.instructions
