"""Pipeline unit tests complementing the isolated graph lifecycle regressions."""

from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend import runtime_config
from backend.graph import schema as S
from backend.graph.client import GraphClient
from backend.indexer.call_analyzer import RawCall
from backend.indexer.hasher import sha256_file
from backend.indexer.parser import ParsedFile, ParsedSymbol, ParsedVariable, ParsedVariableFlow
from backend.indexer.pipeline import IndexPipeline
from backend.indexer.snapshot import FileSnapshot, INDEX_FORMAT_VERSION, parse_snapshot


def _graphs(rows=None):
    live = MagicMock(spec=GraphClient)
    stage = MagicMock(spec=GraphClient)
    live.atomic_update.return_value = nullcontext(stage)

    def query(cypher, params=None):
        if cypher == S.QUERY_FILE_SNAPSHOTS:
            return SimpleNamespace(result_set=rows or [])
        return SimpleNamespace(result_set=[])

    stage.query.side_effect = query
    return live, stage


def test_write_variable_flow_edges_writes_variable_nodes_and_local_flows():
    graph = MagicMock()
    parsed = ParsedFile(path="repo/service.py", language="python")
    parsed.variables.extend([
        ParsedVariable("input", "pkg.render:input", "pkg.render", "repo/service.py", 10, "parameter"),
        ParsedVariable("label", "pkg.render:label", "pkg.render", "repo/service.py", 11, "local"),
    ])
    parsed.variable_flows.append(
        ParsedVariableFlow("pkg.render:input", "pkg.render:label", "pkg.render", 11, "assignment")
    )

    stats = IndexPipeline(graph)._write_variable_flow_edges(parsed)

    assert stats == {"variables": 2, "variable_flows": 1}
    calls = graph.query.call_args_list
    assert any(call.args[0] == S.BATCH_EDGE_SYMBOL_HAS_VARIABLE for call in calls)
    assert any(
        call.args[0] == S.BATCH_EDGE_VARIABLE_FLOWS
        and any(row["flow_type"] == "assignment" for row in call.args[1]["rows"])
        for call in calls
    )


def test_write_cross_scope_variable_flows_writes_argument_and_return_edges():
    graph = MagicMock()
    graph.query.return_value = SimpleNamespace(result_set=[["pkg.callee", "pkg.callee:param", 1, "param"]])
    calls = [RawCall("pkg.caller", "callee", ["input"], "result")]

    written = IndexPipeline(graph)._write_cross_scope_variable_flows(calls, {"callee": "pkg.callee"})

    assert written == 2
    rows = graph.query.call_args.args[1]["rows"]
    assert any(row["source_qname"] == "pkg.caller:input" and row["target_qname"] == "pkg.callee:param" for row in rows)
    assert any(row["source_qname"] == "pkg.callee:__return__" and row["target_qname"] == "pkg.caller:result" for row in rows)


def test_index_full_rebuilds_only_private_stage(tmp_path):
    source = tmp_path / "service.py"
    source.write_text("def render(x):\n    return x\n", encoding="utf-8")
    live, stage = _graphs([[str(source), "old", None, None]])

    stats = IndexPipeline(live).index_full(str(tmp_path))

    assert stats["files"] == 1
    live.query.assert_not_called()
    stage.query.assert_any_call(S.DELETE_FILE, {"file_path": str(source)})
    assert any(call.args[0] == S.SET_FILE_SNAPSHOT for call in stage.query.call_args_list)


def test_index_full_does_not_delete_files_when_repository_is_new(tmp_path):
    (tmp_path / "service.py").write_text("def render():\n    return 1\n", encoding="utf-8")
    live, stage = _graphs()

    IndexPipeline(live).index_full(str(tmp_path))

    assert not any(call.args[0] == S.DELETE_FILE for call in stage.query.call_args_list)
    stage.query.assert_any_call(S.MERGE_REPO, {"path": str(tmp_path), "name": tmp_path.name})


def test_index_full_fails_when_repo_path_is_not_visible(tmp_path):
    live, _ = _graphs()

    with pytest.raises(FileNotFoundError, match="not visible"):
        IndexPipeline(live).index_full(str(tmp_path / "missing-repo"))

    live.atomic_update.assert_not_called()


def test_incremental_resolves_relative_paths_under_repo_root(tmp_path):
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    source = source_dir / "service.py"
    source.write_text("def render():\n    return 1\n", encoding="utf-8")
    live, stage = _graphs()

    IndexPipeline(live).index_incremental(str(tmp_path), ["src/service.py"])

    writes = [call for call in stage.query.call_args_list if call.args[0] == S.MERGE_FILE]
    assert len(writes) == 1
    assert writes[0].args[1]["path"] == str(source)


