"""Pipeline unit tests complementing the isolated graph lifecycle regressions."""

from __future__ import annotations

from contextlib import nullcontext
import json
import textwrap
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend import runtime_config
from backend.graph import schema as S
from backend.graph.client import GraphClient
from backend.indexer.call_analyzer import RawCall
from backend.indexer.hasher import sha256_file
from backend.indexer.parser import ParsedFile, ParsedSymbol, ParsedVariable, ParsedVariableFlow, path_to_module
from backend.indexer.pipeline import IndexPipeline
from backend.indexer.snapshot import FileSnapshot, INDEX_FORMAT_VERSION, parse_snapshot


def _graphs(rows=None):
    live = MagicMock(spec=GraphClient)
    stage = MagicMock(spec=GraphClient)
    stage.published_generation = "published-by-this-stage"
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
    assert stats["published_generation"] == "published-by-this-stage"
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


def _index_python_source(tmp_path, source):
    path = tmp_path / "service.py"
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    live, stage = _graphs()
    IndexPipeline(live).index_full(str(tmp_path))
    return path_to_module(str(path)), stage


def _argument_edges(stage):
    return {
        (row["source_qname"], row["target_qname"])
        for call in stage.query.call_args_list if call.args[0] == S.BATCH_EDGE_VARIABLE_FLOWS
        for row in call.args[1]["rows"] if row["flow_type"] == "argument"
    }


@pytest.mark.parametrize(("expression", "expected"), [
    ("callee(0, x)", {("x", "b")}),
    ("callee(b=x)", {("x", "b")}),
    ("callee(x, x)", {("x", "a"), ("x", "b")}),
    ("callee(x + y, z)", {("x", "a"), ("y", "a"), ("z", "b")}),
    ("callee(b=x + y, a=z)", {("x", "b"), ("y", "b"), ("z", "a")}),
    ("callee((x, {'value': y}), [z, x])", {("x", "a"), ("y", "a"), ("z", "b"), ("x", "b")}),
    ("callee(0, 0)", set()),
])
def test_python_arguments_bind_to_their_original_positions_and_keywords(tmp_path, expression, expected):
    module, stage = _index_python_source(tmp_path, f"""\
        def callee(a=0, b=0):
            return a + b
        def caller(x, y, z):
            return {expression}
    """)

    assert _argument_edges(stage) == {
        (f"{module}.caller:{source}", f"{module}.callee:{target}")
        for source, target in expected
    }


def test_nested_argument_expressions_keep_all_dependencies_in_the_correct_slot(tmp_path):
    module, stage = _index_python_source(tmp_path, """\
        def wrap(value):
            return value
        def callee(a, b):
            return b
        def caller(x, y, z):
            return callee(0, wrap(x + y) + z)
    """)

    assert {
        edge for edge in _argument_edges(stage) if edge[0].startswith(f"{module}.caller:")
    } == {
        *((f"{module}.caller:{name}", f"{module}.callee:b") for name in ("x", "y", "z")),
        *((f"{module}.caller:{name}", f"{module}.wrap:value") for name in ("x", "y")),
    }
    variable_qnames = {
        row["qualified_name"]
        for call in stage.query.call_args_list if call.args[0] == S.BATCH_MERGE_VARIABLES
        for row in call.args[1]["rows"]
    }
    assert all(source in variable_qnames and target in variable_qnames for source, target in _argument_edges(stage))


def test_direct_arguments_respect_positional_only_keyword_only_and_variadic_parameters(tmp_path):
    module, stage = _index_python_source(tmp_path, """\
        def callee(a, /, b=0, *values, flag=0, **options):
            return a
        def caller(x, y, z):
            return callee(x, y, z, x, flag=z, extra=y)
    """)

    assert _argument_edges(stage) == {
        (f"{module}.caller:{source}", f"{module}.callee:{target}")
        for source, target in (
            ("x", "a"), ("y", "b"), ("z", "values"),
            ("x", "values"), ("z", "flag"), ("y", "options"),
        )
    }


@pytest.mark.parametrize("name", ["self", "cls"])
def test_plain_function_parameters_named_like_receivers_are_still_argument_dependencies(tmp_path, name):
    module, stage = _index_python_source(tmp_path, f"""\
        def callee(value):
            return value
        def caller({name}):
            return callee({name})
    """)

    assert _argument_edges(stage) == {(f"{module}.caller:{name}", f"{module}.callee:value")}


