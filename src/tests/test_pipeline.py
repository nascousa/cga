"""Pipeline-level tests for variable flow graph writes."""

from __future__ import annotations

from unittest.mock import MagicMock
from pathlib import Path

from backend import runtime_config
from backend.graph import schema as S
from backend.indexer.call_analyzer import RawCall
from backend.indexer.hasher import sha256_file
from backend.indexer.parser import ParsedFile, ParsedSymbol, ParsedVariable, ParsedVariableFlow
from backend.indexer.pipeline import IndexPipeline


def test_write_variable_flow_edges_writes_variable_nodes_and_local_flows() -> None:
    graph = MagicMock()
    pipeline = IndexPipeline(graph)
    parsed = ParsedFile(path="repo/service.py", language="python")
    parsed.variables.extend(
        [
            ParsedVariable("input", "pkg.render:input", "pkg.render", "repo/service.py", 10, "parameter"),
            ParsedVariable("label", "pkg.render:label", "pkg.render", "repo/service.py", 11, "local"),
        ]
    )
    parsed.variable_flows.append(
        ParsedVariableFlow("pkg.render:input", "pkg.render:label", "pkg.render", 11, "assignment")
    )

    stats = pipeline._write_variable_flow_edges(parsed)

    assert stats["variables"] == 2
    assert stats["variable_flows"] == 1
    calls = graph.query.call_args_list
    # Batch write: BATCH_EDGE_SYMBOL_HAS_VARIABLE called with rows list
    assert any(call.args[0] == S.BATCH_EDGE_SYMBOL_HAS_VARIABLE for call in calls)
    # Batch write: BATCH_EDGE_VARIABLE_FLOWS called with rows containing assignment flow
    assert any(
        call.args[0] == S.BATCH_EDGE_VARIABLE_FLOWS
        and any(row.get("flow_type") == "assignment" for row in call.args[1].get("rows", []))
        for call in calls
        if len(call.args) > 1
    )


def test_write_cross_scope_variable_flows_writes_argument_and_return_edges() -> None:
    graph = MagicMock()

    def side_effect(cypher: str, params: dict | None = None):
        result = MagicMock()
        if "role: 'parameter'" in cypher:
            # BATCH_QUERY_SCOPE_PARAMETERS returns [scope_qname, variable_qname, line_number, name]
            result.result_set = [["pkg.callee", "pkg.callee:param", 1, "param"]]
        else:
            result.result_set = []
        return result

    graph.query.side_effect = side_effect
    pipeline = IndexPipeline(graph)
    raw_calls = [
        RawCall(
            caller_qname="pkg.caller",
            callee_name="callee",
            arg_names=["input"],
            result_var_name="result",
        )
    ]

    written = pipeline._write_cross_scope_variable_flows(raw_calls, {"callee": "pkg.callee"})

    assert written == 2
    # Batch write: BATCH_EDGE_VARIABLE_FLOWS called with rows containing both flows
    batch_flow_calls = [
        call for call in graph.query.call_args_list
        if len(call.args) > 1
        and call.args[0] == S.BATCH_EDGE_VARIABLE_FLOWS
    ]
    assert batch_flow_calls, "Expected BATCH_EDGE_VARIABLE_FLOWS call"
    all_rows = [row for call in batch_flow_calls for row in call.args[1].get("rows", [])]
    assert any(r["source_qname"] == "pkg.caller:input" and r["target_qname"] == "pkg.callee:param" for r in all_rows)
    assert any(r["source_qname"] == "pkg.callee:__return__" and r["target_qname"] == "pkg.caller:result" for r in all_rows)


def test_index_full_clears_repo_subgraph_before_rebuild(tmp_path: Path) -> None:
    graph = MagicMock()

    def side_effect(cypher: str, params: dict | None = None):
        result = MagicMock()
        if cypher == S.QUERY_REPO_EXISTS:
            result.result_set = [[1]]
        elif cypher == S.QUERY_REPO_FILE_PATHS:
            result.result_set = [[str(repo_root / "service.py")]]
        else:
            result.result_set = []
        return result

    graph.query.side_effect = side_effect
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "service.py").write_text("def render(x):\n    return x\n", encoding="utf-8")
    pipeline = IndexPipeline(graph)
    pipeline._index_file = MagicMock(return_value={
        "files": 1,
        "skipped": 0,
        "symbols": 0,
        "calls": 0,
        "imports": 0,
        "variables": 0,
        "variable_flows": 0,
        "errors": 0,
    })
    pipeline._count_symbols = MagicMock(return_value=0)

    pipeline.index_full(str(repo_root))

    calls = graph.query.call_args_list
    delete_idx = next(i for i, call in enumerate(calls) if call.args[0] == S.DELETE_REPO)
    merge_idx = next(i for i, call in enumerate(calls) if call.args[0] == S.MERGE_REPO)
    assert delete_idx < merge_idx


