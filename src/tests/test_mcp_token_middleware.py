from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager, suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from backend.auth.middleware import ProjectTokenMiddleware
from backend.auth.crystals import crystal_suite_headers
from backend.auth.security import hash_token
from backend.auth.context import require_project_scope


async def _seed_db(pool) -> None:
    async with pool.acquire() as db:
        await db.execute(
            "INSERT INTO projects(id, project_name, project_id, is_active) "
            "VALUES (?, ?, ?, 1)",
            (1, "p1", "P1"),
        )
        await db.execute(
            "INSERT INTO projects(id, project_name, project_id, is_active) "
            "VALUES (?, ?, ?, 1)",
            (2, "p2", "P2"),
        )
        await db.execute(
            "INSERT INTO project_tokens(project_id, token_type, token_hash, "
            "token_hint, is_active) VALUES (?, ?, ?, ?, 1)",
            (1, "mcp", hash_token("good-token"), "goodtok..."),
        )
        await db.execute(
            "INSERT INTO project_tokens(project_id, token_type, token_hash, "
            "token_hint, is_active) VALUES (?, ?, ?, ?, 1)",
            (1, "legacy", hash_token("legacy-token"), "legacy..."),
        )


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(ProjectTokenMiddleware)

    @app.get("/mcp")
    async def mcp_discovery() -> dict:
        return {"transport": "sse", "sse_endpoint": "/mcp/sse"}

    @app.get("/mcp/ping")
    async def ping() -> dict:
        return {"ok": True}

    @app.get("/api/project/ping")
    async def project_ping(request: Request) -> dict:
        return {
            "ok": True,
            "project_id": getattr(request.state, "project_id", None),
            "project_name": getattr(request.state, "project_name", None),
            "project_token_type": getattr(request.state, "project_token_type", None),
        }

    return app


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _crystal_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = crystal_suite_headers()
    if extra:
        headers.update(extra)
    return headers


@pytest.mark.asyncio
async def test_mcp_discovery_is_public(auth_pg_pool):
    await _seed_db(auth_pg_pool)
    async with _client(_build_app()) as client:
        resp = await client.get("/mcp")

    assert resp.status_code == 200
    assert resp.json() == {"transport": "sse", "sse_endpoint": "/mcp/sse"}


@pytest.mark.asyncio
async def test_mcp_rejects_missing_project_id(auth_pg_pool):
    await _seed_db(auth_pg_pool)
    async with _client(_build_app()) as client:
        resp = await client.get(
            "/mcp/ping", headers=_crystal_headers({"Authorization": "Bearer good-token"})
        )

    assert resp.status_code == 401
    assert "Missing project_id" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_mcp_rejects_missing_crystals_profile(auth_pg_pool):
    await _seed_db(auth_pg_pool)
    async with _client(_build_app()) as client:
        resp = await client.get(
            "/mcp/ping",
            headers={
                "Authorization": "Bearer good-token",
                "X-Project-ID": "P1",
            },
        )

    assert resp.status_code == 426
    assert "CRYSTALS" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_mcp_rejects_non_mcp_token_type(auth_pg_pool):
    await _seed_db(auth_pg_pool)
    async with _client(_build_app()) as client:
        resp = await client.get(
            "/mcp/ping",
            headers=_crystal_headers({
                "Authorization": "Bearer legacy-token",
                "X-Project-ID": "P1",
            }),
        )

    assert resp.status_code == 403
    assert resp.json()["detail"] == "Token type not allowed for MCP endpoint"


@pytest.mark.asyncio
async def test_mcp_rejects_project_mismatch(auth_pg_pool):
    await _seed_db(auth_pg_pool)
    async with _client(_build_app()) as client:
        resp = await client.get(
            "/mcp/ping",
            headers=_crystal_headers({
                "Authorization": "Bearer good-token",
                "X-Project-ID": "P2",
            }),
        )

    assert resp.status_code == 403
    assert resp.json()["detail"] == "Token is not valid for this project_id"


