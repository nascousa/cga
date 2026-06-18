from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

import backend.main as main_module
from backend.auth.dependencies import require_admin
from backend.ai_first.service import import_github_signals
from backend.main import app
from backend.workbriefing.service import WorkBriefingService


class FakeGraph:
    def __init__(self, counts: dict[str, int]) -> None:
        self.counts = counts

    def query(self, cypher: str):
        if ":Repository" in cypher:
            value = self.counts.get("repositories", 0)
        elif ":File" in cypher:
            value = self.counts.get("files", 0)
        elif ":Symbol" in cypher:
            value = self.counts.get("symbols", 0)
        elif ":Variable" in cypher:
            value = self.counts.get("variables", 0)
        elif ":CALLS" in cypher:
            value = self.counts.get("call_edges", 0)
        elif ":IMPORTS" in cypher:
            value = self.counts.get("import_edges", 0)
        elif ":FLOWS_TO" in cypher:
            value = self.counts.get("flow_edges", 0)
        elif ":DEFINES" in cypher:
            value = self.counts.get("defines_edges", 0)
        elif ":CONTAINS" in cypher:
            value = self.counts.get("contains_edges", 0)
        else:
            value = 0
        return SimpleNamespace(result_set=[[value]])


class FakeRegistry:
    def __init__(self, graph: FakeGraph) -> None:
        self.graph = graph

    def get(self, project_name: str):
        return self.graph


async def _seed_project(db, *, repo_path: str, project_id: int = 1, external_id: str = "CGA123") -> None:
    await db.execute(
        "INSERT INTO projects(id, project_name, project_id, repo_path, is_active) VALUES (?, ?, ?, ?, 1)",
        (project_id, "cga", external_id, repo_path),
    )
    await db.execute(
        "INSERT INTO project_tokens(id, project_id, token_type, token_hash, token_hint, version, is_active) VALUES (?, ?, ?, ?, ?, ?, 1)",
        (55, project_id, "mcp", "hash", "hint", 1),
    )
    await db.commit()


def _make_ready_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for relative in [
        ".adc/index.md",
        ".adc/prompt-rules.md",
        ".adc/planning/status.md",
        ".adc/knowledge/known-issues.md",
        ".adc/knowledge/glossary.md",
    ]:
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ready\n", encoding="utf-8")
    (repo / "src" / "tests").mkdir(parents=True)
    (repo / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    workflows = repo / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "policy-ci.yml").write_text("name: policy\n", encoding="utf-8")
    return repo


@pytest.mark.asyncio
async def test_ai_first_readiness_endpoint_returns_project_snapshot(auth_pg_pool, pg_activity_store, tmp_path, monkeypatch) -> None:
    repo = _make_ready_repo(tmp_path)
    service = WorkBriefingService(store=pg_activity_store)
    await service.record_activity({
        "project_id": "CGA123",
        "event_type": "workflow_complete",
        "title": "Completed AI-first pilot task",
        "status": "done",
        "tags": ["ai-first", "execute"],
    })

    consumer = AsyncMock()
    consumer.get_jobs_by_repo.return_value = [
        {
            "job_id": "job-1",
            "job_type": "index_full",
            "repo_path": str(repo),
            "status": "done",
            "created_at": "2026-06-18T01:00:00+00:00",
            "updated_at": "2026-06-18T01:01:00+00:00",
        }
    ]
    registry = FakeRegistry(FakeGraph({"repositories": 1, "files": 12, "symbols": 40, "call_edges": 80}))

    async with auth_pg_pool.acquire() as db:
        await _seed_project(db, repo_path=str(repo))

    monkeypatch.setattr(main_module, "_work_briefing_service", service)
    monkeypatch.setattr(main_module, "_consumer", consumer)
    monkeypatch.setattr(main_module, "_registry", registry)
    app.dependency_overrides[require_admin] = lambda: {"id": 1, "role": "admin", "username": "admin"}
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/admin/ai-first/readiness?project_id=CGA123")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    snapshot = payload["projects"][0]
    assert snapshot["project"]["project_id"] == "CGA123"
    assert snapshot["overall_score"] >= 50
    dimensions = {item["key"]: item for item in snapshot["dimensions"]}
    assert dimensions["context"]["status"] == "ok"
    assert dimensions["verification"]["status"] == "ok"
    assert any("context-quality benchmark" in action for action in snapshot["recommended_next_actions"])


