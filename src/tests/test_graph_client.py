"""Tests for FalkorDB GraphClient index creation behavior."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from redis.exceptions import ResponseError

from backend.graph.client import GraphClient
from backend.graph.registry import GraphRegistry


def test_ensure_indexes_creates_variable_indexes() -> None:
    client = GraphClient()
    client.query = MagicMock()

    client.ensure_indexes()

    statements = [call.args[0] for call in client.query.call_args_list]
    assert "CREATE INDEX FOR (n:Variable) ON (n.name)" in statements
    assert "CREATE INDEX FOR (n:Variable) ON (n.qualified_name)" in statements
    assert "CREATE INDEX FOR (n:Variable) ON (n.scope_qname)" in statements


def test_ensure_indexes_ignores_existing_index_errors() -> None:
    client = GraphClient()

    def side_effect(cypher: str, params=None):
        if "Variable" in cypher:
            raise RuntimeError("index already exists")
        return MagicMock()

    client.query = MagicMock(side_effect=side_effect)

    client.ensure_indexes()

    assert client.query.call_count >= 1


def test_ensure_indexes_propagates_database_failures() -> None:
    client = GraphClient()
    client.query = MagicMock(side_effect=RuntimeError("database unavailable"))

    with pytest.raises(RuntimeError, match="database unavailable"):
        client.ensure_indexes()


def test_atomic_update_does_not_publish_failed_build() -> None:
    client = GraphClient(graph_name="demo")
    client._db = MagicMock()
    client._graph = MagicMock()
    connection = client._db.connection
    connection.exists.return_value = True
    lock = connection.lock.return_value
    lock.acquire.return_value = True
    lock.local.token = b"lease"

    with pytest.raises(ValueError, match="invalid source"):
        with client.atomic_update():
            raise ValueError("invalid source")

    client._graph.copy.assert_called_once()
    connection.eval.assert_not_called()
    lock.release.assert_called_once()


def test_atomic_update_publishes_only_after_build_finishes() -> None:
    client = GraphClient(graph_name="demo")
    client._db = MagicMock()
    client._graph = MagicMock()
    connection = client._db.connection
    connection.exists.return_value = True
    lock = connection.lock.return_value
    lock.acquire.return_value = True
    lock.local.token = b"lease"

    with client.atomic_update() as staged:
        assert staged is not client
        assert staged._graph_name != "demo"
        connection.eval.assert_not_called()

    connection.eval.assert_called_once()
    assert "RENAME" in connection.eval.call_args.args[0]
    assert "PERSIST" in connection.eval.call_args.args[0]
    lock.release.assert_called_once()


def test_atomic_update_fails_when_write_lease_is_busy() -> None:
    client = GraphClient(graph_name="demo")
    client._db = MagicMock()
    client._graph = MagicMock()
    client._db.connection.lock.return_value.acquire.return_value = False

    with pytest.raises(TimeoutError, match="graph write lease"):
        with client.atomic_update():
            raise AssertionError("build must not start")

    client._graph.copy.assert_not_called()


def test_atomic_update_retries_copy_when_background_save_owns_fork() -> None:
    client = GraphClient(graph_name="demo")
    client._db = MagicMock()
    client._graph = MagicMock()
    connection = client._db.connection
    connection.exists.side_effect = lambda name: name == "demo"
    connection.lock.return_value.acquire.return_value = True
    connection.lock.return_value.local.token = b"lease"
    client._graph.copy.side_effect = [
        ResponseError("GRAPH.COPY failed, could not fork"),
        MagicMock(),
    ]

    with client.atomic_update():
        pass

    assert client._graph.copy.call_count == 2
    connection.eval.assert_called_once()


def test_delete_uses_connected_falkordb_graph() -> None:
    client = GraphClient()
    graph = MagicMock()
    client._graph = graph

    client.delete()

    graph.delete.assert_called_once_with()


def test_registry_delete_removes_cached_graph_client() -> None:
    registry = GraphRegistry("localhost", 6379)
    graph = MagicMock()
    registry._graphs["demo__ref__feature"] = graph

    registry.delete("Demo__Ref__Feature")

    graph.delete.assert_called_once_with()
    graph.close.assert_called_once_with()
    assert "demo__ref__feature" not in registry._graphs