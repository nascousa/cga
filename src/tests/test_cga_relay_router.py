from __future__ import annotations

from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.cga_relay import router as cga_relay_router
from backend.auth.context import ProjectScope, bind_project_scope, branch_graph_name
from backend.graph.registry import _current_project_name


@pytest.fixture(autouse=True)
def bound_project(tmp_path, monkeypatch):
    connection = SimpleNamespace(exists=lambda key: False)
    monkeypatch.setattr(cga_relay_router, "_graph_connection", lambda: connection)
    with bind_project_scope(ProjectScope("PROJECT123", 7, "demo", str(tmp_path))):
        yield connection


@pytest.mark.asyncio
async def test_dispatch_query_impact_graph_uses_strategy_query(monkeypatch):
    captured = {}

    def fake_strategy_query(**kwargs):
        captured.update(kwargs)
        return {"answer": "ok"}

    monkeypatch.setattr(cga_relay_router.mcp_server, "strategy_query", fake_strategy_query)

    result = await cga_relay_router.dispatch_tool(
        "query_impact_graph",
        {"query": "scanner", "token_budget": 900},
        project_name="demo",
    )

    assert result == {
        "ok": True,
        "tool": "query_impact_graph",
        "backend_tool": "strategy_query",
        "result": {"answer": "ok"},
    }
    assert captured["query"] == "scanner"
    assert captured["token_budget"] == 900


@pytest.mark.asyncio
async def test_dispatch_query_impact_graph_omits_token_budget_for_config_default(monkeypatch):
    captured = {}

    def fake_strategy_query(**kwargs):
        captured.update(kwargs)
        return {"answer": "ok"}

    monkeypatch.setattr(cga_relay_router.mcp_server, "strategy_query", fake_strategy_query)

    await cga_relay_router.dispatch_tool(
        "query_impact_graph",
        {"query": "scanner"},
        project_name="demo",
    )

    assert captured["query"] == "scanner"
    assert captured["token_budget"] is None


@pytest.mark.asyncio
async def test_dispatch_index_incremental_sets_project_name(monkeypatch, tmp_path):
    producer_result = {"status": "queued", "job_id": "job-1"}
    index_incremental = AsyncMock(return_value=producer_result)
    monkeypatch.setattr(cga_relay_router.mcp_server, "index_incremental", index_incremental)

    result = await cga_relay_router.dispatch_tool(
        "index_incremental",
        {"repo_path": str(tmp_path), "changed_paths": ["a.py"]},
        project_name="demo",
    )

    assert result["result"] == producer_result
    assert "graph_name" not in result
    assert "ref_id" not in result
    index_incremental.assert_awaited_once_with(
        repo_path=str(tmp_path),
        changed_paths=[str(tmp_path / "a.py")],
        project_name="demo",
    )


@pytest.mark.asyncio
async def test_dispatch_index_incremental_routes_branch_alias_to_ref_graph(monkeypatch, tmp_path):
    index_incremental = AsyncMock(return_value={"status": "queued", "job_id": "job-branch"})
    monkeypatch.setattr(cga_relay_router.mcp_server, "index_incremental", index_incremental)

    result = await cga_relay_router.dispatch_tool(
        "index_incremental",
        {
            "repo_path": str(tmp_path),
            "changed_paths": ["a.py"],
            "branch": "feature/client-menu-order",
            "base_branch": "main",
        },
        project_name="demo",
    )

    index_incremental.assert_awaited_once_with(
        repo_path=str(tmp_path),
        changed_paths=[str(tmp_path / "a.py")],
        project_name=branch_graph_name("PROJECT123", "feature/client-menu-order"),
    )
    assert result["ref_id"] == "feature/client-menu-order"
    assert result["parent_ref"] == "main"
    assert result["graph_name"] == branch_graph_name("PROJECT123", "feature/client-menu-order")
    assert result["parent_graph_name"] == "demo"