@pytest.mark.asyncio
async def test_ai_first_evidence_endpoint_exports_sanitized_markdown(auth_pg_pool, pg_activity_store, tmp_path, monkeypatch) -> None:
    repo = _make_ready_repo(tmp_path)
    service = WorkBriefingService(store=pg_activity_store)
    await service.record_activity({
        "project_id": "CGA123",
        "event_type": "review",
        "title": "Reviewed evidence pack",
        "summary": "Validated context and tests.",
        "status": "done",
        "metadata": {"token": "should-not-export", "safeKey": "visible-key-only"},
    })
    consumer = AsyncMock()
    consumer.get_jobs_by_repo.return_value = []
    registry = FakeRegistry(FakeGraph({"repositories": 1, "files": 3, "symbols": 9}))

    async with auth_pg_pool.acquire() as db:
        await _seed_project(db, repo_path=str(repo))

    monkeypatch.setattr(main_module, "_work_briefing_service", service)
    monkeypatch.setattr(main_module, "_consumer", consumer)
    monkeypatch.setattr(main_module, "_registry", registry)
    app.dependency_overrides[require_admin] = lambda: {"id": 1, "role": "admin", "username": "admin"}
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/admin/ai-first/evidence?project_id=CGA123&limit=5")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "ai-first-evidence-pack.v0"
    assert payload["policy_profile"]["enforcement_level"] == "L0"
    assert "Reviewed evidence pack" in payload["markdown"]
    assert "should-not-export" not in payload["markdown"]
    assert payload["activity_evidence"]["activities"][0]["metadata_keys"] == ["safeKey", "token"]


@pytest.mark.asyncio
async def test_ai_first_evidence_endpoint_filters_by_task_id(auth_pg_pool, pg_activity_store, tmp_path, monkeypatch) -> None:
    repo = _make_ready_repo(tmp_path)
    service = WorkBriefingService(store=pg_activity_store)
    await service.record_activity({
        "project_id": "CGA123",
        "event_type": "code_change",
        "external_id": "task-123-change",
        "title": "Implemented task-bound evidence",
        "summary": "Matched task activity.",
        "status": "done",
        "tags": ["ai-first", "task-123"],
        "metadata": {"task_id": "TASK-123", "pr_id": "42"},
    })
    await service.record_activity({
        "project_id": "CGA123",
        "event_type": "review",
        "external_id": "task-999-review",
        "title": "Reviewed unrelated work",
        "summary": "Should not appear in task filtered evidence.",
        "status": "done",
        "metadata": {"task_id": "TASK-999"},
    })
    consumer = AsyncMock()
    consumer.get_jobs_by_repo.return_value = []
    registry = FakeRegistry(FakeGraph({"repositories": 1, "files": 3, "symbols": 9}))

    async with auth_pg_pool.acquire() as db:
        await _seed_project(db, repo_path=str(repo))

    monkeypatch.setattr(main_module, "_work_briefing_service", service)
    monkeypatch.setattr(main_module, "_consumer", consumer)
    monkeypatch.setattr(main_module, "_registry", registry)
    app.dependency_overrides[require_admin] = lambda: {"id": 1, "role": "admin", "username": "admin"}
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            signal_response = await client.post(
                "/api/admin/ai-first/signals",
                json={
                    "project_id": "CGA123",
                    "signal_type": "benchmark",
                    "name": "Task HPS",
                    "status": "ok",
                    "value": "13.34",
                    "unit": "%",
                    "metadata": {"task_id": "TASK-123"},
                },
            )
            assert signal_response.status_code == 201
            response = await client.get("/api/admin/ai-first/evidence?project_id=CGA123&task_id=TASK-123&limit=10")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["correlation"]["mode"] == "task_bound"
    assert payload["correlation"]["filters"] == {"task_id": "TASK-123"}
    assert payload["activity_evidence"]["count"] == 1
    assert payload["activity_evidence"]["activities"][0]["source_item_id"] == "task-123-change"
    assert payload["signal_evidence"]["count"] == 1
    assert payload["signal_evidence"]["signals"][0]["name"] == "Task HPS"
    assert "Implemented task-bound evidence" in payload["markdown"]
    assert "Task HPS" in payload["markdown"]
    assert "Reviewed unrelated work" not in payload["markdown"]


