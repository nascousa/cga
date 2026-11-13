import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

import backend.main as main_module
from backend.auth.dependencies import require_admin
from backend.auth.database import get_db
from backend.reports import ReportFilters, build_report
from backend.workbriefing.service import WorkBriefingService
from backend.workbriefing import store as activity_store


def filters(**changes):
    return ReportFilters(
        start="2026-11-01T00:00:00Z", end="2026-11-12T00:00:00Z", **changes,
    )


@pytest.mark.parametrize("start,end", [
    ("2026-11-01", "2026-11-12T00:00:00Z"),
    ("2026-11-12T00:00:00Z", "2026-11-01T00:00:00Z"),
    ("2025-01-01T00:00:00Z", "2026-11-12T00:00:00Z"),
])
def test_report_rejects_ambiguous_or_unbounded_windows(start, end):
    with pytest.raises(ValidationError):
        ReportFilters(start=start, end=end)


@pytest.mark.asyncio
async def test_report_counts_do_not_infer_outcomes_or_expose_raw_metadata():
    row = SimpleNamespace(
        activity_id="evidence-1", project_id="P1", event_type="review", status=None,
        title="<img onerror=attack()>", summary="Recorded evidence",
        occurred_at=datetime(2026, 11, 3, 5, tzinfo=timezone.utc),
        raw_metadata={"token": "never-export"}, body_text="private body",
    )
    service = SimpleNamespace(report_rows=AsyncMock(return_value=([row], True)))
    result = await build_report(service, filters(search="evidence"))
    assert result["counts"]["by_status"] == {"unspecified": 1}
    assert result["counts"]["by_day"] == {"2026-11-03": 1}
    assert result["coverage"]["truncated"]
    assert "Partial report" in result["coverage"]["note"]
    assert result["activities"][0]["title"] == row.title
    assert "never-export" not in str(result) and "private body" not in str(result)
    assert service.report_rows.await_args.kwargs["search"] == "evidence"


@pytest.mark.asyncio
async def test_report_filters_are_applied_before_limit_and_use_half_open_utc_window(pg_activity_store):
    service = WorkBriefingService(store=pg_activity_store)
    for project, when, status, title in [
        ("P1", "2026-11-01T00:00:00Z", "done", "Literal %_ evidence"),
        ("P1", "2026-11-12T00:00:00Z", "done", "End excluded"),
        ("P1", "2026-11-02T00:00:00Z", "failed", "Wrong status"),
        ("P2", "2026-11-02T00:00:00Z", "done", "Wrong project"),
        ("P1", "2026-11-03T00:00:00Z", "done", "Not matching search"),
    ]:
        await service.record_activity({
            "project_id": project, "occurred_at": when, "event_type": "review",
            "status": status, "title": title,
        })
    result = await build_report(service, filters(
        project_ids=["P1"], status="done", event_type="review", search="%_",
    ))
    assert result["counts"]["events"] == 1
    assert result["activities"][0]["title"] == "Literal %_ evidence"
    assert not result["coverage"]["truncated"]
    limited, truncated = await service.report_rows(**filters().model_dump(), limit=1)
    assert len(limited) == 1 and truncated


@pytest.mark.asyncio
async def test_report_api_requires_admin_and_validated_filters(monkeypatch):
    app = main_module.app
    async def unused_db():
        yield None
    app.dependency_overrides[get_db] = unused_db
    body = filters().model_dump(mode="json")
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.post("/api/admin/reports/generate", json=body)).status_code == 401
            app.dependency_overrides[require_admin] = lambda: {"role": "admin"}
            assert (await client.post("/api/admin/reports/generate", json={**body, "end": body["start"]})).status_code == 422
            service = SimpleNamespace(report_rows=AsyncMock(return_value=([], False)))
            monkeypatch.setattr(main_module, "_work_briefing_service", service)
            response = await client.post("/api/admin/reports/generate", json=body)
            assert response.status_code == 200 and response.json()["counts"]["events"] == 0
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_report_projects_summaries_without_loading_unbounded_ingestion_fields(pg_activity_store, monkeypatch):
    service = WorkBriefingService(store=pg_activity_store)
    await service.record_activity({
        "project_id": "P1", "occurred_at": "2026-11-02T00:00:00Z",
        "event_type": "review", "title": "Small evidence", "summary": "Summary",
        "body_text": "private body" * 100000,
        "metadata": {"unused": "private metadata" * 100000},
    })
    rows = await pg_activity_store.report_rows(**filters().model_dump(), limit=2)
    assert len(rows) == 1
    assert rows[0].title == "Small evidence"
    assert rows[0].body_text == rows[0].embedding_text == ""
    assert rows[0].raw_metadata == {}
    result = await build_report(service, filters())
    assert result["counts"]["events"] == 1
    with monkeypatch.context() as bounds:
        bounds.setattr(activity_store, "REPORT_BYTE_LIMIT", 1)
        with pytest.raises(activity_store.ReportSizeError, match="Narrow"):
            await pg_activity_store.report_rows(**filters().model_dump(), limit=2)
    await service.record_activity({
        "project_id": "P1", "occurred_at": "2026-11-02T00:00:00Z",
        "event_type": "review", "title": "x" * (activity_store.REPORT_ROW_BYTE_LIMIT + 1),
    })
    with pytest.raises(activity_store.ReportSizeError, match="Narrow"):
        await pg_activity_store.report_rows(**filters().model_dump(), limit=2)