@pytest.mark.asyncio
async def test_dispatch_index_git_incremental_routes_git_branch_alias(monkeypatch, tmp_path):
    index_repo_changes = AsyncMock(return_value={"status": "noop"})
    monkeypatch.setattr(cga_relay_router.mcp_server, "index_repo_changes", index_repo_changes)

    result = await cga_relay_router.dispatch_tool(
        "index_git_incremental",
        {"repo_path": str(tmp_path), "git_branch": "bugfix/cache-key"},
        project_name="demo",
    )

    index_repo_changes.assert_awaited_once_with(
        repo_path=str(tmp_path),
        include_untracked=True,
        auto_full_on_destructive=False,
        project_name=branch_graph_name("PROJECT123", "bugfix/cache-key"),
    )
    assert result["graph_name"] == branch_graph_name("PROJECT123", "bugfix/cache-key")


def test_graph_name_for_project_preserves_default_and_normalizes_ref():
    assert cga_relay_router._graph_name_for_project("Demo") == "demo"
    assert cga_relay_router._graph_name_for_project("Demo", "main") == "demo"
    assert (
        cga_relay_router._graph_name_for_project("Demo", "Feature/Client Menu@Order")
        == branch_graph_name("PROJECT123", "Feature/Client Menu@Order")
    )


def test_ref_arguments_accept_git_branch_and_base_ref_aliases():
    assert cga_relay_router._ref_arguments(
        {"git_branch": "feature/api", "base_ref": "release"}
    ) == ("feature/api", "release")


def test_query_graph_scope_does_not_fallback_when_branch_has_files(monkeypatch):
    ref_graph = branch_graph_name("PROJECT123", "feature/ready")
    counts = {ref_graph: 2}
    requested = []

    def fake_graph_file_count(graph_name):
        requested.append(graph_name)
        return counts[graph_name]

    monkeypatch.setattr(cga_relay_router, "_graph_file_count", fake_graph_file_count)

    scope = cga_relay_router._query_graph_scope("demo", "feature/ready", "main")

    assert scope == (
        ref_graph,
        ref_graph,
        False,
    )
    assert requested == [ref_graph]


@pytest.mark.asyncio
async def test_branch_query_falls_back_to_main_only_when_branch_graph_is_empty(monkeypatch):
    graph_counts = {
        branch_graph_name("PROJECT123", "feature/empty"): 0,
        "demo": 3,
    }
    active_graph = {"name": ""}

    class _CountResult:
        def __init__(self, count):
            self.result_set = [[count]]

    class _FakeGraph:
        def query(self, _query, _params=None):
            graph_name = _current_project_name.get()
            return _CountResult(graph_counts[graph_name])

    class _FakeRegistry:
        def get(self, graph_name):
            return _FakeGraph()

    def fake_strategy_query(**_kwargs):
        active_graph["name"] = _current_project_name.get()
        return {"answer": active_graph["name"]}

    monkeypatch.setattr(cga_relay_router.mcp_server, "_registry", _FakeRegistry())
    monkeypatch.setattr(cga_relay_router.mcp_server, "strategy_query", fake_strategy_query)

    result = await cga_relay_router.dispatch_tool(
        "query_impact_graph",
        {"query": "scanner", "ref_id": "feature/empty", "fallback_ref": "main"},
        project_name="demo",
    )

    assert result["requested_graph_name"] == branch_graph_name("PROJECT123", "feature/empty")
    assert result["graph_name"] == "demo"
    assert result["fallback_graph_used"] is True
    assert result["result"] == {"answer": "demo"}