@pytest.mark.asyncio
async def test_import_github_signals_records_ci_and_pr(auth_pg_pool, tmp_path) -> None:
    repo = _make_ready_repo(tmp_path)

    async def fake_get_json(url: str, headers: dict) -> object:
        assert headers["Accept"] == "application/vnd.github+json"
        if "/actions/runs" in url:
            return {
                "workflow_runs": [
                    {
                        "id": 101,
                        "workflow_id": 9,
                        "name": "Policy CI",
                        "run_number": 62,
                        "status": "completed",
                        "conclusion": "success",
                        "html_url": "https://github.com/nascousa/cga/actions/runs/101",
                        "updated_at": "2026-06-18T02:00:00Z",
                        "head_branch": "dev/ai-first",
                        "head_sha": "abc123",
                        "event": "push",
                    }
                ]
            }
        if "/pulls" in url:
            return [
                {
                    "number": 42,
                    "title": "Add AI-first signals",
                    "state": "closed",
                    "merged_at": "2026-06-18T02:30:00Z",
                    "html_url": "https://github.com/nascousa/cga/pull/42",
                    "updated_at": "2026-06-18T02:31:00Z",
                    "user": {"login": "nasco"},
                }
            ]
        raise AssertionError(f"unexpected url: {url}")

    async with auth_pg_pool.acquire() as db:
        await _seed_project(db, repo_path=str(repo))
        result = await import_github_signals(
            db=db,
            project_id="CGA123",
            repo_url="https://github.com/nascousa/cga",
            http_get_json=fake_get_json,
            created_by="admin",
        )

    assert result["repository"] == "nascousa/cga"
    assert result["imported_count"] == 2
    assert [signal["signal_type"] for signal in result["signals"]] == ["ci", "pr"]
    assert result["signals"][0]["status"] == "ok"
    assert result["signals"][1]["status"] == "merged"


@pytest.mark.asyncio
async def test_ai_first_evidence_pack_can_be_saved_listed_and_loaded(auth_pg_pool, pg_activity_store, tmp_path, monkeypatch) -> None:
    repo = _make_ready_repo(tmp_path)
    service = WorkBriefingService(store=pg_activity_store)
    await service.record_activity({
        "project_id": "CGA123",
        "event_type": "validation",
        "external_id": "task-777-validation",
        "title": "Validated persisted evidence",
        "summary": "Persistence task completed.",
        "status": "done",
        "metadata": {"task_id": "TASK-777"},
    })
    consumer = AsyncMock()
    consumer.get_jobs_by_repo.return_value = []
    registry = FakeRegistry(FakeGraph({"repositories": 1, "files": 3, "symbols": 9}))

    async with auth_pg_pool.acquire() as db:
        await _seed_project(db, repo_path=str(repo))

    monkeypatch.setattr(main_module, "_work_briefing_service", service)
    monkeypatch.setattr(main_module, "_consumer", consumer)
    monkeypatch.setattr(main_module, "_registry", registry)
    app.dependency_overrides[require_admin] = lambda: {"id": 1, "role": "admin", "username": "admin"}
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            save_response = await client.post(
                "/api/admin/ai-first/evidence-packs",
                json={"project_id": "CGA123", "task_id": "TASK-777", "limit": 10},
            )
            assert save_response.status_code == 201
            saved_payload = save_response.json()
            evidence_id = saved_payload["saved"]["evidence_id"]

            list_response = await client.get("/api/admin/ai-first/evidence-packs?project_id=CGA123&limit=10")
            get_response = await client.get(f"/api/admin/ai-first/evidence-packs/{evidence_id}")
    finally:
        app.dependency_overrides.clear()

    assert evidence_id.startswith("ev-")
    assert saved_payload["saved"]["correlation"]["filters"] == {"task_id": "TASK-777"}
    assert "Validated persisted evidence" in saved_payload["evidence"]["markdown"]

    assert list_response.status_code == 200
    listed = list_response.json()["evidence_packs"]
    assert len(listed) == 1
    assert listed[0]["evidence_id"] == evidence_id
    assert listed[0]["project_id"] == "CGA123"

    assert get_response.status_code == 200
    loaded = get_response.json()
    assert loaded["evidence_id"] == evidence_id
    assert loaded["markdown"] == saved_payload["evidence"]["markdown"]
    assert loaded["evidence"]["correlation"]["mode"] == "task_bound"


