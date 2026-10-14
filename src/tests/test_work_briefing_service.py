from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from backend.workbriefing.models import WorkActivity
from backend.workbriefing.service import WorkActivityValidationError, WorkBriefingService


@pytest.mark.asyncio
async def test_record_activity_persists_and_updates_by_external_id(pg_activity_store) -> None:
    service = WorkBriefingService(store=pg_activity_store)

    created = await service.record_activity(
        {
            "project_id": "CGA123",
            "workspace_name": "Context Graph Agent",
            "event_type": "code_change",
            "external_id": "pr-42",
            "title": "Integrate WA",
            "summary": "ported activity store",
            "status": "in_progress",
            "tags": ["merge", "wa"],
        }
    )
    updated = await service.record_activity(
        {
            "project_id": "CGA123",
            "workspace_name": "Context Graph Agent",
            "event_type": "code_change",
            "external_id": "pr-42",
            "title": "Integrate WA",
            "summary": "added admin briefing",
            "status": "done",
            "tags": ["merge", "briefing"],
        }
    )
    rows = await service.list_recent(project_id="CGA123")

    assert created.operation == "created"
    assert updated.operation == "updated"
    assert created.activity.activity_id == updated.activity.activity_id
    assert len(rows) == 1
    assert rows[0].summary == "added admin briefing"
    assert rows[0].status == "done"
    assert rows[0].tags == ("merge", "briefing")


@pytest.mark.asyncio
async def test_get_briefing_aggregates_projects_and_statuses(pg_activity_store) -> None:
    service = WorkBriefingService(store=pg_activity_store)

    await service.record_activity(
        {
            "project_id": "CGA123",
            "event_type": "review",
            "title": "Reviewed PR",
            "status": "done",
        }
    )
    await service.record_activity(
        {
            "project_id": "WA123",
            "event_type": "sync",
            "title": "Synced tickets",
            "status": "pending",
        }
    )

    briefing = await service.get_briefing(limit=10)

    assert briefing["total_events"] == 2
    assert briefing["event_type_counts"] == {"review": 1, "sync": 1}
    assert briefing["status_counts"] == {"done": 1, "pending": 1}
    assert briefing["project_counts"] == {"CGA123": 1, "WA123": 1}


@pytest.mark.asyncio
async def test_get_memory_context_exports_sanitized_activity_with_provenance() -> None:
    activity = WorkActivity(
        activity_id="activity-123",
        plugin_id="workassist.mcp.activity",
        source_system="workassist_mcp",
        source_type="work_activity",
        source_item_id="commit-123",
        project_id="CGA123",
        workspace_name="Context Graph Agent",
        event_type="code_change",
        title="Updated memory context",
        summary="Added a read-only memory context tool.",
        body_text="RAW_BODY_MUST_NOT_BE_EXPORTED",
        status="done",
        priority=None,
        owner=None,
        source_url=(
            "https://user:pass@github.com/example/cga/commit/"
            "abcdef0123456789abcdef0123456789abcdef01?token=RAW_URL_SECRET#details"
        ),
        tags=("memory", "read-only"),
        occurred_at=datetime(2026, 10, 13, tzinfo=timezone.utc),
        synced_at=datetime(2026, 10, 13, tzinfo=timezone.utc),
        content_hash="hash",
        raw_metadata={"access_token": "RAW_METADATA_MUST_NOT_BE_EXPORTED"},
        embedding_text="RAW_EMBEDDING_MUST_NOT_BE_EXPORTED",
    )
    service = WorkBriefingService()
    service.list_recent = AsyncMock(return_value=[activity])

    context = await service.get_memory_context(project_id="CGA123", limit=10)

    assert context["project_id"] == "CGA123"
    assert context["count"] == 1
    memory = context["memories"][0]
    assert memory["summary"] == "Added a read-only memory context tool."
    assert memory["event_type"] == "code_change"
    assert memory["tags"] == ["memory", "read-only"]
    assert memory["confidence"] is None
    assert memory["provenance"]["source_item_id"] == "commit-123"
    assert memory["provenance"]["project_id"] == "CGA123"
    assert memory["provenance"]["source_url"].endswith("/commit/abcdef0123456789abcdef0123456789abcdef01")
    assert memory["provenance"]["commit_sha"] == "abcdef0123456789abcdef0123456789abcdef01"
    assert "RAW_BODY_MUST_NOT_BE_EXPORTED" not in str(context)
    assert "RAW_METADATA_MUST_NOT_BE_EXPORTED" not in str(context)
    assert "RAW_EMBEDDING_MUST_NOT_BE_EXPORTED" not in str(context)
    assert "RAW_URL_SECRET" not in str(context)
    assert "raw_metadata" not in memory
    assert "embedding_text" not in memory


@pytest.mark.asyncio
async def test_record_activity_rejects_missing_required_fields(pg_activity_store) -> None:
    service = WorkBriefingService(store=pg_activity_store)

    with pytest.raises(WorkActivityValidationError):
        await service.record_activity({"project_id": "CGA123", "event_type": "sync"})
