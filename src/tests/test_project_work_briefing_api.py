from __future__ import annotations

from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request
from starlette.responses import Response

import backend.main as main_module
from backend.auth.crystals import crystal_suite_headers
from backend.auth.security import hash_token
from backend.main import app
from backend.workbriefing.models import AgentLearningRequest
from backend.workbriefing.service import WorkBriefingService
from backend.workbriefing.store import PgVectorActivityStore


async def _seed_project_auth(pool) -> None:
    async with pool.acquire() as db:
        await db.execute(
            "INSERT INTO projects(id, project_name, project_id, upstream_url, "
            "description, repo_path, is_active) VALUES (?, ?, ?, ?, ?, ?, 1)",
            (
                1,
                "contextgraphagent",
                "CGA123",
                "http://localhost:18001",
                "CGA host project",
                "D:/Repos/Context Graph Agent",
            ),
        )
        await db.execute(
            "INSERT INTO project_tokens(project_id, token_type, token_hash, "
            "token_hint, version, is_active) VALUES (?, ?, ?, ?, ?, 1)",
            (1, "mcp", hash_token("mcp-token"), "mcp-tokn", 1),
        )


def _install_service(monkeypatch, pg_store: PgVectorActivityStore) -> None:
    service = WorkBriefingService(store=pg_store)
    monkeypatch.setattr(main_module, "_work_briefing_service", service)


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _project_headers() -> dict[str, str]:
    headers = crystal_suite_headers()
    headers["Authorization"] = "Bearer mcp-token"
    return headers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "expected_placeholder"),
    [
        ("/api/project/work-briefing/learning", "<work-briefing-payload-omitted>"),
        ("/api/project/work-briefing/activity", "<work-briefing-payload-omitted>"),
        ("/api/project/work-briefing/learning/", "<work-briefing-payload-omitted>"),
        ("/mcp/messages", "<mcp-request-body-omitted>"),
    ],
)
async def test_audit_log_omits_work_briefing_and_mcp_payload_text(
    monkeypatch, path: str, expected_placeholder: str
) -> None:
    audit_entries: list[dict] = []

    async def capture_audit(**kwargs):
        audit_entries.append(kwargs)

    monkeypatch.setattr(main_module, "insert_audit_log", capture_audit)
    body = b'{"lesson":"token=sensitive-marker-1234567890","metadata":{"note":"sensitive-marker-1234567890"}}'
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 12345),
        "server": ("test", 80),
        "state": {},
    }
    request_body_sent = False

    async def receive():
        nonlocal request_body_sent
        if request_body_sent:
            return {"type": "http.disconnect"}
        request_body_sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request(scope, receive)

    async def call_next(_request):
        return Response(status_code=400)

    await main_module.audit_request_middleware(request, call_next)

    assert len(audit_entries) == 1
    assert audit_entries[0]["request_body"] == expected_placeholder
    assert "sensitive-marker-1234567890" not in str(audit_entries[0])


@pytest.mark.asyncio
async def test_project_learning_ingest_passes_only_authenticated_scope(monkeypatch) -> None:
    class FakeWorkBriefingService:
        def __init__(self) -> None:
            self.kwargs: dict | None = None

        async def record_learning(self, **kwargs):
            self.kwargs = kwargs
            return SimpleNamespace(
                operation="created",
                activity=SimpleNamespace(to_dict=lambda: {"project_id": kwargs["project_id"]}),
            )

        async def get_learning_context(self, *, project_id: str, limit: int) -> dict:
            return {"project_id": project_id, "limit": limit, "learnings": []}

    service = FakeWorkBriefingService()
    monkeypatch.setattr(main_module, "_work_briefing_service", service)
    monkeypatch.setattr(
        main_module,
        "_require_project_token_context",
        lambda request: {"project_id": "CGA123", "project_name": "contextgraphagent"},
    )
    request = Request({"type": "http"})

    result = await main_module.api_project_work_briefing_record_learning(
        AgentLearningRequest(
            title="Learning",
            lesson="Keep the validation command with the outcome.",
            category="successful_pattern",
            outcome="success",
            harness="aider",
            task_id="TASK-2",
        ),
        request,
    )

    assert result == {"operation": "created", "activity": {"project_id": "CGA123"}}
    assert service.kwargs == {
        "project_id": "CGA123",
        "title": "Learning",
        "lesson": "Keep the validation command with the outcome.",
        "category": "successful_pattern",
        "outcome": "success",
        "external_id": None,
        "harness": "aider",
        "task_id": "TASK-2",
        "session_id": None,
        "evidence_activity_ids": None,
    }
    assert await main_module.api_project_work_briefing_learning_context(
        request,
        limit=50,
    ) == {"project_id": "CGA123", "limit": 50, "learnings": []}