@pytest.mark.asyncio
async def test_mcp_accepts_matching_mcp_token(auth_pg_pool):
    await _seed_db(auth_pg_pool)
    async with _client(_build_app()) as client:
        resp = await client.get(
            "/mcp/ping",
            headers=_crystal_headers({
                "Authorization": "Bearer good-token",
                "X-Project-ID": "P1",
            }),
        )

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


@pytest.mark.asyncio
async def test_project_api_accepts_mcp_token_without_explicit_project_id(
    auth_pg_pool,
):
    await _seed_db(auth_pg_pool)
    async with _client(_build_app()) as client:
        resp = await client.get(
            "/api/project/ping",
            headers=_crystal_headers({"Authorization": "Bearer good-token"}),
        )

    assert resp.status_code == 200
    assert resp.json() == {
        "ok": True,
        "project_id": "P1",
        "project_name": "p1",
        "project_token_type": "mcp",
    }


@asynccontextmanager
async def _sse_session(app, headers):
    received = asyncio.Queue()
    sent = asyncio.Queue()
    await received.put({"type": "http.request", "body": b"", "more_body": False})

    async def send(message):
        await sent.put(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "scheme": "http", "method": "GET", "path": "/mcp/sse",
        "raw_path": b"/mcp/sse", "query_string": b"", "root_path": "",
        "server": ("127.0.0.1", 18001), "client": ("127.0.0.1", 12345),
        "headers": [
            (b"host", b"127.0.0.1:18001"), (b"accept", b"text/event-stream"),
            *((name.lower().encode(), value.encode()) for name, value in headers.items()),
        ],
    }

    async def event():
        while True:
            response = await asyncio.wait_for(sent.get(), timeout=5)
            if response["type"] == "http.response.start":
                assert response["status"] == 200
            elif response["type"] == "http.response.body":
                lines = response.get("body", b"").decode().splitlines()
                data = "\n".join(line[6:] for line in lines if line.startswith("data: "))
                if data:
                    return data

    running = asyncio.create_task(app(scope, received.get, send))
    try:
        endpoint = await event()
        yield SimpleNamespace(endpoint=endpoint, event=event)
    finally:
        running.cancel()
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(running, timeout=5)


@pytest.fixture
def mcp_session_api(tmp_path, monkeypatch):
    from backend.auth import middleware
    import backend.main as main
    from backend.tools import server
    from backend import runtime_config

    credentials = {
        "alpha-token": (1, 1, "alpha"),
        "alpha-other-token": (2, 1, "alpha"),
        "bravo-token": (3, 2, "bravo"),
    }
    rows = {
        hash_token(token): {
            "id": token_id, "project_id": project_id, "token_type": "mcp",
            "project_external_id": name, "project_name": name,
            "repo_path": str(tmp_path),
        }
        for token, (token_id, project_id, name) in credentials.items()
    }

    @asynccontextmanager
    async def execute(_sql, params):
        row = rows.get(params[0])

        async def fetchone():
            return row

        yield SimpleNamespace(fetchone=fetchone)

    @asynccontextmanager
    async def acquire():
        yield SimpleNamespace(execute=execute)

    monkeypatch.setattr(middleware.pgshim, "get_pool", lambda: SimpleNamespace(acquire=acquire))
    monkeypatch.setattr(main, "insert_audit_log", AsyncMock())
    monkeypatch.setattr(runtime_config, "RUNTIME_CONFIG_PATH", tmp_path / "runtime-config.json")
    selected = []

    def graph(name):
        def query(*args, **kwargs):
            scope = require_project_scope()
            selected.append(scope.project_id)
            assert scope.project_name == name
            return SimpleNamespace(result_set=[[111 if name == "alpha" else 222]])

        return SimpleNamespace(query=query, cache_generation=lambda: "test-generation")

    monkeypatch.setattr(server, "_registry", SimpleNamespace(get=graph))
    monkeypatch.setattr(server, "_graph", server._GraphProxy())
    monkeypatch.setattr(server, "_cache", None)
    monkeypatch.setattr(server, "_recorder", None)
    written = []

    async def record(payload):
        written.append((require_project_scope().project_id, payload["project_id"]))
        return SimpleNamespace(operation="inserted", activity=SimpleNamespace(
            to_dict=lambda: {"project_id": payload["project_id"]},
        ))

    monkeypatch.setattr(server, "_work_briefing_service", SimpleNamespace(record_activity=record))
    return SimpleNamespace(app=main.app, selected=selected, written=written, rows=rows)