@pytest.mark.parametrize("expression", [
    "callee(x, *items)",
    "callee(x, **options)",
    "callee(*items, b=x)",
    "callee(b=x, **options)",
])
def test_unresolved_argument_unpacking_does_not_invent_argument_flows(tmp_path, expression):
    module, stage = _index_python_source(tmp_path, f"""\
        def callee(a=0, b=0):
            return b
        def caller(x, items, options):
            result = {expression}
            return result
    """)

    assert _argument_edges(stage) == set()
    assert any(
        row["flow_type"] == "call_return"
        and row["source_qname"] == f"{module}.callee:__return__"
        and row["target_qname"] == f"{module}.caller:result"
        for call in stage.query.call_args_list if call.args[0] == S.BATCH_EDGE_VARIABLE_FLOWS
        for row in call.args[1]["rows"]
    )


def test_method_argument_binding_accounts_for_receivers_and_nested_closures(tmp_path):
    module, stage = _index_python_source(tmp_path, """\
        class Worker:
            def run(this, a=0, b=0):
                return b
            @classmethod
            def make(cls, a=0, b=0):
                return b
            @staticmethod
            def utility(a=0, b=0):
                return b
            def caller(self, x):
                def helper():
                    return self.run(0, x)
                return helper()
        def direct(x):
            return Worker.run(None, 0, x)
        def factory(x):
            return Worker.make(b=x)
        def utility(x):
            return Worker.utility(b=x)
    """)

    assert _argument_edges(stage) == {
        (f"{module}.{source}:x", f"{module}.Worker.{target}:b")
        for source, target in (
            ("Worker.caller", "run"), ("direct", "run"),
            ("factory", "make"), ("utility", "utility"),
        )
    }


def test_nested_calls_use_enclosing_argument_and_nonlocal_result_variable_identities(tmp_path):
    module, stage = _index_python_source(tmp_path, """\
        def callee(value):
            return value
        def outer(value):
            result = None
            def inner():
                nonlocal result
                result = callee(value)
            return inner()
    """)

    assert _argument_edges(stage) == {(f"{module}.outer:value", f"{module}.callee:value")}
    assert any(
        row["flow_type"] == "call_return"
        and row["source_qname"] == f"{module}.callee:__return__"
        and row["target_qname"] == f"{module}.outer:result"
        for call in stage.query.call_args_list if call.args[0] == S.BATCH_EDGE_VARIABLE_FLOWS
        for row in call.args[1]["rows"]
    )


def test_cached_structured_calls_and_signatures_rebuild_the_same_argument_flows(tmp_path):
    source = tmp_path / "service.py"
    source.write_text(
        "def callee(a=0, b=0):\n    return b\n"
        "def caller(x):\n    return callee(b=x)\n",
        encoding="utf-8",
    )
    cached = parse_snapshot(str(source))
    (tmp_path / "added.py").write_text("def added():\n    pass\n", encoding="utf-8")
    live, stage = _graphs([[str(source), cached.content_hash, cached.serialize(), INDEX_FORMAT_VERSION]])

    IndexPipeline(live).index_incremental(str(tmp_path), ["added.py"])

    module = path_to_module(str(source))
    assert _argument_edges(stage) == {(f"{module}.caller:x", f"{module}.callee:b")}


def test_legacy_snapshot_is_readable_but_reparsed_even_without_changed_paths(tmp_path):
    source = tmp_path / "service.py"
    source.write_text(
        "def first():\n    def helper():\n        target()\n"
        "def second():\n    def helper():\n        target()\n",
        encoding="utf-8",
    )
    cached = parse_snapshot(str(source))
    legacy = json.loads(cached.serialize())
    legacy.pop("scope_parents")
    for symbol in legacy["parsed"]["symbols"]:
        symbol.pop("parameters")
        symbol.pop("method_binding")
    for call in legacy["parsed"]["calls"]:
        call.pop("arguments")
        call.pop("result_var_qname")
    metadata = json.dumps(legacy)
    assert all(call.arguments is None for call in FileSnapshot.deserialize(metadata).parsed.calls)
    live, stage = _graphs([[str(source), cached.content_hash, metadata, INDEX_FORMAT_VERSION - 1]])

    stats = IndexPipeline(live).index_incremental(str(tmp_path), [])

    assert stats["files"] == 1
    module = path_to_module(str(source))
    symbol_qnames = {
        row["qualified_name"]
        for call in stage.query.call_args_list if call.args[0] == S.BATCH_MERGE_SYMBOLS
        for row in call.args[1]["rows"]
    }
    assert {f"{module}.first.helper", f"{module}.second.helper"} <= symbol_qnames
    writes = [call.args[1] for call in stage.query.call_args_list if call.args[0] == S.SET_FILE_SNAPSHOT]
    assert writes[0]["version"] == INDEX_FORMAT_VERSION
    assert all(call.arguments is not None for call in FileSnapshot.deserialize(writes[0]["metadata"]).parsed.calls)