def test_index_full_skips_repo_delete_when_repo_not_yet_indexed(tmp_path: Path) -> None:
    graph = MagicMock()

    def side_effect(cypher: str, params: dict | None = None):
        result = MagicMock()
        if cypher == S.QUERY_REPO_EXISTS:
            result.result_set = [[0]]
        else:
            result.result_set = []
        return result

    graph.query.side_effect = side_effect
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "service.py").write_text("def render(x):\n    return x\n", encoding="utf-8")
    pipeline = IndexPipeline(graph)
    pipeline._index_file = MagicMock(return_value={
        "files": 1,
        "skipped": 0,
        "symbols": 0,
        "calls": 0,
        "imports": 0,
        "variables": 0,
        "variable_flows": 0,
        "errors": 0,
    })
    pipeline._count_symbols = MagicMock(return_value=0)

    pipeline.index_full(str(repo_root))

    calls = graph.query.call_args_list
    assert any(call.args[0] == S.QUERY_REPO_EXISTS for call in calls)
    assert not any(call.args[0] == S.DELETE_REPO for call in calls)
    assert any(call.args[0] == S.MERGE_REPO for call in calls)


def test_index_full_fails_when_repo_path_is_not_visible(tmp_path: Path) -> None:
    graph = MagicMock()
    pipeline = IndexPipeline(graph)

    missing_repo = tmp_path / "missing-repo"

    try:
        pipeline.index_full(str(missing_repo))
    except FileNotFoundError as exc:
        assert "Repository path is not visible" in str(exc)
    else:
        raise AssertionError("Expected index_full to fail for an invisible repository path")

    graph.query.assert_not_called()


def test_index_incremental_resolves_relative_paths_under_repo_root(tmp_path: Path) -> None:
    graph = MagicMock()
    query_result = MagicMock()
    query_result.result_set = []
    graph.query.return_value = query_result

    repo_root = tmp_path / "repo"
    source_dir = repo_root / "src"
    source_dir.mkdir(parents=True)
    source_file = source_dir / "service.py"
    source_file.write_text("def render(x):\n    return x\n", encoding="utf-8")

    pipeline = IndexPipeline(graph)
    pipeline._index_file = MagicMock(return_value={
        "files": 1,
        "skipped": 0,
        "symbols": 0,
        "calls": 0,
        "imports": 0,
        "variables": 0,
        "variable_flows": 0,
        "errors": 0,
    })
    pipeline._count_symbols = MagicMock(return_value=0)

    pipeline.index_incremental(str(repo_root), ["src/service.py"])

    pipeline._index_file.assert_called_once()
    assert pipeline._index_file.call_args.args[1] == str(source_file)


def test_index_incremental_accepts_registered_conventional_filename(
    tmp_path: Path, monkeypatch
) -> None:
    graph = MagicMock()
    query_result = MagicMock()
    query_result.result_set = []
    graph.query.return_value = query_result
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    source_file = repo_root / "CMakeLists.txt"
    source_file.write_text("project(sample)", encoding="utf-8")
    monkeypatch.setattr("backend.indexer.pipeline.SourceParser", MagicMock)
    monkeypatch.setattr(
        runtime_config,
        "get_disabled_parser_languages",
        lambda: frozenset(),
    )
    pipeline = IndexPipeline(graph)
    pipeline._index_file = MagicMock(return_value={
        "files": 1,
        "skipped": 0,
        "symbols": 0,
        "calls": 0,
        "imports": 0,
        "variables": 0,
        "variable_flows": 0,
        "errors": 0,
    })
    pipeline._count_symbols = MagicMock(return_value=0)

    pipeline.index_incremental(str(repo_root), [str(source_file)])

    pipeline._index_file.assert_called_once()
    assert pipeline._index_file.call_args.args[1] == str(source_file)


def test_index_file_clears_existing_file_subgraph_before_rewrite(tmp_path: Path) -> None:
    graph = MagicMock()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    file_path = repo_root / "service.py"
    file_path.write_text(
        "def normalize(raw):\n    value = raw.strip()\n    return value\n",
        encoding="utf-8",
    )
    pipeline = IndexPipeline(graph)
    pipeline._get_stored_hash = MagicMock(return_value="old-hash")
    pipeline._write_python_call_edges = MagicMock(return_value=0)
    pipeline._write_import_edges = MagicMock(return_value=0)
    pipeline._write_variable_flow_edges = MagicMock(return_value={"variables": 0, "variable_flows": 0})
    pipeline._write_cross_scope_variable_flows = MagicMock(return_value=0)

    pipeline._index_file(str(repo_root), str(file_path), {}, force=False)

    calls = graph.query.call_args_list
    delete_idx = next(i for i, call in enumerate(calls) if call.args[0] == S.DELETE_FILE_VARIABLES)
    merge_idx = next(i for i, call in enumerate(calls) if call.args[0] == S.MERGE_FILE)
    assert delete_idx < merge_idx


