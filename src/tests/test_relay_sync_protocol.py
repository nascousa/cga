"""HTTP durability acknowledgements must follow storage, never just auditing."""

from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from backend.cga_relay import router as relay


@pytest.fixture
def sync_api(monkeypatch):
    app = FastAPI()
    app.include_router(relay.router, prefix="/api")
    app.include_router(relay.account_router, prefix="/api")
    app.dependency_overrides[relay.get_db] = lambda: object()
    app.dependency_overrides[relay.require_crystal_suite] = lambda: None
    app.dependency_overrides[relay.get_current_user] = lambda: {"id": 47, "username": "tester"}
    context = {"project_id": "project-A", "project_db_id": 7, "project_name": "alpha"}
    monkeypatch.setattr(relay, "_project_context", lambda request: context)
    monkeypatch.setattr(relay, "_account_project_context", AsyncMock(return_value=context))
    audit = AsyncMock()
    store = AsyncMock(return_value={"accepted": True, "durable": True, "batch_id": "a" * 64})
    monkeypatch.setattr(relay, "insert_audit_log", audit)
    monkeypatch.setattr(relay, "save_sync_batch", store)
    return app, store, audit


@pytest.mark.parametrize("scope", ["project", "auth"])
async def test_sync_receipt_only_follows_committed_batch(sync_api, scope):
    app, store, audit = sync_api

    async def check_audit_order(**kwargs):
        store.assert_awaited_once()
        assert kwargs["status_code"] == 202

    audit.side_effect = check_audit_order
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/{scope}/cga-relay/sync", json={
            "project_id": "project-A", "agent_id": "relay-test", "tombstones": ["gone.py"],
        })
    assert response.status_code == 202
    assert response.headers["X-CGA-Sync-Receipt"] == "a" * 64
    assert response.json()["durable"] is True
    assert store.await_args.args[1] == 7
    assert store.await_args.args[2]["tombstones"] == ["gone.py"]


@pytest.mark.parametrize("scope", ["project", "auth"])
async def test_sync_storage_failure_never_acknowledges(sync_api, scope):
    app, store, audit = sync_api
    store.side_effect = RuntimeError("injected storage failure")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test",
    ) as client:
        response = await client.post(f"/api/{scope}/cga-relay/sync", json={
            "project_id": "project-A", "agent_id": "relay-test",
        })
    assert response.status_code >= 500
    assert "X-CGA-Sync-Receipt" not in response.headers
    audit.assert_not_awaited()


@pytest.mark.parametrize("scope", ["project", "auth"])
async def test_sync_audit_failure_does_not_undo_durable_storage(sync_api, scope):
    app, store, audit = sync_api
    audit.side_effect = RuntimeError("injected audit failure")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/{scope}/cga-relay/sync", json={
            "project_id": "project-A", "agent_id": "relay-test",
        })
    assert response.status_code == 202
    assert response.headers["X-CGA-Sync-Receipt"] == "a" * 64
    store.assert_awaited_once()


async def test_sync_project_mismatch_cannot_store(sync_api):
    app, store, _ = sync_api
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/project/cga-relay/sync", json={
            "project_id": "project-B", "agent_id": "relay-test",
        })
    assert response.status_code == 403
    store.assert_not_awaited()


async def test_sync_account_access_denied_cannot_store(sync_api, monkeypatch):
    app, store, _ = sync_api
    monkeypatch.setattr(relay, "_account_project_context", AsyncMock(side_effect=HTTPException(403)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/auth/cga-relay/sync", json={
            "project_id": "project-A", "agent_id": "relay-test",
        })
    assert response.status_code == 403
    store.assert_not_awaited()


@pytest.mark.parametrize("scope", ["project", "auth"])
async def test_sync_replay_is_project_scoped_and_not_cached(sync_api, monkeypatch, scope):
    app, _, _ = sync_api
    payload = {"snapshots": [{"path": "a.py", "content": "private-source"}], "tombstones": ["gone.py"]}
    load = AsyncMock(return_value={"batch_id": "a" * 64, "payload": payload})
    listing = AsyncMock(return_value=[{"id": 9, "batch_id": "a" * 64}])
    monkeypatch.setattr(relay, "load_sync_batch", load)
    monkeypatch.setattr(relay, "list_sync_batches", listing)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/{scope}/cga-relay/sync-batches/{'a' * 64}?project_id=project-A")
        page = await client.get(f"/api/{scope}/cga-relay/sync-batches?project_id=project-A&after_id=8&limit=1")
    assert response.status_code == 200
    assert response.json()["payload"] == payload
    assert response.headers["Cache-Control"] == "no-store"
    assert page.headers["Cache-Control"] == "no-store"
    assert page.json()["next_after_id"] == 9
    assert load.await_args.args[1:] == (7, "a" * 64)
    assert listing.await_args.args[1] == 7
    assert listing.await_args.kwargs == {"after_id": 8, "limit": 1}
    assert "private-source" not in page.text


@pytest.mark.parametrize("scope", ["project", "auth"])
async def test_sync_replay_missing_batch_does_not_disclose_other_projects(sync_api, monkeypatch, scope):
    app, _, _ = sync_api
    monkeypatch.setattr(relay, "load_sync_batch", AsyncMock(return_value=None))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/{scope}/cga-relay/sync-batches/{'a' * 64}?project_id=project-A")
    assert response.status_code == 404


@pytest.mark.parametrize("suffix", ["?limit=101", "?after_id=-1", "/invalid-id"])
async def test_sync_replay_rejects_invalid_pagination_and_ids(sync_api, monkeypatch, suffix):
    app, _, _ = sync_api
    listing = AsyncMock()
    load = AsyncMock()
    monkeypatch.setattr(relay, "load_sync_batch", load)
    monkeypatch.setattr(relay, "list_sync_batches", listing)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/project/cga-relay/sync-batches{suffix}")
    assert response.status_code == 422
    listing.assert_not_awaited()
    load.assert_not_awaited()


@pytest.mark.parametrize("scope", ["project", "auth"])
@pytest.mark.parametrize("chunked", [False, True])
async def test_sync_rejects_oversized_raw_json_before_parsing(sync_api, scope, chunked):
    app, store, _ = sync_api
    body = b" " * (relay.MAX_SYNC_BYTES + 1)

    async def chunks():
        for offset in range(0, len(body), 65536):
            yield body[offset:offset + 65536]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/api/{scope}/cga-relay/sync",
            content=chunks() if chunked else body, headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 413
    assert "X-CGA-Sync-Receipt" not in response.headers
    store.assert_not_awaited()