@pytest.mark.asyncio
async def test_ai_first_policy_profile_update_affects_readiness_and_evidence(auth_pg_pool, pg_activity_store, tmp_path, monkeypatch) -> None:
    repo = _make_ready_repo(tmp_path)
    service = WorkBriefingService(store=pg_activity_store)
    await service.record_activity({
        "project_id": "CGA123",
        "event_type": "policy",
        "title": "Configured team policy",
        "status": "done",
    })
    consumer = AsyncMock()
    consumer.get_jobs_by_repo.return_value = []
    registry = FakeRegistry(FakeGraph({"repositories": 1, "files": 3, "symbols": 9}))

    async with auth_pg_pool.acquire() as db:
        await _seed_project(db, repo_path=str(repo))

    monkeypatch.setattr(main_module, "_work_briefing_service", service)
    monkeypatch.setattr(main_module, "_consumer", consumer)
    monkeypatch.setattr(main_module, "_registry", registry)
    app.dependency_overrides[require_admin] = lambda: {"id": 1, "role": "admin", "username": "admin"}
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            update_response = await client.patch(
                "/api/admin/ai-first/policy-profiles",
                json={"project_id": "CGA123", "profile_name": "team-default", "notes": "pilot team"},
            )
            list_response = await client.get("/api/admin/ai-first/policy-profiles?project_id=CGA123")
            readiness_response = await client.get("/api/admin/ai-first/readiness?project_id=CGA123")
            evidence_response = await client.get("/api/admin/ai-first/evidence?project_id=CGA123&limit=5")
    finally:
        app.dependency_overrides.clear()

    assert update_response.status_code == 200
    assert update_response.json()["profile"]["name"] == "team-default"
    assert update_response.json()["profile"]["enforcement_level"] == "L1"

    assert list_response.status_code == 200
    profile = list_response.json()["profiles"][0]
    assert profile["name"] == "team-default"
    assert profile["notes"] == "pilot team"

    readiness = readiness_response.json()["projects"][0]
    assert readiness["policy_profile"]["name"] == "team-default"
    governance = next(item for item in readiness["dimensions"] if item["key"] == "governance")
    policy_signal = next(item for item in governance["signals"] if item["key"] == "policy_profile")
    assert policy_signal["status"] == "ok"

    evidence = evidence_response.json()
    assert evidence["policy_profile"]["name"] == "team-default"
    assert evidence["policy_profile"]["tool_policy"]["write_tools"] == "approval_required"


@pytest.mark.asyncio
async def test_ai_first_signals_feed_readiness_dimensions(auth_pg_pool, pg_activity_store, tmp_path, monkeypatch) -> None:
    repo = _make_ready_repo(tmp_path)
    service = WorkBriefingService(store=pg_activity_store)
    await service.record_activity({
        "project_id": "CGA123",
        "event_type": "workflow_complete",
        "title": "Completed signal ingestion task",
        "status": "done",
    })
    consumer = AsyncMock()
    consumer.get_jobs_by_repo.return_value = []
    registry = FakeRegistry(FakeGraph({"repositories": 1, "files": 3, "symbols": 9}))

    async with auth_pg_pool.acquire() as db:
        await _seed_project(db, repo_path=str(repo))

    monkeypatch.setattr(main_module, "_work_briefing_service", service)
    monkeypatch.setattr(main_module, "_consumer", consumer)
    monkeypatch.setattr(main_module, "_registry", registry)
    app.dependency_overrides[require_admin] = lambda: {"id": 1, "role": "admin", "username": "admin"}
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            ci_response = await client.post(
                "/api/admin/ai-first/signals",
                json={"project_id": "CGA123", "signal_type": "ci", "name": "policy-ci", "status": "ok", "value": "62", "unit": "tests"},
            )
            pr_response = await client.post(
                "/api/admin/ai-first/signals",
                json={"project_id": "CGA123", "signal_type": "pr", "name": "review", "status": "merged", "value": "18", "unit": "minutes"},
            )
            benchmark_response = await client.post(
                "/api/admin/ai-first/signals",
                json={"project_id": "CGA123", "signal_type": "benchmark", "name": "HPS", "status": "ok", "value": "13.34", "unit": "%"},
            )
            list_response = await client.get("/api/admin/ai-first/signals?project_id=CGA123&limit=10")
            readiness_response = await client.get("/api/admin/ai-first/readiness?project_id=CGA123")
    finally:
        app.dependency_overrides.clear()

    assert ci_response.status_code == 201
    assert pr_response.status_code == 201
    assert benchmark_response.status_code == 201
    assert list_response.status_code == 200
    assert list_response.json()["count"] == 3

    readiness = readiness_response.json()["projects"][0]
    assert readiness["signals"]["ci"]["name"] == "policy-ci"
    assert readiness["signals"]["pr"]["status"] == "merged"
    assert readiness["signals"]["benchmark"]["value"] == "13.34"

    verification = next(item for item in readiness["dimensions"] if item["key"] == "verification")
    workflow = next(item for item in readiness["dimensions"] if item["key"] == "workflow")
    roi = next(item for item in readiness["dimensions"] if item["key"] == "roi")
    assert next(item for item in verification["signals"] if item["key"] == "ci_signal")["status"] == "ok"
    assert next(item for item in workflow["signals"] if item["key"] == "pr_signal")["status"] == "ok"
    assert next(item for item in roi["signals"] if item["key"] == "context_efficiency")["status"] == "ok"