@pytest.mark.asyncio
async def test_project_api_records_activity_with_mcp_token(
    monkeypatch, auth_pg_pool, pg_activity_store
) -> None:
    await _seed_project_auth(auth_pg_pool)
    _install_service(monkeypatch, pg_activity_store)

    async with _client() as client:
        response = await client.post(
            "/api/project/work-briefing/activity",
            headers=_project_headers(),
            json={
                "event_type": "sync",
                "title": "Synced project status",
                "summary": "pushed current repo progress to CGA",
                "status": "done",
                "tags": ["wa", "sync"],
            },
        )

    assert response.status_code == 201
    payload = response.json()
    assert payload["operation"] == "created"
    assert payload["activity"]["project_id"] == "CGA123"
    assert payload["activity"]["workspace_name"] == "contextgraphagent"
    assert payload["activity"]["status"] == "done"


@pytest.mark.asyncio
async def test_project_activity_api_rejects_learning_and_reserved_identity(
    monkeypatch, auth_pg_pool, pg_activity_store
) -> None:
    await _seed_project_auth(auth_pg_pool)
    _install_service(monkeypatch, pg_activity_store)

    async with _client() as client:
        response = await client.post(
            "/api/project/work-briefing/activity",
            headers=_project_headers(),
            json={
                "event_type": "agent_learning",
                "external_id": "learning:existing",
                "title": "Override candidate",
                "summary": "Attempt to mark a candidate approved.",
                "status": "approved",
                "metadata": {"learning": {"lifecycle": "approved"}},
            },
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "learning records must use the dedicated learning endpoint"
    assert await pg_activity_store.list_recent(project_id="CGA123", event_type="agent_learning") == []


@pytest.mark.asyncio
async def test_project_api_rejects_body_project_spoof(
    monkeypatch, auth_pg_pool, pg_activity_store
) -> None:
    await _seed_project_auth(auth_pg_pool)
    _install_service(monkeypatch, pg_activity_store)

    async with _client() as client:
        response = await client.post(
            "/api/project/work-briefing/activity",
            headers=_project_headers(),
            json={
                "project_id": "WA999",
                "event_type": "sync",
                "title": "Attempted spoof",
            },
        )

    assert response.status_code == 403
    assert response.json()["detail"] == "project_id must match the authenticated project"


@pytest.mark.asyncio
async def test_project_api_summary_uses_authenticated_project_scope(
    monkeypatch, auth_pg_pool, pg_activity_store
) -> None:
    await _seed_project_auth(auth_pg_pool)
    _install_service(monkeypatch, pg_activity_store)

    async with _client() as client:
        create_response = await client.post(
            "/api/project/work-briefing/activity",
            headers=_project_headers(),
            json={
                "event_type": "review",
                "title": "Reviewed integration slice",
                "summary": "validated project-scoped ingestion",
            },
        )
        assert create_response.status_code == 201

        response = await client.get(
            "/api/project/work-briefing?limit=10",
            headers=_project_headers(),
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["project_id"] == "CGA123"
    assert payload["total_events"] == 1
    assert payload["project_counts"] == {"CGA123": 1}


@pytest.mark.asyncio
async def test_project_api_records_activity_with_ado_pbi_and_pr_refs(
    monkeypatch, auth_pg_pool, pg_activity_store
) -> None:
    """Optional but recommended: cite the ADO PBI and the repo PR via external_id /
    source_url / metadata so downstream briefings can backtrack to the originating
    work item and code change."""
    await _seed_project_auth(auth_pg_pool)
    _install_service(monkeypatch, pg_activity_store)

    async with _client() as client:
        response = await client.post(
            "/api/project/work-briefing/activity",
            headers=_project_headers(),
            json={
                "event_type": "checkin",
                "title": "Implemented work-briefing PBI/PR linkage example",
                "summary": "Added a test that demonstrates citing ADO PBI 123456 and GitHub PR #789.",
                "status": "done",
                "tags": ["wa", "checkin", "pbi:123456", "pr:789"],
                "external_id": "ado:pbi:123456",
                "source_url": "https://dev.azure.com/contoso/CGA/_workitems/edit/123456",
                "metadata": {
                    "ado": {
                        "organization": "contoso",
                        "project": "CGA",
                        "work_item_type": "Product Backlog Item",
                        "pbi_id": 123456,
                        "url": "https://dev.azure.com/contoso/CGA/_workitems/edit/123456",
                    },
                    "pr": {
                        "repo": "nascousa/cga",
                        "number": 789,
                        "url": "https://github.com/nascousa/cga/pull/789",
                        "branch": "feature/workbriefing-pbi-pr-example",
                        "commit": "abc1234",
                    },
                },
            },
        )

    assert response.status_code == 201
    payload = response.json()
    assert payload["operation"] == "created"
    activity = payload["activity"]
    assert activity["project_id"] == "CGA123"
    assert activity["source_url"].endswith("/_workitems/edit/123456")
    assert activity["source_item_id"] == "ado:pbi:123456"
    assert activity["raw_metadata"]["ado"]["pbi_id"] == 123456
    assert activity["raw_metadata"]["pr"]["number"] == 789
    assert "pbi:123456" in activity["tags"]
    assert "pr:789" in activity["tags"]