def test_index_incremental_accepts_registered_conventional_filename(tmp_path, monkeypatch):
    source = tmp_path / "CMakeLists.txt"
    source.write_text("project(sample)", encoding="utf-8")
    monkeypatch.setattr(runtime_config, "get_disabled_parser_languages", lambda: frozenset())
    live, stage = _graphs()

    stats = IndexPipeline(live).index_incremental(str(tmp_path), [str(source)])

    assert stats["files"] == 1
    assert any(call.args[0] == S.MERGE_FILE for call in stage.query.call_args_list)


def test_same_hash_requires_complete_versioned_evidence_before_skip(tmp_path):
    source = tmp_path / "service.py"
    source.write_text("def good():\n    return 1\n", encoding="utf-8")
    live, stage = _graphs([[str(source), sha256_file(str(source)), None, None]])

    stats = IndexPipeline(live).index_incremental(str(tmp_path), [str(source)])

    assert stats["files"] == 1
    assert stats["skipped"] == 0
    assert any(call.args[0] == S.SET_FILE_SNAPSHOT for call in stage.query.call_args_list)


def test_index_file_writes_calls_from_registered_language(tmp_path, monkeypatch):
    source = tmp_path / "Formatter.cs"
    source.write_text("public class Formatter {}", encoding="utf-8")
    parsed = ParsedFile(path=str(source), language="csharp")
    parsed.symbols.extend([
        ParsedSymbol("Build", "Formatter.Build", "method", str(source), 1, 1),
        ParsedSymbol("Normalize", "Formatter.Normalize", "method", str(source), 1, 1),
    ])
    parsed.calls.append(RawCall("Formatter.Build", "Normalize"))
    monkeypatch.setattr(
        "backend.indexer.pipeline.parse_snapshot",
        lambda path: FileSnapshot(parsed, sha256_file(path)),
    )
    live, stage = _graphs()

    stats = IndexPipeline(live).index_incremental(str(tmp_path), [str(source)])

    assert stats["calls"] == 1
    stage.query.assert_any_call(S.BATCH_EDGE_SYMBOL_CALLS, {"rows": [
        {"caller_qname": "Formatter.Build", "callee_qname": "Formatter.Normalize"}
    ]})


def test_incremental_explicit_deletion_removes_cached_snapshot(tmp_path):
    source = tmp_path / "removed.py"
    cached = FileSnapshot(ParsedFile(path=str(source), language="python"), "old")
    live, stage = _graphs([[str(source), "old", cached.serialize(), INDEX_FORMAT_VERSION]])

    stats = IndexPipeline(live).index_incremental(str(tmp_path), [str(source)])

    assert stats["errors"] == 0
    stage.query.assert_any_call(S.DELETE_FILE, {"file_path": str(source)})
    assert not any(call.args[0] == S.MERGE_FILE for call in stage.query.call_args_list)


def test_incremental_removes_disabled_language_from_generation(tmp_path, monkeypatch):
    source = tmp_path / "service.py"
    source.write_text("def run():\n    return True\n", encoding="utf-8")
    cached = FileSnapshot(ParsedFile(path=str(source), language="python"), "old")
    live, stage = _graphs([[str(source), "old", cached.serialize(), INDEX_FORMAT_VERSION]])
    monkeypatch.setattr(runtime_config, "get_disabled_parser_languages", lambda: frozenset({"python"}))

    stats = IndexPipeline(live).index_incremental(str(tmp_path), [str(source)])

    assert stats["skipped"] == 1
    stage.query.assert_any_call(S.DELETE_FILE, {"file_path": str(source)})
    assert not any(call.args[0] == S.MERGE_FILE for call in stage.query.call_args_list)


def test_resolved_language_gate_applies_to_shared_extension(tmp_path, monkeypatch):
    source = tmp_path / "Formatter.m"
    source.write_text("@interface Formatter\n@end\n", encoding="utf-8")
    live, stage = _graphs([[str(source), "old", None, None]])
    monkeypatch.setattr(runtime_config, "get_disabled_parser_languages", lambda: frozenset({"objective_c"}))
    monkeypatch.setattr(
        "backend.indexer.pipeline.parse_snapshot",
        lambda path: FileSnapshot(ParsedFile(path=path, language="objective_c"), sha256_file(path)),
    )

    stats = IndexPipeline(live).index_incremental(str(tmp_path), [str(source)])

    assert stats["skipped"] == 1
    assert not any(call.args[0] == S.MERGE_FILE for call in stage.query.call_args_list)


def test_parse_snapshot_rejects_edit_then_revert_during_parse(tmp_path):
    source = tmp_path / "service.py"
    original = "def original():\n    pass\n"
    source.write_text(original, encoding="utf-8")

    def parse(path):
        source.write_text("def temporary():\n    pass\n", encoding="utf-8")
        source.write_text(original, encoding="utf-8")
        return ParsedFile(path=str(source), language="python")

    with pytest.raises(RuntimeError, match="changed while being parsed"):
        parse_snapshot(str(source), MagicMock(parse=parse))
