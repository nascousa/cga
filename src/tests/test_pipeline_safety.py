"""Graph lifecycle regressions; every test owns a unique disposable graph."""

from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError, ResponseError

from backend.graph.client import GraphClient, GraphGenerationChanged
from backend.graph import schema as S
from backend.indexer.parser import path_to_module
from backend.indexer.pipeline import IndexPipeline


pytestmark = [pytest.mark.live_graph, pytest.mark.live_graph_smoke]


@pytest.mark.parametrize("replacement", ["none", "target_before_delete", "target_at_delete", "source"])
def test_promotion_deletion_atomically_checks_both_generations(replacement, monkeypatch):
    graphs = [
        GraphClient(
            host=os.getenv("FALKORDB_HOST", "127.0.0.1"),
            port=int(os.getenv("FALKORDB_PORT", "16379")),
            graph_name=f"cga_promotion_{uuid.uuid4().hex}",
        ) for _ in range(2)
    ]
    source, target = graphs
    try:
        for graph in graphs:
            graph.connect()
            graph.ensure_indexes()
            with graph.atomic_update() as stage:
                stage.query("CREATE (:File {path: 'original'})")
            assert stage.published_generation == graph.cache_generation()
        source_generation = source.cache_generation()
        target_generation = target.cache_generation()

        def replace(graph):
            with graph.atomic_update() as stage:
                stage.query("MATCH (n) DETACH DELETE n")
                stage.query("CREATE (:File {path: 'replacement'})")

        if replacement == "source":
            replace(source)
        elif replacement == "target_before_delete":
            replace(target)
        elif replacement == "target_at_delete":
            original_eval = source._db.connection.eval

            def racing_eval(script, *args):
                replace(target)
                return original_eval(script, *args)

            monkeypatch.setattr(source._db.connection, "eval", racing_eval)

        def delete():
            source.delete(
                expected_generation=source_generation,
                expected_target_graph=target._graph_name,
                expected_target_generation=target_generation,
            )

        if replacement == "none":
            delete()
            assert not source._db.connection.exists(source._graph_name)
        else:
            with pytest.raises(GraphGenerationChanged):
                delete()
            assert source._db.connection.exists(source._graph_name)
        assert _count(target, "File") == 1
    finally:
        for graph in graphs:
            if graph._db is not None:
                if graph._db.connection.exists(graph._graph_name):
                    graph.delete()
                graph._db.connection.unlink(graph._generation_key)
                graph.close()


@pytest.fixture
def graph():
    client = GraphClient(
        host=os.getenv("FALKORDB_HOST", "127.0.0.1"),
        port=int(os.getenv("FALKORDB_PORT", "16379")),
        graph_name=f"cga_safety_{uuid.uuid4().hex}",
    )
    client.connect()
    try:
        client.ensure_indexes()
    except RedisConnectionError as exc:
        client.close()
        pytest.skip(f"Isolated graph service unavailable: {exc}")
    try:
        yield client
    finally:
        client.delete()
        client.close()


def _count(graph, label: str) -> int:
    return graph.query(f"MATCH (n:{label}) RETURN count(n)").result_set[0][0]


def _calls(graph) -> set[tuple[str, str]]:
    return {
        tuple(row)
        for row in graph.query(
            "MATCH (a:Symbol)-[:CALLS]->(b:Symbol) RETURN a.qualified_name, b.qualified_name"
        ).result_set
    }


def test_full_write_failure_preserves_published_graph(graph, tmp_path, monkeypatch):
    source = tmp_path / "service.py"
    source.write_text("def good():\n    return 1\n", encoding="utf-8")
    pipeline = IndexPipeline(graph)
    pipeline.index_full(str(tmp_path))
    old_generation = graph.cache_generation()
    query = GraphClient.query

    def fail_stage(self, cypher, params=None, timeout=None):
        if self._staging and cypher == S.MERGE_REPO:
            raise RuntimeError("injected write outage")
        return query(self, cypher, params, timeout)

    monkeypatch.setattr(GraphClient, "query", fail_stage)
    with pytest.raises(RuntimeError, match="injected write outage"):
        pipeline.index_full(str(tmp_path))

    assert _count(graph, "Symbol") == 1
    assert graph.cache_generation() == old_generation


def test_parse_failure_does_not_replace_hash_or_symbols(graph, tmp_path):
    source = tmp_path / "service.py"
    source.write_text("def good():\n    return 1\n", encoding="utf-8")
    pipeline = IndexPipeline(graph)
    pipeline.index_full(str(tmp_path))
    old_hash = graph.query(S.QUERY_FILE_HASH, {"path": str(source)}).result_set
    source.write_text("def broken(:\n", encoding="utf-8")

    with pytest.raises(ValueError, match="parse"):
        pipeline.index_incremental(str(tmp_path), ["service.py"])

    assert _count(graph, "Symbol") == 1
    assert graph.query(S.QUERY_FILE_HASH, {"path": str(source)}).result_set == old_hash


def test_empty_full_rebuild_keeps_existing_graph(graph, tmp_path):
    source = tmp_path / "service.py"
    source.write_text("def good():\n    return 1\n", encoding="utf-8")
    pipeline = IndexPipeline(graph)
    pipeline.index_full(str(tmp_path))
    source.unlink()

    with pytest.raises(ValueError, match="[Ee]mpty"):
        pipeline.index_full(str(tmp_path))

    assert _count(graph, "File") == 1
    pipeline.index_incremental(str(tmp_path), ["service.py"])
    assert _count(graph, "File") == 0