@pytest.mark.asyncio
async def test_report_preserves_learning_safety_validation_with_summary_projection(pg_activity_store):
    service = WorkBriefingService(store=pg_activity_store)
    recorded = await service.record_learning(
        project_id="P1", title="Valid candidate", lesson="A bounded lesson",
        category="environment_fact", outcome="neutral",
    )
    now = datetime.now(timezone.utc)
    recent = ReportFilters(start=now.replace(year=now.year - 1), end=now)
    rows, _ = await service.report_rows(**recent.model_dump(), limit=5)
    assert [row.activity_id for row in rows] == [recorded.activity.activity_id]
    pool = await pg_activity_store._get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE {pg_activity_store._qualified('work_activities')} SET body_text='invalid learning body'"
        )
    rows, _ = await service.report_rows(**recent.model_dump(), limit=5)
    assert rows == [], "Projection must retain fields needed to reject malformed learning records"


@pytest.mark.asyncio
@pytest.mark.parametrize("oversized_row", [False, True])
async def test_report_stream_rejects_row_and_total_byte_overflow_without_fetch_all(monkeypatch, oversized_row):
    store = activity_store.PgVectorActivityStore()
    monkeypatch.setattr(store, "ensure_schema", AsyncMock())
    monkeypatch.setattr(activity_store, "REPORT_BYTE_LIMIT", 20)
    consumed = []

    async def cursor(sql, *args, prefetch):
        assert prefetch == 1
        if oversized_row:
            consumed.append(1)
            yield {"report_bytes": activity_store.REPORT_ROW_BYTE_LIMIT + 1, "payload": None}
        else:
            for number in range(10):
                consumed.append(number)
                yield {"report_bytes": 12, "payload": json.dumps({
                    "occurred_at": "2026-11-02T00:00:00+00:00",
                    "synced_at": "2026-11-02T00:00:00+00:00",
                })}

    @asynccontextmanager
    async def transaction(**kwargs):
        yield

    conn = SimpleNamespace(transaction=transaction, execute=AsyncMock(), cursor=cursor)

    @asynccontextmanager
    async def acquire():
        yield conn

    monkeypatch.setattr(store, "_get_pool", AsyncMock(return_value=SimpleNamespace(acquire=acquire)))
    monkeypatch.setattr(store, "_activity_from_row", lambda row: row)
    with pytest.raises(activity_store.ReportSizeError, match="Narrow"):
        await store.report_rows(**filters().model_dump(), limit=10000)
    assert len(consumed) == (1 if oversized_row else 2)


@pytest.mark.asyncio
async def test_report_api_surfaces_byte_limit_as_actionable_error(monkeypatch):
    app = main_module.app
    app.dependency_overrides[require_admin] = lambda: {"role": "admin"}
    service = SimpleNamespace(report_rows=AsyncMock(side_effect=activity_store.ReportSizeError(
        "Report data exceeds the byte limit. Narrow the report filters."
    )))
    monkeypatch.setattr(main_module, "_work_briefing_service", service)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/admin/reports/generate", json=filters().model_dump(mode="json"))
        assert response.status_code == 413
        assert "Narrow" in response.json()["detail"]
    finally:
        app.dependency_overrides.clear()