@pytest.mark.asyncio
async def test_promote_ref_full_rebuilds_target_then_clears_source(monkeypatch, tmp_path, bound_project):
    source_graph = branch_graph_name("PROJECT123", "feature/client-menu-order")
    deleted_graphs = []
    generations = {source_graph: "source-1", "demo": "before"}
    bound_project.exists = lambda key: key in {source_graph, "demo"}

    class _Result:
        def __init__(self, rows):
            self.result_set = rows

    class _FakeRegistry:
        def get(self, graph_name):
            return SimpleNamespace(
                query=lambda query, params=None: _Result([[2]]),
                cache_generation=lambda: generations[graph_name],
            )

        def delete(self, graph_name, *, expected_generation=None):
            assert expected_generation == "source-1"
            assert generations["demo"] == "published"
            deleted_graphs.append(graph_name)

    index_full = AsyncMock(return_value={"status": "queued", "job_id": "job-promote"})

    async def wait(**kwargs):
        assert not deleted_graphs
        generations["demo"] = "published"
        return {"status": "done", "project_name": "demo", "files": 2, "errors": 0}

    monkeypatch.setattr(cga_relay_router.mcp_server, "_registry", _FakeRegistry())
    monkeypatch.setattr(cga_relay_router.mcp_server, "index_full", index_full)
    monkeypatch.setattr(cga_relay_router.mcp_server, "wait_for_index_ready", wait)

    result = await cga_relay_router.dispatch_tool(
        "promote_ref",
        {
            "ref_id": "feature/client-menu-order",
            "parent_ref": "main",
            "repo_path": str(tmp_path),
            "delete_ref_graph": True,
        },
        project_name="demo",
    )

    index_full.assert_awaited_once_with(
        repo_path=str(tmp_path),
        project_name="demo",
    )
    assert deleted_graphs == [source_graph]
    assert result["result"]["rebuild_mode"] == "full"
    assert result["result"]["source_graph_name"] == source_graph
    assert result["result"]["target_graph_name"] == "demo"
    assert result["result"]["deleted_ref_graph"] is True


@pytest.mark.asyncio
async def test_promote_ref_rejects_default_ref():
    with pytest.raises(HTTPException) as exc:
        await cga_relay_router.dispatch_tool(
            "promote_ref",
            {"ref_id": "main", "repo_path": "C:/repo"},
            project_name="demo",
        )

    assert exc.value.status_code == 400


def test_require_project_match_rejects_mismatched_project_id():
    with pytest.raises(HTTPException) as exc:
        cga_relay_router._require_project_match("PROJECT123", "OTHER")

    assert exc.value.status_code == 403


def test_sync_summary_never_includes_snapshot_content():
    payload = cga_relay_router.CgaRelaySync(
        agent_id="dev-agent-01",
        project_id="PROJECT123",
        namespace="dev",
        project_tag="repo",
        root="C:/repo",
        counts={"changed": 1},
        snapshots=[{"path": "a.py", "content": "TEST_SECRET_VALUE_SHOULD_NEVER_LEAK"}],
        tombstones=["old.py"],
    )

    summary = cga_relay_router.sync_summary(payload)

    assert summary == {
        "agent_id": "dev-agent-01",
        "project_id": "PROJECT123",
        "namespace": "dev",
        "project_tag": "repo",
        "root": "C:/repo",
        "counts": {"changed": 1},
        "snapshot_count": 1,
        "tombstone_count": 1,
    }
    assert "TEST_SECRET_VALUE_SHOULD_NEVER_LEAK" not in repr(summary)


class _FakeCursor:
    def __init__(self, row):
        self.row = row

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def fetchone(self):
        return self.row


class _FakeDb:
    def __init__(self, row):
        self.row = row
        self.calls = []

    def execute(self, sql, params):
        self.calls.append((sql, params))
        return _FakeCursor(self.row)


@pytest.mark.asyncio
async def test_account_project_context_resolves_active_project_by_project_id(tmp_path):
    db = _FakeDb({"id": 7, "project_name": "Demo", "project_id": "PROJECT123", "repo_path": str(tmp_path)})

    context = await cga_relay_router._account_project_context(db, "PROJECT123", {"role": "admin"})

    assert context == {
        "project_id": "PROJECT123",
        "project_name": "Demo",
        "project_db_id": 7,
        "registered_project_scope": ProjectScope("PROJECT123", 7, "demo", str(tmp_path)),
    }
    assert db.calls[0][1] == ("PROJECT123",)


@pytest.mark.asyncio
async def test_account_project_context_rejects_unknown_project():
    db = _FakeDb(None)

    with pytest.raises(HTTPException) as exc:
        await cga_relay_router._account_project_context(db, "MISSING", {"role": "admin"})

    assert exc.value.status_code == 404