@pytest.mark.asyncio
async def test_ai_first_policy_gates_warn_for_missing_team_default_evidence(auth_pg_pool, pg_activity_store, tmp_path, monkeypatch) -> None:
    repo = _make_ready_repo(tmp_path)
    service = WorkBriefingService(store=pg_activity_store)
    consumer = AsyncMock()
    consumer.get_jobs_by_repo.return_value = []
    registry = FakeRegistry(FakeGraph({"repositories": 1, "files": 3, "symbols": 9}))

    async with auth_pg_pool.acquire() as db:
        await _seed_project(db, repo_path=str(repo))

    monkeypatch.setattr(main_module, "_work_briefing_service", service)
    monkeypatch.setattr(main_module, "_consumer", consumer)
    monkeypatch.setattr(main_module, "_registry", registry)
    app.dependency_overrides[require_admin] = lambda: {"id": 1, "role": "admin", "username": "admin"}
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            update_response = await client.patch(
                "/api/admin/ai-first/policy-profiles",
                json={"project_id": "CGA123", "profile_name": "team-default"},
            )
            readiness_response = await client.get("/api/admin/ai-first/readiness?project_id=CGA123")
    finally:
        app.dependency_overrides.clear()

    assert update_response.status_code == 200
    payload = readiness_response.json()["projects"][0]
    gates = payload["policy_gates"]
    assert gates["profile_name"] == "team-default"
    assert gates["overall_status"] == "warn"
    gate_by_key = {gate["key"]: gate for gate in gates["gates"]}
    assert gate_by_key["saved_evidence_pack"]["status"] == "warn"
    assert gate_by_key["ci_signal"]["status"] == "warn"
    assert gate_by_key["pr_signal"]["status"] == "warn"


@pytest.mark.asyncio
async def test_ai_first_policy_gates_fail_on_failed_ci_signal(auth_pg_pool, pg_activity_store, tmp_path, monkeypatch) -> None:
    repo = _make_ready_repo(tmp_path)
    service = WorkBriefingService(store=pg_activity_store)
    consumer = AsyncMock()
    consumer.get_jobs_by_repo.return_value = []
    registry = FakeRegistry(FakeGraph({"repositories": 1, "files": 3, "symbols": 9}))

    async with auth_pg_pool.acquire() as db:
        await _seed_project(db, repo_path=str(repo))

    monkeypatch.setattr(main_module, "_work_briefing_service", service)
    monkeypatch.setattr(main_module, "_consumer", consumer)
    monkeypatch.setattr(main_module, "_registry", registry)
    app.dependency_overrides[require_admin] = lambda: {"id": 1, "role": "admin", "username": "admin"}
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.patch(
                "/api/admin/ai-first/policy-profiles",
                json={"project_id": "CGA123", "profile_name": "regulated"},
            )
            signal_response = await client.post(
                "/api/admin/ai-first/signals",
                json={"project_id": "CGA123", "signal_type": "ci", "name": "policy-ci", "status": "failed"},
            )
            readiness_response = await client.get("/api/admin/ai-first/readiness?project_id=CGA123")
    finally:
        app.dependency_overrides.clear()

    assert signal_response.status_code == 201
    gates = readiness_response.json()["projects"][0]["policy_gates"]
    assert gates["profile_name"] == "regulated"
    assert gates["overall_status"] == "fail"
    gate_by_key = {gate["key"]: gate for gate in gates["gates"]}
    assert gate_by_key["ci_signal"]["status"] == "fail"
    assert gate_by_key["ci_signal"]["severity"] == "required"