def test_index_file_writes_calls_from_registered_language(tmp_path: Path) -> None:
    graph = MagicMock()
    query_result = MagicMock()
    query_result.result_set = []
    graph.query.return_value = query_result
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    file_path = repo_root / "Formatter.cs"
    file_path.write_text("public class Formatter {}", encoding="utf-8")
    parsed = ParsedFile(path=str(file_path), language="csharp")
    parsed.symbols.extend(
        [
            ParsedSymbol("Build", "Formatter.Build", "method", str(file_path), 1, 1),
            ParsedSymbol("Normalize", "Formatter.Normalize", "method", str(file_path), 1, 1),
        ]
    )
    parsed.calls.append(RawCall("Formatter.Build", "Normalize"))
    pipeline = IndexPipeline(graph)
    pipeline._parser.parse = MagicMock(return_value=parsed)

    stats = pipeline._index_file(str(repo_root), str(file_path), {}, force=True)

    assert stats["calls"] == 1
    assert any(
        call.args[0] == S.BATCH_EDGE_SYMBOL_CALLS
        and call.args[1]["rows"] == [
            {
                "caller_qname": "Formatter.Build",
                "callee_qname": "Formatter.Normalize",
            }
        ]
        for call in graph.query.call_args_list
        if len(call.args) > 1
    )


def test_index_incremental_deletes_missing_file_subgraph_without_error(tmp_path: Path) -> None:
    graph = MagicMock()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    missing_file = repo_root / "removed.py"
    pipeline = IndexPipeline(graph)
    pipeline._load_symbol_map = MagicMock(return_value={})
    pipeline._count_symbols = MagicMock(return_value=0)

    stats = pipeline.index_incremental(str(repo_root), [str(missing_file)])

    assert stats["errors"] == 0
    assert any(
        call.args[0] == S.DELETE_FILE and call.args[1]["file_path"] == str(missing_file)
        for call in graph.query.call_args_list
        if len(call.args) > 1
    )


def test_index_incremental_deletes_graph_for_disabled_language(
    tmp_path: Path, monkeypatch
) -> None:
    graph = MagicMock()
    query_result = MagicMock()
    query_result.result_set = []
    graph.query.return_value = query_result
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    source_file = repo_root / "service.py"
    source_file.write_text("def run():\n    return True\n", encoding="utf-8")
    monkeypatch.setattr(
        runtime_config,
        "get_disabled_parser_languages",
        lambda: frozenset({"python"}),
    )
    monkeypatch.setattr("backend.indexer.pipeline.SourceParser", MagicMock)
    pipeline = IndexPipeline(graph)
    pipeline._load_symbol_map = MagicMock(return_value={})
    pipeline._count_symbols = MagicMock(return_value=0)
    pipeline._parser.parse = MagicMock()

    stats = pipeline.index_incremental(str(repo_root), [str(source_file)])

    assert stats["skipped"] == 1
    pipeline._parser.parse.assert_not_called()
    assert any(
        call.args[0] == S.DELETE_FILE and call.args[1]["file_path"] == str(source_file)
        for call in graph.query.call_args_list
        if len(call.args) > 1
    )


def test_index_file_gates_resolved_language_for_shared_extension(
    tmp_path: Path, monkeypatch
) -> None:
    graph = MagicMock()
    query_result = MagicMock()
    query_result.result_set = []
    graph.query.return_value = query_result
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    source_file = repo_root / "Formatter.m"
    source_file.write_text("@interface Formatter\n@end\n", encoding="utf-8")
    parser = MagicMock()
    parser.parse.return_value = ParsedFile(path=str(source_file), language="objective_c")
    monkeypatch.setattr("backend.indexer.pipeline.SourceParser", lambda: parser)
    pipeline = IndexPipeline(graph)
    pipeline._get_stored_hash = MagicMock(return_value=sha256_file(str(source_file)))

    stats = pipeline._index_file(
        str(repo_root),
        str(source_file),
        {},
        force=False,
        disabled_languages=frozenset({"objective_c"}),
    )

    assert stats["skipped"] == 1
    parser.parse.assert_called_once_with(str(source_file))
    assert any(
        call.args[0] == S.DELETE_FILE and call.args[1]["file_path"] == str(source_file)
        for call in graph.query.call_args_list
        if len(call.args) > 1
    )
    assert not any(call.args[0] == S.MERGE_FILE for call in graph.query.call_args_list)