def test_cross_file_calls_are_complete_and_stable(graph, tmp_path):
    (tmp_path / "a.py").write_text(
        "from b import helper\ndef entry():\n    return helper()\n",
        encoding="utf-8",
    )
    (tmp_path / "b.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    pipeline = IndexPipeline(graph)
    expected = {
        (path_to_module(str(tmp_path / "a.py")) + ".entry",
         path_to_module(str(tmp_path / "b.py")) + ".helper")
    }
    for _ in range(3):
        pipeline.index_full(str(tmp_path))
        assert _calls(graph) == expected


def test_import_binding_wins_over_unrelated_same_name(graph, tmp_path):
    (tmp_path / "wanted.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    (tmp_path / "unrelated.py").write_text("def helper():\n    return 2\n", encoding="utf-8")
    (tmp_path / "caller.py").write_text(
        "from wanted import helper as selected\ndef entry():\n    return selected()\n",
        encoding="utf-8",
    )

    IndexPipeline(graph).index_full(str(tmp_path))

    assert _calls(graph) == {
        (path_to_module(str(tmp_path / "caller.py")) + ".entry",
         path_to_module(str(tmp_path / "wanted.py")) + ".helper")
    }


def test_incremental_callee_update_preserves_unchanged_callers(graph, tmp_path):
    (tmp_path / "a.py").write_text(
        "from b import helper\ndef entry():\n    return helper()\n", encoding="utf-8"
    )
    target = tmp_path / "b.py"
    target.write_text("def helper():\n    return 1\n", encoding="utf-8")
    pipeline = IndexPipeline(graph)
    pipeline.index_full(str(tmp_path))
    old_calls = _calls(graph)
    assert len(old_calls) == 1
    target.write_text("def helper():\n    return 2\n", encoding="utf-8")

    pipeline.index_incremental(str(tmp_path), ["b.py"])

    assert _calls(graph) == old_calls


def test_failed_relationship_write_is_retried_without_hash_skip(graph, tmp_path, monkeypatch):
    source = tmp_path / "service.py"
    source.write_text("def helper():\n    return 1\ndef entry():\n    return helper()\n", encoding="utf-8")
    pipeline = IndexPipeline(graph)
    pipeline.index_full(str(tmp_path))
    old_hash = graph.query(S.QUERY_FILE_HASH, {"path": str(source)}).result_set
    source.write_text("def helper():\n    return 2\ndef entry():\n    return helper()\n", encoding="utf-8")
    query = GraphClient.query

    def fail_calls(self, cypher, params=None, timeout=None):
        if self._staging and cypher == S.BATCH_EDGE_SYMBOL_CALLS:
            raise RuntimeError("injected CALLS outage")
        return query(self, cypher, params, timeout)

    with monkeypatch.context() as patcher:
        patcher.setattr(GraphClient, "query", fail_calls)
        with pytest.raises(RuntimeError, match="CALLS outage"):
            pipeline.index_incremental(str(tmp_path), ["service.py"])
    assert graph.query(S.QUERY_FILE_HASH, {"path": str(source)}).result_set == old_hash

    stats = pipeline.index_incremental(str(tmp_path), ["service.py"])

    assert stats["files"] == 1
    assert stats["errors"] == 0
    assert len(_calls(graph)) == 1


def test_identical_incremental_does_not_change_published_generation(graph, tmp_path):
    (tmp_path / "service.py").write_text("def good():\n    return 1\n", encoding="utf-8")
    pipeline = IndexPipeline(graph)
    pipeline.index_full(str(tmp_path))
    before = graph.cache_generation()

    stats = pipeline.index_incremental(str(tmp_path), ["service.py"])

    assert stats["skipped"] == 1
    assert graph.cache_generation() == before


def test_lost_write_lease_cannot_publish(graph):
    graph.query("CREATE (:File {path:'old.py'})")
    before = graph.cache_generation()

    with pytest.raises(ResponseError, match="graph write lease lost"):
        with graph.atomic_update() as stage:
            stage.query("MATCH (n) DETACH DELETE n")
            graph._db.connection.delete(graph._lease_key)

    assert _count(graph, "File") == 1
    assert graph.cache_generation() == before


def test_promotion_cleanup_preserves_a_newer_source_generation(graph):
    before = graph.cache_generation()
    with graph.atomic_update() as stage:
        stage.query("CREATE (:File {path:'new-source-change.py'})")

    with pytest.raises(RuntimeError, match="source graph was retained"):
        graph.delete(expected_generation=before)

    assert _count(graph, "File") == 1


def test_concurrent_writers_do_not_lose_committed_updates(graph):
    entered = threading.Event()
    release = threading.Event()

    def first():
        with graph.atomic_update() as stage:
            entered.set()
            assert release.wait(10)
            stage.query("CREATE (:File {path:'first.py'})")

    def second():
        assert entered.wait(10)
        other = GraphClient(host=graph._host, port=graph._port, graph_name=graph._graph_name)
        other.connect()
        try:
            with other.atomic_update() as stage:
                stage.query("CREATE (:File {path:'second.py'})")
        finally:
            other.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        one = executor.submit(first)
        two = executor.submit(second)
        assert entered.wait(10)
        release.set()
        one.result(timeout=30)
        two.result(timeout=30)

    assert _count(graph, "File") == 2


def test_cached_shared_extension_obeys_changed_parser_settings(graph, tmp_path, monkeypatch):
    source = tmp_path / "Formatter.m"
    source.write_text("@interface Formatter\n@end\n", encoding="utf-8")
    pipeline = IndexPipeline(graph)
    pipeline.index_full(str(tmp_path))
    assert _count(graph, "File") == 1
    monkeypatch.setattr(
        "backend.runtime_config.get_disabled_parser_languages",
        lambda: frozenset({"objective_c"}),
    )

    pipeline.index_incremental(str(tmp_path), [str(source)])

    assert _count(graph, "File") == 0