async def _initialize_sse(client, session, headers):
    response = await client.post(session.endpoint, headers=headers, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "isolation-test", "version": "1"},
        },
    })
    assert response.status_code == 202
    assert json.loads(await session.event())["id"] == 1
    response = await client.post(session.endpoint, headers=headers, json={
        "jsonrpc": "2.0", "method": "notifications/initialized",
    })
    assert response.status_code == 202


@pytest.mark.asyncio
@pytest.mark.parametrize("attacker_token,attacker_project", [
    ("bravo-token", "bravo"), ("alpha-other-token", "alpha"),
])
async def test_mcp_sse_rejects_other_project_or_credential_before_tool_execution(
    mcp_session_api, attacker_token, attacker_project,
):
    runtime = mcp_session_api
    owner = _crystal_headers({"Authorization": "Bearer alpha-token", "X-Project-ID": "alpha"})
    attacker = _crystal_headers({
        "Authorization": f"Bearer {attacker_token}", "X-Project-ID": attacker_project,
    })
    async with AsyncClient(transport=ASGITransport(app=runtime.app), base_url="http://127.0.0.1:18001") as client:
        async with _sse_session(runtime.app, owner) as session:
            await _initialize_sse(client, session, owner)
            for name, arguments in (
                ("get_stats", {}),
                ("workassist_record_activity", {"event_type": "review", "title": "test-only-canary"}),
            ):
                response = await client.post(session.endpoint, headers=attacker, json={
                    "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                })
                assert response.status_code == 404
            assert runtime.selected == []
            assert runtime.written == []

            response = await client.post(session.endpoint, headers=owner, json={
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "get_stats", "arguments": {}},
            })
            assert response.status_code == 202
            result = json.loads(await session.event())
            assert result["id"] == 3 and not result["result"]["isError"]
            assert runtime.selected and set(runtime.selected) == {"alpha"}

            response = await client.post(session.endpoint, headers=owner, json={
                "jsonrpc": "2.0", "id": 4, "method": "tools/call",
                "params": {"name": "workassist_record_activity", "arguments": {
                    "event_type": "review", "title": "test-only-canary",
                }},
            })
            assert response.status_code == 202
            result = json.loads(await session.event())
            assert result["id"] == 4 and not result["result"]["isError"]
            assert runtime.written == [("alpha", "alpha")]
        response = await client.post(session.endpoint, headers=owner, json={
            "jsonrpc": "2.0", "id": 5, "method": "ping",
        })
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_mcp_sse_sessions_keep_their_own_project_and_revalidate_tokens(mcp_session_api):
    runtime = mcp_session_api
    alpha = _crystal_headers({"Authorization": "Bearer alpha-token", "X-Project-ID": "alpha"})
    bravo = _crystal_headers({"Authorization": "Bearer bravo-token", "X-Project-ID": "bravo"})
    async with AsyncClient(transport=ASGITransport(app=runtime.app), base_url="http://127.0.0.1:18001") as client:
        async with _sse_session(runtime.app, alpha) as a, _sse_session(runtime.app, bravo) as b:
            for session, headers, expected in ((a, alpha, "alpha"), (b, bravo, "bravo")):
                await _initialize_sse(client, session, headers)
                runtime.selected.clear()
                response = await client.post(session.endpoint, headers=headers, json={
                    "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "get_stats", "arguments": {}},
                })
                assert response.status_code == 202
                result = json.loads(await session.event())
                assert not result["result"]["isError"]
                assert runtime.selected and set(runtime.selected) == {expected}
            runtime.rows.pop(hash_token("alpha-token"))
            response = await client.post(a.endpoint, headers=alpha, json={
                "jsonrpc": "2.0", "id": 3, "method": "ping",
            })
            assert response.status_code == 401
