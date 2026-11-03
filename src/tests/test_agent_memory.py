from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import asyncpg
import pytest

from backend.workbriefing.models import WorkActivity
from backend.workbriefing.service import WorkActivityValidationError, WorkBriefingService
from backend.workbriefing.store import ActivitySearchResult, PgVectorActivityStore


def _activity(
    activity_id: str,
    *,
    event_type: str = "code_change",
    summary: str = "summary",
    status: str | None = "done",
    tags: tuple[str, ...] = (),
    owner: str | None = None,
    day: int = 1,
) -> WorkActivity:
    return WorkActivity(
        activity_id=activity_id,
        plugin_id="workassist.mcp.activity",
        source_system="workassist_mcp",
        source_type="work_activity",
        source_item_id=f"item-{activity_id}",
        project_id="CGA123",
        workspace_name=None,
        event_type=event_type,
        title=f"Title {activity_id}",
        summary=summary,
        body_text="RAW_BODY",
        status=status,
        priority=None,
        owner=owner,
        source_url=None,
        tags=tags,
        occurred_at=datetime(2026, 10, day, tzinfo=timezone.utc),
        synced_at=datetime(2026, 10, day, tzinfo=timezone.utc),
        content_hash=f"{activity_id}0123456789abcdef",
        raw_metadata={},
        embedding_text="",
    )


@pytest.mark.asyncio
async def test_memory_context_adds_governance_and_scenario_layer() -> None:
    service = WorkBriefingService()
    service.list_recent = AsyncMock(
        return_value=[
            _activity("a1", event_type="code_change", tags=("memory", "mcp"), owner="nate", day=3),
            _activity("a2", event_type="code_change", status="in_progress", tags=("memory",), day=2),
            _activity("a3", event_type="review", tags=("pr",), day=1),
        ]
    )

    context = await service.get_memory_context(project_id="CGA123", limit=10)

    governance = context["memories"][0]["governance"]
    assert governance == {
        "owner": "nate",
        "visibility": "project",
        "review_status": "unreviewed",
        "version": "a10123456789",
    }
    assert context["scenarios"] == [
        {
            "scenario": "code_change",
            "memory_count": 2,
            "latest_occurred_at": "2026-10-03T00:00:00Z",
            "status_counts": {"done": 1, "in_progress": 1},
            "top_tags": ["memory", "mcp"],
            "memory_ids": ["a1", "a2"],
        },
        {
            "scenario": "review",
            "memory_count": 1,
            "latest_occurred_at": "2026-10-01T00:00:00Z",
            "status_counts": {"done": 1},
            "top_tags": ["pr"],
            "memory_ids": ["a3"],
        },
    ]
    assert "RAW_BODY" not in str(context)


@pytest.mark.asyncio
async def test_search_memory_returns_sanitized_items_and_search_health() -> None:
    service = WorkBriefingService()
    store = AsyncMock()
    store.search_activities = AsyncMock(
        return_value=ActivitySearchResult(
            activities=[_activity("a1", summary="Added memory search")],
            mode="lexical_fallback",
            fts_available=False,
        )
    )
    service._store = store

    result = await service.search_memory(project_id="CGA123", query="  memory  ", limit=500)

    store.search_activities.assert_awaited_once_with(project_id="CGA123", query="memory", limit=100)
    assert result["project_id"] == "CGA123"
    assert result["query"] == "memory"
    assert result["search_mode"] == "lexical_fallback"
    assert result["fts_available"] is False
    assert result["count"] == 1
    assert result["memories"][0]["summary"] == "Added memory search"
    assert "RAW_BODY" not in str(result)


@pytest.mark.asyncio
async def test_search_memory_rejects_blank_query() -> None:
    service = WorkBriefingService()

    with pytest.raises(WorkActivityValidationError, match="query is required"):
        await service.search_memory(project_id="CGA123", query="   ")


@pytest.mark.asyncio
async def test_record_learning_persists_bounded_candidate_with_harness_provenance() -> None:
    service = WorkBriefingService()
    service.record_activity = AsyncMock(return_value="stored")

    result = await service.record_learning(
        project_id="CGA123",
        title="Prefer graph context for call-chain changes",
        lesson="Check callers and callees before editing a shared helper.",
        category="successful_pattern",
        outcome="success",
        external_id="task-42:reflection",
        harness="codex",
        task_id="task-42",
        session_id="session-abc",
        evidence_activity_ids=["activity-1", "activity-2"],
    )

    assert result == "stored"
    service.record_activity.assert_awaited_once()
    payload = service.record_activity.await_args.args[0]
    assert payload["project_id"] == "CGA123"
    assert payload["event_type"] == "agent_learning"
    assert payload["external_id"] == "learning:task-42:reflection"
    assert payload["summary"] == "Check callers and callees before editing a shared helper."
    assert payload["body_text"] == ""
    assert payload["status"] == "candidate"
    assert payload["tags"] == ["learning", "category:successful_pattern", "outcome:success"]
    assert payload["metadata"]["learning"] == {
        "schema_version": 1,
        "category": "successful_pattern",
        "outcome": "success",
        "harness": "codex",
        "task_id": "task-42",
        "session_id": "session-abc",
        "evidence_activity_ids": ["activity-1", "activity-2"],
        "lifecycle": "candidate",
    }


