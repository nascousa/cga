"""Unit tests for MCP tool handlers using mocked graph and producer."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend import runtime_config
from backend.auth.context import _current_project_external_id
import backend.tools.server as mcp_srv
from backend.workbriefing.service import WorkBriefingService
from backend.workbriefing.store import PgVectorActivityStore


@pytest.fixture(autouse=True)
def reset_server_state():
    """Reset module-level singletons between tests."""
    mcp_srv._registry = None
    mcp_srv._graph = mcp_srv._GraphProxy()
    mcp_srv._producer = None
    mcp_srv._cache = None
    mcp_srv._recorder = None
    mcp_srv._work_briefing_service = None
    yield
    mcp_srv._registry = None
    mcp_srv._graph = mcp_srv._GraphProxy()
    mcp_srv._producer = None
    mcp_srv._cache = None
    mcp_srv._recorder = None
    mcp_srv._work_briefing_service = None


def _mock_graph(rows: list[list]) -> MagicMock:
    graph = MagicMock()
    result = MagicMock()
    result.result_set = rows
    graph.query.return_value = result
    return graph


def _mock_producer(stream_id: str = "1234-0") -> AsyncMock:
    producer = AsyncMock()
    producer.submit_full_index.return_value = {"job_id": "job-1", "stream_id": stream_id}
    producer.submit_incremental_index.return_value = {"job_id": "job-2", "stream_id": stream_id}
    return producer


class _FakeGitStatusProcess:
    def __init__(self, stdout: str, stderr: str = "", returncode: int = 0) -> None:
        self.returncode = returncode
        self._stdout = stdout.encode()
        self._stderr = stderr.encode()

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr


def test_init_sets_singletons():
    registry = MagicMock()
    producer = MagicMock()
    mcp_srv.init(registry=registry, producer=producer)
    assert mcp_srv._registry is registry
    assert mcp_srv._producer is producer


def test_find_symbol_returns_results():
    mcp_srv._graph = _mock_graph(
        [["backend.indexer.parser.PythonParser", "class", "src/backend/indexer/parser.py", 30, 80]]
    )
    results = mcp_srv.find_symbol(name="PythonParser", limit=5)
    assert len(results) == 1
    assert results[0]["qualified_name"] == "backend.indexer.parser.PythonParser"
    assert results[0]["symbol_type"] == "class"


def test_find_symbol_not_initialized():
    with pytest.raises(RuntimeError, match="not initialized"):
        mcp_srv.find_symbol(name="anything")


def test_find_callers():
    mcp_srv._graph = _mock_graph(
        [["backend.main.lifespan", "src/backend/main.py", 42]]
    )
    results = mcp_srv.find_callers(qualified_name="backend.graph.client.GraphClient.connect")
    assert results[0]["caller"] == "backend.main.lifespan"


def test_find_callees():
    mcp_srv._graph = _mock_graph(
        [["backend.graph.client.GraphClient.query", "src/backend/graph/client.py", 44]]
    )
    results = mcp_srv.find_callees(qualified_name="backend.indexer.pipeline.IndexPipeline._index_file")
    assert "callee" in results[0]


def test_find_variable_returns_results():
    mcp_srv._graph = _mock_graph(
        [["backend.service.render:label", "backend.service.render", "src/backend/service.py", 12, "local"]]
    )
    results = mcp_srv.find_variable(name="label", limit=5)
    assert len(results) == 1
    assert results[0]["qualified_name"] == "backend.service.render:label"
    assert results[0]["role"] == "local"


def test_get_variable_flows():
    mcp_srv._graph = _mock_graph(
        [["backend.service.render:input", "backend.service.render:label", "assignment", 12]]
    )
    results = mcp_srv.get_variable_flows("backend.service.render", limit=20)
    assert results[0]["source"] == "backend.service.render:input"
    assert results[0]["target"] == "backend.service.render:label"


def test_trace_variable_lineage():
    mcp_srv._graph = _mock_graph(
        [[["backend.service.render:input"] , ["backend.service.render:result"]]]
    )
    result = mcp_srv.trace_variable_lineage("backend.service.render:label")
    assert result["qualified_name"] == "backend.service.render:label"
    assert result["upstream"] == ["backend.service.render:input"]
    assert result["downstream"] == ["backend.service.render:result"]


def test_analyze_return_influence():
    mcp_srv._graph = _mock_graph(
        [
            [
                "backend.service.render:input",
                [
                    "backend.service.render:input",
                    "backend.service.render:label",
                    "backend.service.render:result",
                    "backend.service.render:__return__",
                ],
            ],
            [
                "backend.service.render:suffix",
                [
                    "backend.service.render:suffix",
                    "backend.service.render:result",
                    "backend.service.render:__return__",
                ],
            ],
        ]
    )
    result = mcp_srv.analyze_return_influence("backend.service.render", limit=10)
    assert result["scope_qname"] == "backend.service.render"
    assert result["influenced_by_parameters"] == [
        "backend.service.render:input",
        "backend.service.render:suffix",
    ]
    assert result["paths"][0]["path_length"] >= 1


def test_analyze_scope_variables():
    mcp_srv._graph = _mock_graph(
        [
            ["backend.service.render:input", "input", "parameter", 0, 1],
            ["backend.service.render:suffix", "suffix", "parameter", 0, 0],
            ["backend.service.render:label", "label", "local", 1, 1],
            ["backend.service.render:result", "result", "local", 2, 1],
            ["backend.service.render:__return__", "__return__", "return", 2, 0],
        ]
    )
    result = mcp_srv.analyze_scope_variables("backend.service.render", limit=10)
    assert result["unused_parameters"] == ["backend.service.render:suffix"]
    assert result["key_intermediates"][0]["qualified_name"] == "backend.service.render:result"


def test_explain_data_flow():
    graph = MagicMock()
    flow_result = MagicMock()
    flow_result.result_set = [
        ["backend.service.render:input", "backend.service.render:label", "assignment", 10],
        ["backend.service.render:label", "backend.service.render:result", "assignment", 11],
        ["backend.service.render:result", "backend.service.render:__return__", "return", 12],
    ]
    metrics_result = MagicMock()
    metrics_result.result_set = [
        ["backend.service.render:input", "input", "parameter", 0, 1],
        ["backend.service.render:suffix", "suffix", "parameter", 0, 0],
        ["backend.service.render:label", "label", "local", 1, 1],
        ["backend.service.render:result", "result", "local", 1, 1],
        ["backend.service.render:__return__", "__return__", "return", 1, 0],
    ]
    return_result = MagicMock()
    return_result.result_set = [
        ["backend.service.render:input", ["backend.service.render:input", "backend.service.render:label", "backend.service.render:result", "backend.service.render:__return__"]]
    ]
    graph.query.side_effect = [flow_result, metrics_result, return_result]
    mcp_srv._graph = graph
    result = mcp_srv.explain_data_flow("backend.service.render", limit=10)
    assert result["scope_qname"] == "backend.service.render"
    assert "backend.service.render:suffix" in result["unused_parameters"]
    assert "backend.service.render:label" in result["key_intermediates"]
    assert any("Return value is influenced by these inputs" in line for line in result["summary"])
    assert "render inputs include" in result["narrative"]
    assert result["narrative"].isascii()


def test_retrieve_context():
    mcp_srv._graph = _mock_graph(
        [["backend.indexer.parser.PythonParser.parse", "method", "src/backend/indexer/parser.py", 40, 70]]
    )
    with patch("backend.tools.server._read_symbol_snippet", return_value="def parse(...): ..."), patch(
        "backend.tools.server._fetch_relation_summary",
        return_value={
            "callers": ["caller.one"],
            "callees": ["callee.one"],
            "callers_count": 1,
            "callees_count": 1,
        },
    ):
        results = mcp_srv.retrieve_context(query="parse", limit=5)
    assert results[0]["file_path"] == "src/backend/indexer/parser.py"
    assert results[0]["snippet"] == "def parse(...): ..."
    assert "summary" in results[0]
    assert results[0]["callers"] == ["caller.one"]
    assert results[0]["callees"] == ["callee.one"]


def test_retrieve_context_records_task_correlation_in_trace_args():
    mcp_srv._graph = _mock_graph(
        [["backend.indexer.parser.PythonParser.parse", "method", "src/backend/indexer/parser.py", 40, 70]]
    )
    recorder = MagicMock()
    mcp_srv._recorder = recorder
    with patch("backend.tools.server._read_symbol_snippet", return_value="def parse(...): ..."), patch(
        "backend.tools.server._fetch_relation_summary",
        return_value={
            "callers": [],
            "callees": [],
            "callers_count": 0,
            "callees_count": 0,
        },
    ):
        results = mcp_srv.retrieve_context(query="parse", limit=5, task_id="TASK-123", pr_id="42")

    assert results[0]["qualified_name"] == "backend.indexer.parser.PythonParser.parse"
    recorder.record.assert_called_once()
    _tool, args, _results, _latency = recorder.record.call_args.args
    assert args["query"] == "parse"
    assert args["task_id"] == "TASK-123"
    assert args["pr_id"] == "42"


@pytest.mark.asyncio
async def test_index_full_queues_job():
    mcp_srv._producer = _mock_producer("5000-0")
    result = await mcp_srv.index_full(repo_path="/repo/myproject")
    assert result["status"] == "queued"
    assert result["stream_id"] == "5000-0"
    assert result["job_id"] == "job-1"
    mcp_srv._producer.submit_full_index.assert_awaited_once_with(
        "/repo/myproject",
        project_name="contextgraph",
    )


@pytest.mark.asyncio
async def test_index_full_uses_explicit_project_name_override():
    mcp_srv._producer = _mock_producer("5000-1")
    result = await mcp_srv.index_full(repo_path="/repo/osagent", project_name="osagent")
    assert result["job_id"] == "job-1"
    mcp_srv._producer.submit_full_index.assert_awaited_once_with(
        "/repo/osagent",
        project_name="osagent",
    )


@pytest.mark.asyncio
async def test_index_incremental_queues_job():
    mcp_srv._producer = _mock_producer("5001-0")
    result = await mcp_srv.index_incremental(
        repo_path="/repo", changed_paths=["a.py", "b.py"]
    )
    assert result["changed_count"] == 2
    assert result["job_id"] == "job-2"
    mcp_srv._producer.submit_incremental_index.assert_awaited_once()


@pytest.mark.asyncio
async def test_index_incremental_uses_explicit_project_name_override():
    mcp_srv._producer = _mock_producer("5001-1")
    result = await mcp_srv.index_incremental(
        repo_path="/repo",
        changed_paths=["a.py"],
        project_name="osagent",
    )
    assert result["changed_count"] == 1
    mcp_srv._producer.submit_incremental_index.assert_awaited_once_with(
        "/repo",
        ["a.py"],
        project_name="osagent",
    )


@pytest.mark.asyncio
async def test_workassist_record_activity_uses_authenticated_project_context(pg_activity_store):
    mcp_srv._work_briefing_service = WorkBriefingService(store=pg_activity_store)
    token = _current_project_external_id.set("CGA123")
    try:
        result = await mcp_srv.workassist_record_activity(
            event_type="sync",
            title="Synced work item",
            summary="imported progress",
        )
    finally:
        _current_project_external_id.reset(token)

    assert result["operation"] == "created"
    assert result["activity"]["project_id"] == "CGA123"
    assert result["activity"]["summary"] == "imported progress"


def test_workassist_tools_are_registered_in_existing_cga_mcp_server():
    tools_by_name = {tool.name: tool for tool in mcp_srv.mcp._tool_manager.list_tools()}

    assert "workassist_record_activity" in tools_by_name
    assert "workassist_list_recent_activity" in tools_by_name
    assert "workassist_get_activity_briefing" in tools_by_name

    record_tool = tools_by_name["workassist_record_activity"]
    assert "project work event" in record_tool.description.lower()
    assert set(record_tool.parameters["properties"].keys()) >= {
        "project_id",
        "event_type",
        "title",
        "summary",
        "metadata",
    }

    briefing_tool = tools_by_name["workassist_get_activity_briefing"]
    assert "project_id" in briefing_tool.parameters["properties"]
    assert briefing_tool.parameters["properties"]["limit"]["default"] == 25


@pytest.mark.asyncio
async def test_workassist_record_activity_rejects_project_spoof(pg_activity_store):
    mcp_srv._work_briefing_service = WorkBriefingService(store=pg_activity_store)
    token = _current_project_external_id.set("CGA123")
    try:
        with pytest.raises(Exception, match="project_id must match"):
            await mcp_srv.workassist_record_activity(
                event_type="sync",
                title="Synced work item",
                project_id="WA999",
            )
    finally:
        _current_project_external_id.reset(token)


async def _seed_workassist_activity_service(pg_activity_store):
    service = WorkBriefingService(store=pg_activity_store)
    await service.record_activity(
        {
            "project_id": "CGA123",
            "workspace_name": "Context Graph Agent",
            "event_type": "sync",
            "external_id": "sync-1",
            "title": "Synced work item",
            "summary": "imported project progress",
            "status": "in_progress",
        }
    )
    await service.record_activity(
        {
            "project_id": "CGA123",
            "workspace_name": "Context Graph Agent",
            "event_type": "review",
            "external_id": "review-1",
            "title": "Reviewed change",
            "summary": "validated merged tool behavior",
            "status": "done",
        }
    )
    await service.record_activity(
        {
            "project_id": "WA999",
            "workspace_name": "WorkAssist",
            "event_type": "sync",
            "external_id": "sync-2",
            "title": "Synced external work item",
            "summary": "should stay outside authenticated scope",
            "status": "pending",
        }
    )
    return service


@pytest.mark.asyncio
async def test_workassist_list_recent_activity_uses_authenticated_project_context(pg_activity_store):
    mcp_srv._work_briefing_service = await _seed_workassist_activity_service(pg_activity_store)
    token = _current_project_external_id.set("CGA123")
    try:
        result = await mcp_srv.workassist_list_recent_activity(limit=10)
    finally:
        _current_project_external_id.reset(token)

    assert result["project_id"] == "CGA123"
    assert result["count"] == 2
    assert [activity["project_id"] for activity in result["activities"]] == ["CGA123", "CGA123"]
    assert {activity["event_type"] for activity in result["activities"]} == {"sync", "review"}


@pytest.mark.asyncio
async def test_workassist_list_recent_activity_rejects_project_spoof(pg_activity_store):
    mcp_srv._work_briefing_service = await _seed_workassist_activity_service(pg_activity_store)
    token = _current_project_external_id.set("CGA123")
    try:
        with pytest.raises(Exception, match="project_id must match"):
            await mcp_srv.workassist_list_recent_activity(project_id="WA999", limit=10)
    finally:
        _current_project_external_id.reset(token)


@pytest.mark.asyncio
async def test_workassist_get_activity_briefing_uses_authenticated_project_context(pg_activity_store):
    mcp_srv._work_briefing_service = await _seed_workassist_activity_service(pg_activity_store)
    token = _current_project_external_id.set("CGA123")
    try:
        result = await mcp_srv.workassist_get_activity_briefing(limit=10)
    finally:
        _current_project_external_id.reset(token)

    assert result["project_id"] == "CGA123"
    assert result["total_events"] == 2
    assert result["project_counts"] == {"CGA123": 2}
    assert result["event_type_counts"] == {"review": 1, "sync": 1}
    assert result["status_counts"] == {"done": 1, "in_progress": 1}


@pytest.mark.asyncio
async def test_index_repo_changes_uses_explicit_project_name_override():
    mcp_srv._producer = _mock_producer("5011-0")
    with patch(
        "backend.tools.server._collect_git_changed_paths",
        return_value={"changed_paths": ["src/a.py"], "destructive_paths": []},
    ):
        result = await mcp_srv.index_repo_changes(repo_path="/repo", project_name="osagent")
    assert result["mode"] == "incremental"
    mcp_srv._producer.submit_incremental_index.assert_awaited_once_with(
        "/repo",
        ["src/a.py"],
        project_name="osagent",
    )


@pytest.mark.asyncio
async def test_collect_git_changed_paths_parses_modified_and_untracked():
    proc = _FakeGitStatusProcess(stdout=" M src/a.py\n?? docs/b.md\n")
    with patch("backend.tools.server.asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        result = await mcp_srv._collect_git_changed_paths("/repo", include_untracked=True)
    assert result["changed_paths"] == ["src/a.py", "docs/b.md"]
    assert result["destructive_paths"] == []


@pytest.mark.asyncio
async def test_collect_git_changed_paths_marks_delete_and_rename_destructive():
    proc = _FakeGitStatusProcess(stdout=" D src/old.py\nR  src/old2.py -> src/new2.py\n")
    with patch("backend.tools.server.asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        result = await mcp_srv._collect_git_changed_paths("/repo", include_untracked=True)
    assert "src/old.py" in result["destructive_paths"]
    assert "src/new2.py" in result["changed_paths"]
    assert "src/old2.py" in result["destructive_paths"]


@pytest.mark.asyncio
async def test_index_repo_changes_returns_noop_when_git_clean():
    mcp_srv._producer = _mock_producer("5010-0")
    with patch(
        "backend.tools.server._collect_git_changed_paths",
        return_value={"changed_paths": [], "destructive_paths": []},
    ):
        result = await mcp_srv.index_repo_changes(repo_path="/repo")
    assert result["status"] == "noop"
    assert result["mode"] == "none"
    mcp_srv._producer.submit_incremental_index.assert_not_called()
    mcp_srv._producer.submit_full_index.assert_not_called()


@pytest.mark.asyncio
async def test_index_repo_changes_queues_incremental_for_safe_git_changes():
    mcp_srv._producer = _mock_producer("5011-0")
    with patch(
        "backend.tools.server._collect_git_changed_paths",
        return_value={"changed_paths": ["src/a.py", "docs/b.md"], "destructive_paths": []},
    ):
        result = await mcp_srv.index_repo_changes(repo_path="/repo")
    assert result["status"] == "queued"
    assert result["mode"] == "incremental"
    assert result["changed_count"] == 2
    mcp_srv._producer.submit_incremental_index.assert_awaited_once()
    mcp_srv._producer.submit_full_index.assert_not_called()


@pytest.mark.asyncio
async def test_index_repo_changes_uses_incremental_for_destructive_git_changes_by_default():
    mcp_srv._producer = _mock_producer("5012-0")
    with patch(
        "backend.tools.server._collect_git_changed_paths",
        return_value={"changed_paths": ["src/new.py"], "destructive_paths": ["src/old.py"]},
    ):
        result = await mcp_srv.index_repo_changes(repo_path="/repo")
    assert result["status"] == "queued"
    assert result["mode"] == "incremental"
    assert result["changed_count"] == 2
    mcp_srv._producer.submit_incremental_index.assert_awaited_once_with(
        "/repo",
        ["src/old.py", "src/new.py"],
        project_name="contextgraph",
    )
    mcp_srv._producer.submit_full_index.assert_not_called()


@pytest.mark.asyncio
async def test_index_repo_changes_can_still_promote_to_full_on_destructive_git_changes():
    mcp_srv._producer = _mock_producer("5013-0")
    with patch(
        "backend.tools.server._collect_git_changed_paths",
        return_value={"changed_paths": ["src/new.py"], "destructive_paths": ["src/old.py"]},
    ):
        result = await mcp_srv.index_repo_changes(repo_path="/repo", auto_full_on_destructive=True)
    assert result["status"] == "queued"
    assert result["mode"] == "full"
    assert result["reason"] == "destructive_git_change"
    mcp_srv._producer.submit_full_index.assert_awaited_once()
    mcp_srv._producer.submit_incremental_index.assert_not_called()


@pytest.mark.asyncio
async def test_index_repo_changes_falls_back_to_full_when_git_binary_missing():
    mcp_srv._producer = _mock_producer("5014-0")
    with patch(
        "backend.tools.server._collect_git_changed_paths",
        side_effect=FileNotFoundError("git"),
    ):
        result = await mcp_srv.index_repo_changes(repo_path="/repo")
    assert result["status"] == "queued"
    assert result["mode"] == "full"
    assert result["reason"] == "git_unavailable"
    mcp_srv._producer.submit_full_index.assert_awaited_once_with(
        "/repo",
        project_name="contextgraph",
    )
    mcp_srv._producer.submit_incremental_index.assert_not_called()


@pytest.mark.asyncio
async def test_index_repo_changes_falls_back_to_full_when_git_status_fails():
    mcp_srv._producer = _mock_producer("5015-0")
    with patch(
        "backend.tools.server._collect_git_changed_paths",
        side_effect=RuntimeError("git status failed"),
    ):
        result = await mcp_srv.index_repo_changes(repo_path="/repo")
    assert result["status"] == "queued"
    assert result["mode"] == "full"
    assert result["reason"] == "git_status_failed"
    mcp_srv._producer.submit_full_index.assert_awaited_once_with(
        "/repo",
        project_name="contextgraph",
    )
    mcp_srv._producer.submit_incremental_index.assert_not_called()


@pytest.mark.asyncio
async def test_get_index_job_status():
    producer = _mock_producer("5002-0")
    producer.get_job_status.return_value = {"job_id": "job-2", "status": "processing"}
    mcp_srv._producer = producer

    result = await mcp_srv.get_index_job_status("job-2")
    assert result["status"] == "processing"
    producer.get_job_status.assert_awaited_once_with("job-2")


@pytest.mark.asyncio
async def test_wait_for_index_ready():
    producer = _mock_producer("5003-0")
    producer.wait_for_job_status.return_value = {
        "job_id": "job-3",
        "status": "done",
        "ready": True,
        "timeout": False,
    }
    mcp_srv._producer = producer

    result = await mcp_srv.wait_for_index_ready("job-3", timeout_sec=5.0, poll_interval_sec=0.2)
    assert result["ready"] is True
    producer.wait_for_job_status.assert_awaited_once_with(
        "job-3",
        timeout_sec=5.0,
        poll_interval_sec=0.2,
    )


@pytest.mark.asyncio
async def test_index_full_not_initialized():
    with pytest.raises(RuntimeError, match="not initialized"):
        await mcp_srv.index_full(repo_path="/repo")


# ---------------------------------------------------------------------------
# Phase 2 tools
# ---------------------------------------------------------------------------

def test_find_call_graph():
    graph = MagicMock()
    callers_result = MagicMock()
    callers_result.result_set = [["backend.main.lifespan"]]
    callees_result = MagicMock()
    callees_result.result_set = [["backend.graph.client.GraphClient.connect"]]
    graph.query.side_effect = [callers_result, callees_result]
    mcp_srv._graph = graph
    result = mcp_srv.find_call_graph(qualified_name="backend.indexer.pipeline.IndexPipeline._upsert_repo")
    assert "callers" in result
    assert "callees" in result


def test_get_stats():
    graph = MagicMock()
    def side_effect(cypher, *args, **kwargs):
        r = MagicMock()
        if "Symbol" in cypher:
            r.result_set = [[42]]
        elif "File" in cypher:
            r.result_set = [[10]]
        elif "Variable" in cypher:
            r.result_set = [[18]]
        elif "CALLS" in cypher:
            r.result_set = [[15]]
        elif "FLOWS_TO" in cypher:
            r.result_set = [[9]]
        else:
            r.result_set = [[0]]
        return r
    graph.query.side_effect = side_effect
    mcp_srv._graph = graph
    stats = mcp_srv.get_stats()
    assert stats["symbols"] == 42
    assert stats["files"] == 10
    assert stats["variables"] == 18
    assert stats["call_edges"] == 15
    assert stats["variable_flow_edges"] == 9


# ---------------------------------------------------------------------------
# Phase 3 tools
# ---------------------------------------------------------------------------

def test_clear_cache_no_cache():
    result = mcp_srv.clear_cache()
    assert result["status"] == "no_cache_configured"


def test_clear_cache_with_cache():
    mock_cache = MagicMock()
    mock_cache.invalidate_all.return_value = 5
    mcp_srv._cache = mock_cache
    result = mcp_srv.clear_cache()
    assert result["deleted"] == 5
    assert result["status"] == "ok"


def test_strategy_query_uses_server_strategy(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_config, "RUNTIME_CONFIG_PATH", tmp_path / "runtime-config.json")
    runtime_config.update_runtime_config({"indexing": {"default_token_budget": 2400}})
    mcp_srv._graph = MagicMock()
    with patch("backend.tools.server.run_cg_first_strategy") as mocked:
        mocked.return_value = {
            "strategy": "cg-first",
            "source": "contextgraph-server",
            "used_fallback": False,
            "graph_context": [{"qualified_name": "pkg.mod.fn"}],
        }
        result = mcp_srv.strategy_query(query="index flow")

    assert result["strategy"] == "cg-first"
    mocked.assert_called_once()
    assert mocked.call_args.kwargs["token_budget"] == 2400


# ---------------------------------------------------------------------------
# Aggregation & architecture analysis tools
# ---------------------------------------------------------------------------

def test_get_architecture_overview():
    mcp_srv._graph = _mock_graph([[15, 250, 2, 8, 16.7, 1.2]])
    result = mcp_srv.get_architecture_overview()
    assert result["total_files"] == 15
    assert result["total_symbols"] == 250
    assert result["languages"] == 2
    assert result["files_with_incoming_calls"] == 8
    assert result["avg_symbols_per_file"] == 16.7
    assert result["avg_callers_per_file"] == 1.2


def test_get_key_modules():
    rows = [
        ["src/main.py", "python", 12, 15, 8, 5.6],
        ["src/util.py", "python", 8, 5, 3, 2.1],
    ]
    mcp_srv._graph = _mock_graph(rows)
    result = mcp_srv.get_key_modules(limit=10)
    assert len(result) == 2
    assert result[0]["file_path"] == "src/main.py"
    assert result[0]["importance_score"] == 5.6


def test_get_file_stats():
    mcp_srv._graph = _mock_graph([[12, 5, 8]])
    result = mcp_srv.get_file_stats("src/core.py")
    assert result["file_path"] == "src/core.py"
    assert result["symbol_count"] == 12
    assert result["incoming_calls"] == 5
    assert result["symbols_with_outgoing_calls"] == 8


def test_analyze_dependencies():
    rows = [
        ["src/main.py", "src/util.py", 3, 2],
        ["src/core.py", "src/main.py", 2, 4],
    ]
    mcp_srv._graph = _mock_graph(rows)
    result = mcp_srv.analyze_dependencies(limit=20)
    assert len(result) == 2
    assert result[0]["from_file"] == "src/main.py"
    assert result[0]["caller_symbols"] == 3


def test_find_dependency_chain():
    rows = [[2, 5], [3, 2]]
    mcp_srv._graph = _mock_graph(rows)
    result = mcp_srv.find_dependency_chain("src/a.py", "src/b.py")
    assert result["source_path"] == "src/a.py"
    assert result["target_path"] == "src/b.py"
    assert result["closest_distance"] == 2
    assert len(result["chains"]) == 2


def test_find_dependency_chain_no_path():
    mcp_srv._graph = _mock_graph([])
    result = mcp_srv.find_dependency_chain("src/x.py", "src/y.py")
    assert result["closest_distance"] is None
    assert result["chains"] == []


# ---------------------------------------------------------------------------
# Import tracking tools
# ---------------------------------------------------------------------------

def test_get_file_imports():
    rows = [["src/utils.py", "python"], ["src/helpers.py", "python"]]
    mcp_srv._graph = _mock_graph(rows)
    result = mcp_srv.get_file_imports("src/main.py")
    assert len(result) == 2
    assert result[0]["target_file"] == "src/utils.py"


def test_get_file_dependents():
    rows = [["src/handler.py", "python"], ["src/service.py", "python"]]
    mcp_srv._graph = _mock_graph(rows)
    result = mcp_srv.get_file_dependents("src/core.py")
    assert len(result) == 2
    assert result[0]["dependent_file"] == "src/handler.py"


def test_get_dependency_overview():
    rows = [
        ["src/main.py", "src/utils.py", "python", "python"],
        ["src/main.py", "src/helpers.py", "python", "python"],
        ["src/app.ts", "src/utils.ts", "typescript", "typescript"],
    ]
    mcp_srv._graph = _mock_graph(rows)
    result = mcp_srv.get_dependency_overview(limit=30)
    assert len(result) == 3
    assert result[0]["from_file"] == "src/main.py"


def test_analyze_import_surface():
    rows = [
        ["src/core.py", "python", 5, 8],
        ["src/utils.py", "python", 2, 3],
    ]
    mcp_srv._graph = _mock_graph(rows)
    result = mcp_srv.analyze_import_surface(limit=15)
    assert len(result) == 2
    assert result[0]["file_path"] == "src/core.py"
    assert result[0]["internal_imports"] == 5
    assert result[0]["incoming_imports"] == 8

