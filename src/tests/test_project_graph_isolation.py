"""Mock-only regression tests for project roots, branch ownership and promotion."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from backend.auth import access, context, middleware, models, pgshim
from backend.auth import router as auth_router
from backend.auth.database import get_db
from backend.auth.dependencies import get_current_user
from backend.cga_relay import router as relay
from backend.graph import schema as S
from backend.graph.client import GraphGenerationChanged
from backend.graph.registry import _current_project_name
from backend.indexer import paths
from backend.tools import server


class Cursor:
    def __init__(self, row):
        self.row = row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def fetchone(self):
        return self.row

    async def fetchall(self):
        return self.row


class Database:
    def __init__(self, row):
        self.row = row
        self.queries = []

    def execute(self, sql, params=()):
        self.queries.append((sql, params))
        return Cursor(self.row)

    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


class Registry:
    def __init__(self):
        self.keys = set()
        self.markers = {}
        self.retrieval_rows = []
        self.counts = {}
        self.generations = {}
        self.deleted = []
        self.queries = []
        self.selected = []
        self.connection = SimpleNamespace(
            exists=lambda key: key in self.keys,
            get=lambda key: self.markers.get(key),
        )

    def get(self, name):
        self.selected.append(name)

        def query(cypher, params=None):
            self.queries.append((name, cypher, params))
            if cypher == S.QUERY_RETRIEVE_CONTEXT:
                rows = self.retrieval_rows
            elif "RETURN count(f)" in cypher:
                rows = [[self.counts.get(name, 0)]]
            else:
                rows = []
            return SimpleNamespace(result_set=rows)

        return SimpleNamespace(
            _db=SimpleNamespace(connection=self.connection),
            query=query,
            cache_generation=lambda: self.generations.get(name, "generation-1"),
        )

    def current(self):
        return self.get(_current_project_name.get())

    def delete(self, name, *, expected_generation=None):
        if expected_generation is not None and self.generations.get(name, "generation-1") != expected_generation:
            raise GraphGenerationChanged("Graph changed during promotion")
        self.deleted.append(name)


@pytest.fixture(autouse=True)
def isolated_services(monkeypatch):
    import asyncpg
    import falkordb
    import socket

    no_network = Mock(side_effect=AssertionError("Live services are prohibited in isolation tests"))
    monkeypatch.setattr(pgshim, "get_pool", no_network)
    monkeypatch.setattr(asyncpg, "connect", AsyncMock(side_effect=no_network))
    monkeypatch.setattr(asyncpg, "create_pool", no_network)
    monkeypatch.setattr(falkordb, "FalkorDB", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(server, "_producer", AsyncMock())
    monkeypatch.setattr(server, "_consumer", None)
    monkeypatch.setattr(server, "_cache", None)
    monkeypatch.setattr(server, "_recorder", None)
    monkeypatch.setattr(access.runtime_config, "get_indexing_repo_search_roots", lambda *_: [])
    registry = Registry()
    monkeypatch.setattr(server, "_registry", registry)
    token = context._current_project_scope.set(None)
    ref_token = context._current_ref.set("")
    name_token = _current_project_name.set("contextgraph")
    try:
        yield registry
    finally:
        _current_project_name.reset(name_token)
        context._current_ref.reset(ref_token)
        context._current_project_scope.reset(token)


@pytest.fixture
def projects(tmp_path):
    alpha, bravo = tmp_path / "alpha", tmp_path / "bravo"
    alpha.mkdir()
    bravo.mkdir()
    (alpha / "owned.py").write_text("def owned():\n    return 'alpha-only'\n", encoding="utf-8")
    (bravo / "private.py").write_text("def private():\n    return 'BRAVO-DO-NOT-READ'\n", encoding="utf-8")
    record_a = {"id": 1, "project_id": "project-alpha-id", "project_name": "alpha", "repo_path": str(alpha)}
    record_b = {"id": 2, "project_id": "project-bravo-id", "project_name": "bravo", "repo_path": str(bravo)}
    return SimpleNamespace(
        alpha=alpha, bravo=bravo, record_a=record_a, record_b=record_b,
        scope_a=access.registered_project_scope(record_a),
        scope_b=access.registered_project_scope(record_b),
    )


def assert_forbidden(call):
    with pytest.raises(HTTPException) as caught:
        call()
    assert caught.value.status_code == 403


@pytest.mark.parametrize("kind", ["relative", "absolute", "windows-separators", "drive-relative", "ads", "network", "device"])
def test_shared_path_helper_rejects_escape(projects, kind):
    attempted = {
        "relative": "../bravo/private.py",
        "absolute": str(projects.bravo / "private.py"),
        "windows-separators": "..\\bravo\\private.py",
        "drive-relative": "C:private.py",
        "ads": "owned.py:private",
        "network": r"\\untrusted.example\share\private.py",
        "device": r"\\.\pipe\private",
    }[kind]
    with pytest.raises(paths.RepositoryPathError):
        paths.resolve_changed_path(str(projects.alpha), projects.alpha, attempted)


def test_shared_path_helper_keeps_missing_tombstones_inside_root(projects):
    target = projects.alpha / "deleted" / "old.py"
    assert paths.resolve_changed_path(str(projects.alpha), projects.alpha, "deleted/old.py") == str(target)
    assert paths.resolve_repo_root(str(projects.alpha / ".." / "alpha")) == projects.alpha


@pytest.mark.parametrize("path", [r"\\untrusted.example\share", r"\\.\pipe\private", r"\\?\C:\private"])
def test_network_and_device_roots_are_rejected_without_filesystem_probe(monkeypatch, path):
    probe = Mock(side_effect=AssertionError("Untrusted network paths must not be probed"))
    monkeypatch.setattr(paths, "Path", probe)
    with pytest.raises(paths.RepositoryPathError):
        paths.resolve_repo_root(path)
    probe.assert_not_called()


def test_shared_helper_resolves_symlink_before_containment(projects, monkeypatch):
    alias = projects.alpha / "alias.py"
    real_resolve = Path.resolve

    def resolve(path, *args, **kwargs):
        if path == alias:
            return projects.bravo / "private.py"
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(paths.RepositoryPathError):
        paths.resolve_changed_path(str(projects.alpha), projects.alpha, "alias.py")
    with context.bind_project_scope(projects.scope_a):
        assert_forbidden(lambda: server._read_symbol_snippet(str(alias), 1, 2))


def test_native_symlink_and_symlinked_deleted_parent_are_rejected(projects):
    link = projects.alpha / "outside"
    try:
        link.symlink_to(projects.bravo, target_is_directory=True)
    except OSError:
        pytest.skip("Native symlink creation is not permitted on this host")
    for file in ("private.py", "deleted.py"):
        with pytest.raises(paths.RepositoryPathError):
            paths.resolve_changed_path(str(projects.alpha), projects.alpha, f"outside/{file}")


def test_windows_paths_map_only_within_the_registered_container_repo(projects, monkeypatch):
    native_path = Path
    mount = projects.alpha.parent
    raw_root = "Z:/Repos/alpha"

    def container_path(*parts):
        raw = str(parts[0]) if len(parts) == 1 else ""
        if raw.startswith("Z:/"):
            return SimpleNamespace(exists=lambda: False, is_dir=lambda: False, is_absolute=lambda: False)
        if raw == "/repos":
            return mount
        return native_path(*parts)

    monkeypatch.setattr(paths, "Path", container_path)
    assert paths.resolve_repo_root(raw_root) == projects.alpha
    assert paths.normalize_repo_path(raw_root + "/deleted.py") == str(projects.alpha / "deleted.py")
    assert paths.resolve_changed_path(raw_root, projects.alpha, raw_root + "/owned.py") == str(projects.alpha / "owned.py")
    with pytest.raises(paths.RepositoryPathError):
        paths.resolve_changed_path(raw_root, projects.alpha, "Z:/Repos/bravo/private.py")
    with pytest.raises(paths.RepositoryPathError):
        paths.resolve_repo_root("Z:/Repos/alpha/../bravo")


@pytest.mark.parametrize("prefix", ["__cga_ref_v2__", "__cga_stage__"])
def test_missing_scope_and_reserved_project_names_fail_closed(projects, prefix):
    assert_forbidden(lambda: server._resolve_project_name("alpha"))
    assert_forbidden(lambda: server._read_symbol_snippet(str(projects.alpha / "owned.py"), 1, 2))
    assert_forbidden(lambda: relay._graph_name_for_project("alpha", "feature/read"))
    reserved = prefix + "a" * 32
    for model in (models.ProjectCreate, models.ProjectUpdate):
        with pytest.raises(ValidationError):
            model(project_name=reserved)
    assert_forbidden(lambda: access.registered_project_scope({**projects.record_a, "project_name": reserved}))


def test_registered_root_is_required_and_default_repo_discovery_is_server_owned(projects, monkeypatch):
    missing = access.registered_project_scope({**projects.record_a, "repo_path": ""})
    with context.bind_project_scope(missing):
        assert_forbidden(lambda: access.authorized_repo_root(str(projects.bravo)))
    monkeypatch.setattr(access.runtime_config, "get_indexing_repo_search_roots", lambda *_: [projects.alpha.parent])
    discovered = access.registered_project_scope({**projects.record_a, "repo_path": ""})
    with context.bind_project_scope(discovered):
        assert access.authorized_repo_root() == projects.alpha
        assert_forbidden(lambda: access.authorized_repo_root(str(projects.bravo)))


def test_scope_restores_after_nested_trusted_admin_calls(projects):
    with context.bind_project_scope(projects.scope_a):
        with context.bind_project_scope(projects.scope_b):
            assert server._resolve_project_name() == "bravo"
        assert server._resolve_project_name() == "alpha"
    assert context._current_project_scope.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["index_full", "index_incremental", "index_repo_changes"])
async def test_mcp_all_index_entrypoints_reject_cross_project_root(projects, entrypoint):
    with context.bind_project_scope(projects.scope_a):
        kwargs = {"repo_path": str(projects.bravo)}
        if entrypoint == "index_incremental":
            kwargs["changed_paths"] = ["private.py"]
        with pytest.raises(HTTPException) as caught:
            await getattr(server, entrypoint)(**kwargs)
    assert caught.value.status_code == 403
    server._producer.submit_full_index.assert_not_awaited()
    server._producer.submit_incremental_index.assert_not_awaited()


@pytest.mark.asyncio
async def test_mcp_validates_entire_changed_batch_before_enqueue(projects):
    with context.bind_project_scope(projects.scope_a):
        with pytest.raises(HTTPException) as caught:
            await server.index_incremental(str(projects.alpha), ["owned.py", "../bravo/private.py"])
    assert caught.value.status_code == 403
    server._producer.submit_incremental_index.assert_not_awaited()


@pytest.mark.asyncio
async def test_mcp_canonicalizes_safe_batch_and_tombstone_before_enqueue(projects):
    server._producer.submit_incremental_index.return_value = {"job_id": "safe-job", "stream_id": "1-0"}
    with context.bind_project_scope(projects.scope_a):
        result = await server.index_incremental(str(projects.alpha), ["owned.py", "deleted.py"])
    assert result["status"] == "queued"
    server._producer.submit_incremental_index.assert_awaited_once_with(
        str(projects.alpha), [str(projects.alpha / "owned.py"), str(projects.alpha / "deleted.py")],
        project_name="alpha",
    )


@pytest.mark.asyncio
async def test_mcp_prevents_direct_and_context_only_graph_overrides(projects):
    reserved = context.branch_graph_name(projects.scope_a.project_id, "feature/own")
    with context.bind_project_scope(projects.scope_a):
        for name in ("bravo", reserved, "__cga_stage__pending"):
            with pytest.raises(HTTPException) as caught:
                await server.index_incremental(str(projects.alpha), ["owned.py"], project_name=name)
            assert caught.value.status_code == 403
        token = _current_project_name.set(reserved)
        try:
            assert_forbidden(lambda: server._resolve_project_name())
        finally:
            _current_project_name.reset(token)
    server._producer.submit_incremental_index.assert_not_awaited()


@pytest.mark.asyncio
async def test_git_changes_are_checked_before_full_fallback(projects, monkeypatch):
    collector = AsyncMock(return_value={"changed_paths": ["owned.py"], "destructive_paths": ["../bravo/deleted.py"]})
    monkeypatch.setattr(server, "_collect_git_changed_paths", collector)
    with context.bind_project_scope(projects.scope_a):
        with pytest.raises(HTTPException):
            await server.index_repo_changes(str(projects.alpha), auto_full_on_destructive=True)
    server._producer.submit_full_index.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["index_full", "index_incremental", "index_git_incremental", "promote_ref"])
async def test_relay_checks_root_before_backend_calls(projects, monkeypatch, tool):
    incremental = AsyncMock()
    full = AsyncMock()
    monkeypatch.setattr(server, "index_incremental", incremental)
    monkeypatch.setattr(server, "index_full", full)
    with context.bind_project_scope(projects.scope_a):
        with pytest.raises(HTTPException) as caught:
            await relay.dispatch_tool(
                tool, {"repo_path": str(projects.bravo), "changed_paths": ["private.py"], "ref_id": "feature/test"},
                "alpha",
            )
    assert caught.value.status_code == 403
    incremental.assert_not_awaited()
    full.assert_not_awaited()


@pytest.mark.asyncio
async def test_account_relay_scope_comes_from_db_after_project_access_check(projects, monkeypatch):
    db = Database(projects.record_a)
    require_access = AsyncMock()
    monkeypatch.setattr(relay, "require_project_access", require_access)
    account = {"id": 50, "role": "developer"}
    resolved = await relay._account_project_context(db, projects.scope_a.project_id, account)
    require_access.assert_awaited_once_with(db, account, projects.scope_a.project_db_id)
    assert "repo_path" in db.queries[0][0]
    with pytest.raises(HTTPException) as caught:
        await relay._dispatch_with_project_context(
            "index_incremental", {"repo_path": str(projects.bravo), "changed_paths": ["private.py"]}, resolved
        )
    assert caught.value.status_code == 403
    assert context._current_project_scope.get() is None


@pytest.mark.asyncio
async def test_project_token_middleware_binds_db_root_and_resets_scope(projects, monkeypatch):
    token_row = {
        "id": 8, "project_id": 1, "project_external_id": projects.scope_a.project_id,
        "project_name": "alpha", "token_type": "mcp", "repo_path": str(projects.alpha),
    }
    db = Database(token_row)
    monkeypatch.setattr(pgshim, "get_pool", lambda: db)
    monkeypatch.setattr(middleware, "hash_token", lambda _: "synthetic-test-digest")
    monkeypatch.setattr(middleware, "validate_crystal_suite_headers", lambda _: None)
    seen = []

    async def endpoint(scope, receive, send):
        seen.append(access.authorized_repo_root())
        assert scope["state"]["registered_project_scope"].repo_path == str(projects.alpha)
        with pytest.raises(HTTPException) as caught:
            await server.index_incremental(str(projects.bravo), ["private.py"])
        assert caught.value.status_code == 403

    await middleware.ProjectTokenMiddleware(endpoint)(
        {
            "type": "http", "method": "POST", "path": "/mcp/",
            "headers": [(b"authorization", b"Bearer synthetic-test-token"), (b"x-project-id", projects.scope_a.project_id.encode()),
                        (b"x-repo-path", str(projects.bravo).encode())],
        },
        AsyncMock(), AsyncMock(),
    )
    assert seen == [projects.alpha]
    assert "p.repo_path" in db.queries[0][0]
    assert context._current_project_scope.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["trigger_project_index", "trigger_project_full_index"])
@pytest.mark.parametrize("cross_root", [False, True])
async def test_admin_index_route_binds_registered_scope_without_relaxing_root(projects, monkeypatch, endpoint, cross_root):
    monkeypatch.setattr(auth_router, "_get_active_project", AsyncMock(return_value=projects.record_a))
    requested_root = projects.bravo if cross_root else projects.alpha
    monkeypatch.setattr(auth_router, "_resolve_project_repo_path", lambda _: str(requested_root))
    monkeypatch.setattr(server, "_collect_git_changed_paths", AsyncMock(return_value={
        "changed_paths": ["owned.py"], "destructive_paths": [],
    }))
    seen = []

    async def publish(repo_path, *args, project_name):
        seen.append(context.require_project_scope())
        assert context.authorized_graph_name(project_name) == "alpha"
        assert access.authorized_repo_root(repo_path) == projects.alpha
        return {"job_id": "admin-index-job", "stream_id": "10-0"}

    server._producer.submit_full_index.side_effect = publish
    server._producer.submit_incremental_index.side_effect = publish
    assert context._current_project_scope.get() is None
    if cross_root:
        with pytest.raises(HTTPException) as caught:
            await getattr(auth_router, endpoint)(1, _={"role": "admin"}, db=Database(projects.record_a))
        assert caught.value.status_code == 403
        assert seen == []
    else:
        result = await getattr(auth_router, endpoint)(1, _={"role": "admin"}, db=Database(projects.record_a))
        assert result.status == "queued"
        assert seen == [projects.scope_a]
    assert context._current_project_scope.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("route_path", ["index", "index-full"])
@pytest.mark.parametrize("role", ["admin", "developer"])
async def test_admin_index_http_routes_bind_only_after_admin_authorization(projects, monkeypatch, route_path, role):
    get_project = AsyncMock(return_value=projects.record_a)
    monkeypatch.setattr(auth_router, "_get_active_project", get_project)
    monkeypatch.setattr(auth_router, "_resolve_project_repo_path", lambda _: str(projects.alpha))
    monkeypatch.setattr(server, "_collect_git_changed_paths", AsyncMock(return_value={
        "changed_paths": ["owned.py"], "destructive_paths": [],
    }))

    async def publish(repo_path, *args, project_name):
        assert context.require_project_scope() == projects.scope_a
        assert context.authorized_graph_name(project_name) == "alpha"
        return {"job_id": "http-admin-job", "stream_id": "11-0"}

    server._producer.submit_full_index.side_effect = publish
    server._producer.submit_incremental_index.side_effect = publish
    app = FastAPI()
    app.include_router(auth_router.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: {"id": 9, "role": role}
    app.dependency_overrides[get_db] = lambda: Database(projects.record_a)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(f"/api/auth/projects/1/{route_path}")
    assert response.status_code == (200 if role == "admin" else 403)
    if role != "admin":
        get_project.assert_not_awaited()
        server._producer.submit_full_index.assert_not_awaited()
        server._producer.submit_incremental_index.assert_not_awaited()
    assert context._current_project_scope.get() is None


@pytest.mark.asyncio
async def test_relay_full_ref_rebuild_is_available_without_using_raw_graph_override(projects):
    server._producer.submit_full_index.return_value = {"job_id": "full-ref-job", "stream_id": "3-0"}
    with context.bind_project_scope(projects.scope_a):
        result = await relay.dispatch_tool("index_full", {
            "repo_path": str(projects.alpha), "ref_id": "feature/full",
        }, "alpha")
    expected = context.branch_graph_name(projects.scope_a.project_id, "feature/full")
    assert result["graph_name"] == expected
    server._producer.submit_full_index.assert_awaited_once_with(str(projects.alpha), project_name=expected)


@pytest.mark.asyncio
async def test_job_status_rejects_other_project_even_if_repo_path_is_forged(projects):
    server._producer.get_job_status.return_value = {
        "job_id": "foreign-job", "project_name": "bravo", "repo_path": str(projects.alpha), "status": "done",
    }
    with context.bind_project_scope(projects.scope_a):
        with pytest.raises(HTTPException) as caught:
            await server.get_index_job_status("foreign-job")
    assert caught.value.status_code == 403


@pytest.mark.asyncio
async def test_job_status_allows_own_ref_without_guessing_ref_from_name(projects):
    expected = {
        "job_id": "own-ref-job", "project_name": context.branch_graph_name(projects.scope_a.project_id, "feature/full"),
        "repo_path": str(projects.alpha), "status": "done", "errors": "[]",
    }
    server._producer.get_job_status.return_value = expected
    with context.bind_project_scope(projects.scope_a):
        result = await server.get_index_job_status("own-ref-job")
    assert result == expected


@pytest.mark.asyncio
async def test_job_status_keeps_retrying_active_without_reenqueue(projects, monkeypatch):
    retrying = {
        "job_id": "recovering-job", "project_name": "alpha", "repo_path": str(projects.alpha),
        "status": "retrying", "stream_id": "42-0", "recovery_action": "pending_replay",
    }
    server._producer.get_job_status.return_value = retrying
    consumer = SimpleNamespace(get_queue_snapshot=AsyncMock(return_value={
        "pending_jobs": [{"job_id": "another-job"}, retrying],
        "failed_jobs": [{"job_id": "failed-job", "repo_path": str(projects.bravo)}],
        "avg_duration_sec": 12,
    }))
    monkeypatch.setattr(server, "_consumer", consumer)
    with context.bind_project_scope(projects.scope_a):
        result = await server.get_index_job_status("recovering-job")
    assert result["status"] == "retrying"
    assert result["stream_id"] == "42-0"
    assert result["recovery_action"] == "pending_replay"
    assert result["queue_position"] == 1
    assert result["eta_seconds"] == 24
    assert "failed_jobs" not in result
    server._producer.submit_full_index.assert_not_awaited()
    server._producer.submit_incremental_index.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("ref", ["", "feature/job"])
async def test_worker_revalidates_job_root_from_db_without_request_context(projects, monkeypatch, ref):
    monkeypatch.setattr(pgshim, "get_pool", lambda: Database([projects.record_a, projects.record_b]))
    graph = context.branch_graph_name(projects.scope_a.project_id, ref) if ref else "alpha"
    assert context._current_project_scope.get() is None
    assert await access.authorized_job_repo_root(str(projects.alpha), graph) == projects.alpha
    with pytest.raises(HTTPException) as caught:
        await access.authorized_job_repo_root(str(projects.bravo), graph)
    assert caught.value.status_code == 403
    assert context._current_project_scope.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("graph", [None, "", "__cga_stage__pending", "__cga_ref_v2__unowned", "alpha__ref__feature_job"])
async def test_worker_does_not_trust_legacy_or_unregistered_graph_override(projects, monkeypatch, graph):
    monkeypatch.setattr(pgshim, "get_pool", lambda: Database([projects.record_a]))
    with pytest.raises(HTTPException) as caught:
        await access.authorized_job_repo_root(str(projects.alpha), graph)
    assert caught.value.status_code == 403


@pytest.mark.parametrize("changed", [None, [], ("owned.py", "deleted.py")])
def test_producer_validation_checks_full_and_incremental_roots(projects, changed):
    with context.bind_project_scope(projects.scope_a):
        root, files = access.authorize_project_paths(str(projects.alpha), changed, graph_name="alpha")
        assert root == projects.alpha
        expected = None if changed is None else [str(projects.alpha / path) for path in changed]
        assert files == expected
        assert_forbidden(lambda: access.authorize_project_paths(str(projects.bravo), changed, graph_name="alpha"))
        assert_forbidden(lambda: access.authorize_project_paths(str(projects.alpha), changed, graph_name="bravo"))


def test_producer_validation_requires_context_and_eagerly_rejects_whole_batch(projects):
    assert_forbidden(lambda: access.authorize_project_paths(str(projects.alpha)))
    with context.bind_project_scope(projects.scope_a):
        assert_forbidden(lambda: access.authorize_project_paths(
            str(projects.alpha), ["owned.py", "../bravo/private.py"],
        ))


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [None, [], ("owned.py", "deleted.py")])
async def test_consumer_payload_validation_is_db_bound_without_request_scope(projects, monkeypatch, changed):
    monkeypatch.setattr(pgshim, "get_pool", lambda: Database([projects.record_a]))
    root, files = await access.authorize_job_paths(str(projects.alpha), "alpha", changed)
    assert root == projects.alpha
    assert files == (None if changed is None else [str(projects.alpha / path) for path in changed])
    with pytest.raises(HTTPException) as caught:
        await access.authorize_job_paths(str(projects.bravo), "alpha", changed)
    assert caught.value.status_code == 403
    assert context._current_project_scope.get() is None


@pytest.mark.asyncio
async def test_consumer_payload_validation_rejects_escape_before_any_graph_operation(projects, isolated_services, monkeypatch):
    monkeypatch.setattr(pgshim, "get_pool", lambda: Database([projects.record_a]))
    with pytest.raises(HTTPException) as caught:
        await access.authorize_job_paths(str(projects.alpha), "alpha", ["owned.py", "../bravo/private.py"])
    assert caught.value.status_code == 403
    assert isolated_services.selected == []


@pytest.mark.asyncio
async def test_consumer_payload_validation_rejects_missing_project_for_full_index(projects, isolated_services):
    with pytest.raises(HTTPException) as caught:
        await access.authorize_job_paths(str(projects.alpha), None)
    assert caught.value.status_code == 403
    assert isolated_services.selected == []


def test_retrieval_filters_poisoned_paths_and_ignores_global_root(projects, isolated_services, monkeypatch):
    isolated_services.retrieval_rows = [
        ["owned", "function", "owned.py", 1, 2],
        ["private", "function", str(projects.bravo / "private.py"), 1, 2],
        ["escape", "function", "../bravo/private.py", 1, 2],
    ]
    monkeypatch.setattr(server, "_repo_root", projects.bravo)
    with context.bind_project_scope(projects.scope_a):
        result = server.retrieve_context("owned")
    assert len(result) == 1
    assert "alpha-only" in result[0]["snippet"]
    assert "BRAVO-DO-NOT-READ" not in json.dumps(result)


def test_strategy_keyword_fallback_checks_resolved_paths(projects, monkeypatch):
    alias = projects.alpha / "alias.py"
    alias.write_text("placeholder", encoding="utf-8")
    real_resolve = Path.resolve

    def resolve(path, *args, **kwargs):
        if path == alias:
            return projects.bravo / "private.py"
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    monkeypatch.setattr(server.runtime_config, "get_indexing_token_budget", lambda: 1800)
    with context.bind_project_scope(projects.scope_a):
        result = server.strategy_query("BRAVO")
        owned = server.strategy_query("alpha-only")
    assert result["fallback_context"] == []
    assert owned["fallback_context"][0]["file_path"] == "owned.py"
    assert "alpha-only" in owned["fallback_context"][0]["snippet"]


def test_read_cache_is_scoped_by_project_root_and_authorization_version(projects, monkeypatch):
    cache = Mock()
    cache.get.return_value = None
    monkeypatch.setattr(server, "_cache", cache)
    with context.bind_project_scope(projects.scope_a):
        server.retrieve_context("owned")
    key = cache.get.call_args.args[1]
    assert key["project_id"] == projects.scope_a.project_id
    assert key["registered_repo_path"] == str(projects.alpha)
    assert key["authorization_version"] == 2
    assert key["graph_generation"] == "generation-1"


def test_successful_commit_changes_read_cache_key_without_invalidation(projects, isolated_services, monkeypatch):
    generation = {"value": "before-commit"}
    values = {}
    cache = Mock()
    cache.get.side_effect = lambda tool, args: values.get((tool, json.dumps(args, sort_keys=True)))
    cache.set.side_effect = lambda tool, args, result: values.update({(tool, json.dumps(args, sort_keys=True)): result})
    monkeypatch.setattr(server, "_cache", cache)
    monkeypatch.setattr(
        isolated_services, "get",
        lambda name: SimpleNamespace(cache_generation=lambda: generation["value"]),
    )
    fetch = Mock(side_effect=[{"snapshot": "old"}, {"snapshot": "published"}])
    with context.bind_project_scope(projects.scope_a):
        assert server._cached_read("test", {}, fetch) == {"snapshot": "old"}
        assert server._cached_read("test", {}, fetch) == {"snapshot": "old"}
        assert fetch.call_count == 1
        generation["value"] = "after-atomic-rename"
        assert server._cached_read("test", {}, fetch) == {"snapshot": "published"}
        assert fetch.call_count == 2
    cache.invalidate_all.assert_not_called()
    assert len(values) == 2


@pytest.mark.parametrize("generation", [None, lambda: "", lambda: None])
def test_cache_is_bypassed_when_published_generation_is_unavailable(projects, isolated_services, monkeypatch, generation):
    cache = Mock()
    cache.get.return_value = {"snapshot": "stale"}
    monkeypatch.setattr(server, "_cache", cache)
    monkeypatch.setattr(isolated_services, "get", lambda name: SimpleNamespace(cache_generation=generation))
    fetch = Mock(side_effect=[{"snapshot": 1}, {"snapshot": 2}])
    with context.bind_project_scope(projects.scope_a):
        assert server._cached_read("test", {}, fetch) == {"snapshot": 1}
        assert server._cached_read("test", {}, fetch) == {"snapshot": 2}
    cache.get.assert_not_called()
    cache.set.assert_not_called()


def test_branch_names_preserve_main_but_separate_projects_and_exact_refs(projects):
    refs = ["feature/a-b", "feature/a_b", "feature/a/b", "Feature/a-b", "feature/a b", "Main"]
    with context.bind_project_scope(projects.scope_a):
        assert relay._graph_name_for_project("Alpha") == "alpha"
        assert relay._graph_name_for_project("Alpha", "main") == "alpha"
        names = [relay._graph_name_for_project("alpha", ref) for ref in refs]
    assert len(set(names)) == len(refs)
    assert all(name.startswith(context.BRANCH_GRAPH_PREFIX) for name in names)
    with context.bind_project_scope(projects.scope_b):
        assert relay._graph_name_for_project("bravo", refs[0]) not in names
    collision = access.registered_project_scope({**projects.record_b, "project_name": "alpha__ref__feature_a_b"})
    with context.bind_project_scope(collision):
        assert relay._graph_name_for_project(collision.project_name) == "alpha__ref__feature_a_b"
        assert collision.graph_name not in names


@pytest.mark.asyncio
async def test_relay_branch_enqueue_is_derived_from_bound_project(projects):
    server._producer.submit_incremental_index.return_value = {"job_id": "branch-job", "stream_id": "1-1"}
    with context.bind_project_scope(projects.scope_a):
        result = await relay.dispatch_tool(
            "index_incremental", {
                "repo_path": str(projects.alpha), "changed_paths": ["owned.py"],
                "branch": "feature/a-b", "project_name": "bravo",
            }, "alpha",
        )
        assert _current_project_name.get() == "alpha"
    assert result["graph_name"] == context.branch_graph_name(projects.scope_a.project_id, "feature/a-b")
    assert server._producer.submit_incremental_index.call_args.kwargs["project_name"] == result["graph_name"]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["fetch_minimal_code", "index_incremental", "query_impact_graph"])
async def test_ambiguous_legacy_graph_requires_admin_migration_before_read_or_write(projects, isolated_services, tool):
    isolated_services.keys.add("alpha__ref__feature_a_b")
    with context.bind_project_scope(projects.scope_a):
        with pytest.raises(HTTPException) as caught:
            await relay.dispatch_tool(tool, {
                "repo_path": str(projects.alpha), "changed_paths": ["owned.py"],
                "query": "owned", "ref_id": "feature/a-b", "fallback_ref": "main",
            }, "alpha")
    assert caught.value.status_code == 409
    assert "administrator" in str(caught.value.detail)
    assert not isolated_services.queries
    assert not isolated_services.deleted
    server._producer.submit_incremental_index.assert_not_awaited()


def test_legacy_migration_acknowledgement_is_bound_to_exact_project_and_ref(projects, isolated_services):
    ref = "feature/a-b"
    legacy = "alpha__ref__feature_a_b"
    isolated_services.keys.add(legacy)
    name = context.branch_graph_name(projects.scope_a.project_id, ref)
    marker_key = f"cga:ref-migration:v2:{name}"
    marker = {"version": 2, "project_id": projects.scope_a.project_id, "ref_id": ref, "legacy_graph_name": legacy}
    with context.bind_project_scope(projects.scope_a):
        for wrong in ({**marker, "project_id": projects.scope_b.project_id}, {**marker, "ref_id": "feature/a_b"}):
            isolated_services.markers[marker_key] = json.dumps(wrong)
            with pytest.raises(HTTPException) as caught:
                relay._require_ref_migration("alpha", ref)
            assert caught.value.status_code == 409
        isolated_services.markers[marker_key] = json.dumps(marker)
        assert relay._require_ref_migration("alpha", ref) == name
    assert not isolated_services.deleted


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [
    {"status": "queued", "errors": []},
    {"status": "failed", "errors": ["parse failed"]},
    {"status": "done", "errors": ["parse failed"]},
    {"status": "done", "errors": "['parse failed']"},
    {"status": "done"},
    {"status": "done", "errors": [], "ready": False},
    {"status": "done", "errors": [], "timeout": True},
    {"status": "not_found", "errors": []},
    {"status": "done", "errors": 0, "files": 0},
    {"status": "done", "errors": "0", "files": "0"},
    {"status": "noop", "errors": 0, "files": 0},
    {"status": "failed", "errors": 0, "files": 0, "error": "Empty repository scan would erase the existing graph"},
])
async def test_promotion_preserves_source_without_proven_success(projects, isolated_services, monkeypatch, terminal):
    source = context.branch_graph_name(projects.scope_a.project_id, "feature/promote")
    isolated_services.keys.add(source)
    full = AsyncMock(return_value={"status": "queued", "job_id": "promote-job"})
    wait = AsyncMock(return_value={**terminal, "project_name": "alpha", "repo_path": str(projects.alpha)})
    monkeypatch.setattr(server, "index_full", full)
    monkeypatch.setattr(server, "wait_for_index_ready", wait)
    with context.bind_project_scope(projects.scope_a):
        result = await relay.dispatch_tool("promote_ref", {
            "repo_path": str(projects.alpha), "ref_id": "feature/promote", "delete_ref_graph": True,
        }, "alpha")
    assert result["backend_tool"] == "index_full"
    assert result["result"]["deleted_ref_graph"] is False
    if terminal.get("timeout") or terminal["status"] in {"queued", "not_found"}:
        expected = "pending"
    elif terminal["status"] == "noop" or (terminal["status"] == "done" and terminal.get("files") in (0, "0")):
        expected = "noop"
    else:
        expected = "failed"
    assert result["result"]["status"] == expected
    assert result["result"]["reason"]
    assert not isolated_services.deleted
    full.assert_awaited_once_with(repo_path=str(projects.alpha), project_name="alpha")
    assert not isolated_services.queries


@pytest.mark.asyncio
@pytest.mark.parametrize("errors", [[], "[]", 0, "0"])
@pytest.mark.parametrize("source_changes", [False, True])
async def test_promotion_uses_full_rebuild_and_deletes_only_after_success(projects, isolated_services, monkeypatch, errors, source_changes):
    source = context.branch_graph_name(projects.scope_a.project_id, "feature/promote")
    isolated_services.keys.add(source)
    events = []

    async def full(**kwargs):
        assert _current_project_name.get() == "alpha"
        events.append("full-rebuild-queued")
        return {"status": "queued", "job_id": "promote-job"}

    async def wait(**kwargs):
        assert not isolated_services.deleted
        events.append("published-successfully")
        isolated_services.keys.add("alpha")
        isolated_services.counts["alpha"] = 1
        isolated_services.generations["alpha"] = "published-target-generation"
        if source_changes:
            isolated_services.generations[source] = "newer-source-generation"
        return {"status": "done", "errors": errors, "project_name": "alpha", "repo_path": str(projects.alpha), "files": 1}

    monkeypatch.setattr(server, "index_full", full)
    monkeypatch.setattr(server, "wait_for_index_ready", wait)
    with context.bind_project_scope(projects.scope_a):
        result = await relay._promote_ref({
            "repo_path": str(projects.alpha), "ref_id": "feature/promote", "delete_ref_graph": True,
        }, "alpha")
    assert events == ["full-rebuild-queued", "published-successfully"]
    assert result["status"] == "done"
    assert result["rebuild_mode"] == "full"
    assert isolated_services.deleted == ([] if source_changes else [source])
    assert result["deleted_ref_graph"] is not source_changes
    if source_changes:
        assert result["reason"] == "target_published_source_changed_and_retained"
    assert all(name == "alpha" and "count(f)" in query for name, query, _ in isolated_services.queries)
    assert all("RETURN f.path" not in query for _, query, _ in isolated_services.queries)


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["absent", "unpublished", "empty"])
async def test_promotion_preserves_source_when_target_publication_is_not_proven(projects, isolated_services, monkeypatch, target):
    source = context.branch_graph_name(projects.scope_a.project_id, "feature/promote")
    isolated_services.keys.add(source)
    monkeypatch.setattr(server, "index_full", AsyncMock(return_value={"status": "queued", "job_id": "promote-job"}))

    async def wait(**kwargs):
        if target != "absent":
            isolated_services.keys.add("alpha")
        if target != "unpublished":
            isolated_services.generations["alpha"] = "new-generation"
        isolated_services.counts["alpha"] = 0 if target == "empty" else 1
        return {"status": "done", "errors": 0, "files": 1, "project_name": "alpha", "repo_path": str(projects.alpha)}

    monkeypatch.setattr(server, "wait_for_index_ready", wait)
    with context.bind_project_scope(projects.scope_a):
        result = await relay._promote_ref({
            "repo_path": str(projects.alpha), "ref_id": "feature/promote", "delete_ref_graph": True,
        }, "alpha")
    assert result["status"] == "noop"
    assert result["reason"] == "target_not_published_or_empty"
    assert result["deleted_ref_graph"] is False
    assert isolated_services.deleted == []


@pytest.mark.asyncio
async def test_promotion_empty_scan_exception_returns_failed_and_retains_source(projects, isolated_services, monkeypatch):
    source = context.branch_graph_name(projects.scope_a.project_id, "feature/promote")
    isolated_services.keys.add(source)
    monkeypatch.setattr(server, "index_full", AsyncMock(side_effect=ValueError("Empty repository scan would erase the existing graph")))
    with context.bind_project_scope(projects.scope_a):
        result = await relay._promote_ref({
            "repo_path": str(projects.alpha), "ref_id": "feature/promote", "delete_ref_graph": True,
        }, "alpha")
    assert result["status"] == "failed"
    assert result["index_result"]["reason"] == "full_rebuild_rejected"
    assert result["deleted_ref_graph"] is False
    assert not isolated_services.deleted


@pytest.mark.asyncio
async def test_promotion_timeout_is_bounded_and_keeps_source(projects, isolated_services, monkeypatch):
    source = context.branch_graph_name(projects.scope_a.project_id, "feature/promote")
    isolated_services.keys.add(source)
    monkeypatch.setattr(server, "index_full", AsyncMock(return_value={"status": "queued", "job_id": "promote-job"}))
    monkeypatch.setattr(server, "wait_for_index_ready", AsyncMock(side_effect=TimeoutError))
    with context.bind_project_scope(projects.scope_a):
        result = await relay._promote_ref({
            "repo_path": str(projects.alpha), "ref_id": "feature/promote", "delete_ref_graph": True,
        }, "alpha")
    assert result["index_result"]["timeout"] is True
    assert not result["deleted_ref_graph"]
    assert not isolated_services.deleted


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [{"parent_ref": "feature/promote"}, {"delete_ref_graph": "false"}])
async def test_promotion_rejects_same_ref_or_nonboolean_delete(projects, isolated_services, invalid):
    isolated_services.keys.add(context.branch_graph_name(projects.scope_a.project_id, "feature/promote"))
    with context.bind_project_scope(projects.scope_a):
        with pytest.raises(HTTPException) as caught:
            await relay._promote_ref({
                "repo_path": str(projects.alpha), "ref_id": "feature/promote", **invalid,
            }, "alpha")
    assert caught.value.status_code == 400
    assert not isolated_services.deleted
    server._producer.submit_full_index.assert_not_awaited()


def test_fallback_does_not_bypass_legacy_migration(projects, isolated_services):
    requested = context.branch_graph_name(projects.scope_a.project_id, "feature/empty")
    isolated_services.counts[requested] = 0
    isolated_services.keys.add("alpha__ref__feature_old")
    with context.bind_project_scope(projects.scope_a):
        with pytest.raises(HTTPException) as caught:
            relay._query_graph_scope("alpha", "feature/empty", "feature/old")
    assert caught.value.status_code == 409
    assert all(name != "alpha__ref__feature_old" for name, _, _ in isolated_services.queries)


@pytest.mark.asyncio
async def test_concurrent_scopes_do_not_share_repo_or_graph_context(projects):
    async def request(scope):
        with context.bind_project_scope(scope):
            with context.bind_project_ref("feature/shared"):
                await asyncio.sleep(0)
                return server._resolve_project_name(), access.authorized_repo_root()

    left, right = await asyncio.gather(request(projects.scope_a), request(projects.scope_b))
    assert left[0] != right[0]
    assert left[1] == projects.alpha
    assert right[1] == projects.bravo