@pytest.mark.asyncio
async def test_record_learning_rejects_invalid_or_unbounded_input() -> None:
    service = WorkBriefingService()

    with pytest.raises(WorkActivityValidationError, match="category"):
        await service.record_learning(
            project_id="CGA123",
            title="Title",
            lesson="Lesson",
            category="unknown",
            outcome="success",
        )
    with pytest.raises(WorkActivityValidationError, match="lesson"):
        await service.record_learning(
            project_id="CGA123",
            title="Title",
            lesson="x" * 2001,
            category="failure_correction",
            outcome="failure",
        )
    with pytest.raises(WorkActivityValidationError, match="evidence_activity_ids"):
        await service.record_learning(
            project_id="CGA123",
            title="Title",
            lesson="Lesson",
            category="failure_correction",
            outcome="failure",
            evidence_activity_ids=[""] * 21,
        )
    with pytest.raises(WorkActivityValidationError, match="credential"):
        await service.record_learning(
            project_id="CGA123",
            title="Credential leak",
            lesson="The API token=abcdef1234567890 should never be stored.",
            category="failure_correction",
            outcome="corrected",
        )


@pytest.mark.asyncio
async def test_get_learning_context_only_returns_learning_provenance() -> None:
    service = WorkBriefingService()
    store = AsyncMock()
    store.list_recent = AsyncMock(
        return_value=[
            replace(
                _activity("lesson-1", event_type="agent_learning", summary="Check callers before editing"),
                raw_metadata={
                    "learning": {
                        "schema_version": 1,
                        "category": "failure_correction",
                        "outcome": "corrected",
                        "harness": "copilot",
                        "task_id": "TASK-1",
                        "session_id": "SESSION-1",
                        "evidence_activity_ids": ["activity-1"],
                        "lifecycle": "candidate",
                        "untrusted_extra": "not exported",
                    },
                    "sensitive_value": "not exported",
                },
            )
        ]
    )
    service._store = store

    context = await service.get_learning_context(project_id="CGA123", limit=500)

    store.list_recent.assert_awaited_once_with(
        project_id="CGA123",
        limit=100,
        event_type="agent_learning",
    )
    assert context["count"] == 1
    assert context["learnings"][0]["learning"] == {
        "schema_version": 1,
        "category": "failure_correction",
        "outcome": "corrected",
        "harness": "copilot",
        "task_id": "TASK-1",
        "session_id": "SESSION-1",
        "evidence_activity_ids": ["activity-1"],
        "lifecycle": "candidate",
    }
    assert "sensitive_value" not in str(context)


@pytest.mark.asyncio
async def test_store_filters_learning_by_project_and_event_type() -> None:
    class Acquire:
        def __init__(self, connection) -> None:
            self.connection = connection

        async def __aenter__(self):
            return self.connection

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class Pool:
        def __init__(self, connection) -> None:
            self.connection = connection

        def acquire(self):
            return Acquire(self.connection)

    connection = AsyncMock()
    connection.fetch.return_value = []
    store = PgVectorActivityStore()
    store.ensure_schema = AsyncMock()
    store._get_pool = AsyncMock(return_value=Pool(connection))

    result = await store.list_recent(
        project_id="CGA123",
        limit=12,
        event_type="agent_learning",
    )

    assert result == []
    sql, *params = connection.fetch.await_args.args
    assert "WHERE project_id = $1 AND event_type = $2" in sql
    assert sql.endswith("LIMIT $3")
    assert params == ["CGA123", "agent_learning", 12]


async def _seed(store) -> WorkBriefingService:
    service = WorkBriefingService(store=store)
    await service.record_activity(
        {
            "project_id": "CGA123",
            "event_type": "code_change",
            "external_id": "fts-1",
            "title": "Memory search",
            "summary": "Added governance metadata for derived memory",
            "body_text": "hiddenbodyterm",
            "tags": ["memory"],
        }
    )
    await service.record_activity(
        {
            "project_id": "CGA123",
            "event_type": "review",
            "external_id": "fts-2",
            "title": "Reviewed relay",
            "summary": "Relay health check reviewed",
        }
    )
    await service.record_activity(
        {
            "project_id": "WA999",
            "event_type": "code_change",
            "external_id": "fts-3",
            "title": "Other project",
            "summary": "governance metadata in another project",
        }
    )
    return service


@pytest.mark.asyncio
async def test_store_full_text_search_is_project_scoped_and_ignores_body(pg_activity_store) -> None:
    await _seed(pg_activity_store)

    result = await pg_activity_store.search_activities(project_id="CGA123", query="governance metadata", limit=10)
    hidden = await pg_activity_store.search_activities(project_id="CGA123", query="hiddenbodyterm", limit=10)

    assert result.mode == "full_text"
    assert result.fts_available is True
    assert [activity.source_item_id for activity in result.activities] == ["fts-1"]
    assert hidden.activities == []


@pytest.mark.asyncio
async def test_store_search_falls_back_to_lexical_when_full_text_fails(pg_activity_store, monkeypatch) -> None:
    await _seed(pg_activity_store)

    async def _broken_full_text(*args, **kwargs):
        raise asyncpg.exceptions.UndefinedFunctionError("websearch_to_tsquery unavailable")

    monkeypatch.setattr(pg_activity_store, "_search_full_text", _broken_full_text)

    result = await pg_activity_store.search_activities(project_id="CGA123", query="relay health", limit=10)
    wildcard = await pg_activity_store.search_activities(project_id="CGA123", query="%", limit=10)

    assert result.mode == "lexical_fallback"
    assert result.fts_available is False
    assert [activity.source_item_id for activity in result.activities] == ["fts-2"]
    assert wildcard.activities == []
