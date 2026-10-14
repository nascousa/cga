from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from backend.workbriefing.models import WorkActivity, parse_utc_datetime
from backend.workbriefing.store import ActivityStore, ActivityUpsertResult, PgVectorActivityStore


PLUGIN_ID = "workassist.mcp.activity"
SOURCE_SYSTEM = "workassist_mcp"
SOURCE_TYPE = "work_activity"


class WorkActivityValidationError(ValueError):
    pass


class WorkBriefingService:
    def __init__(self, store: ActivityStore | None = None) -> None:
        self._store: ActivityStore = store or PgVectorActivityStore()

    async def record_activity(self, payload: dict[str, Any]) -> ActivityUpsertResult:
        project_id = self._required_string(payload, "project_id")
        event_type = self._required_string(payload, "event_type")
        title = self._required_string(payload, "title")
        occurred_at = parse_utc_datetime(payload.get("occurred_at"), "occurred_at")
        synced_at = datetime.now(timezone.utc)

        summary = self._optional_string(payload, "summary") or ""
        body_text = self._optional_string(payload, "body_text") or ""
        workspace_name = self._optional_string(payload, "workspace_name")
        status = self._optional_string(payload, "status")
        priority = self._optional_string(payload, "priority")
        owner = self._optional_string(payload, "owner")
        source_url = self._optional_string(payload, "source_url")
        tags = self._normalize_tags(payload.get("tags"))
        raw_metadata = self._normalize_metadata(payload.get("metadata"))

        content_hash = self._content_hash(
            {
                "project_id": project_id,
                "workspace_name": workspace_name,
                "event_type": event_type,
                "title": title,
                "summary": summary,
                "body_text": body_text,
                "status": status,
                "priority": priority,
                "owner": owner,
                "source_url": source_url,
                "tags": tags,
                "occurred_at": occurred_at.isoformat(),
                "metadata": raw_metadata,
            }
        )
        requested_source_item_id = self._optional_string(payload, "external_id") or f"generated:{content_hash[:24]}"
        existing_by_content = await self._store.find_by_content_hash(
            plugin_id=PLUGIN_ID,
            project_id=project_id,
            source_type=SOURCE_TYPE,
            content_hash=content_hash,
        )
        source_item_id = existing_by_content.source_item_id if existing_by_content else requested_source_item_id
        activity_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{PLUGIN_ID}:{project_id}:{source_item_id}"))
        embedding_text = self._build_embedding_text(project_id, event_type, title, summary, body_text, tags, status)

        activity = WorkActivity(
            activity_id=activity_id,
            plugin_id=PLUGIN_ID,
            source_system=SOURCE_SYSTEM,
            source_type=SOURCE_TYPE,
            source_item_id=source_item_id,
            project_id=project_id,
            workspace_name=workspace_name,
            event_type=event_type,
            title=title,
            summary=summary,
            body_text=body_text,
            status=status,
            priority=priority,
            owner=owner,
            source_url=source_url,
            tags=tags,
            occurred_at=occurred_at,
            synced_at=synced_at,
            content_hash=content_hash,
            raw_metadata=raw_metadata,
            embedding_text=embedding_text,
        )
        result = await self._store.upsert(activity)
        if existing_by_content and existing_by_content.source_item_id != requested_source_item_id:
            return ActivityUpsertResult(operation="deduplicated", activity=result.activity)
        return result

    async def list_recent(self, project_id: str | None = None, limit: int = 25) -> list[WorkActivity]:
        return await self._store.list_recent(project_id=project_id, limit=limit)

    async def count_recent(self, project_id: str | None = None) -> int:
        return await self._store.count_recent(project_id=project_id)

    async def cleanup_exact_duplicates(self, project_id: str | None = None, dry_run: bool = True) -> dict[str, Any]:
        return await self._store.cleanup_exact_duplicates(project_id=project_id, dry_run=dry_run)

    async def get_briefing(self, project_id: str | None = None, limit: int = 25) -> dict[str, Any]:
        activities = await self.list_recent(project_id=project_id, limit=limit)
        total_available = await self.count_recent(project_id=project_id)
        event_type_counts: dict[str, int] = {}
        status_counts: dict[str, int] = {}
        project_counts: dict[str, int] = {}
        duplicate_activity_map, duplicate_groups = self._detect_suspected_duplicates(activities)

        for activity in activities:
            event_type_counts[activity.event_type] = event_type_counts.get(activity.event_type, 0) + 1
            project_counts[activity.project_id] = project_counts.get(activity.project_id, 0) + 1
            if activity.status:
                status_counts[activity.status] = status_counts.get(activity.status, 0) + 1

        return {
            "project_id": project_id,
            "total_events": len(activities),
            "total_available": total_available,
            "has_more": total_available > len(activities),
            "event_type_counts": event_type_counts,
            "status_counts": status_counts,
            "project_counts": project_counts,
            "duplicate_signal_count": len(duplicate_groups),
            "suspected_duplicates": duplicate_groups,
            "activities": [
                {
                    **activity.to_dict(),
                    **duplicate_activity_map.get(activity.activity_id, {}),
                }
                for activity in activities
            ],
        }

    async def get_memory_context(self, project_id: str, limit: int = 25) -> dict[str, Any]:
        activities = await self.list_recent(project_id=project_id, limit=max(1, min(int(limit), 100)))
        memories = [self._to_memory_item(activity) for activity in activities]
        return {
            "project_id": project_id,
            "count": len(memories),
            "scenarios": self._build_scenarios(activities),
            "memories": memories,
        }

    async def search_memory(self, project_id: str, query: str, limit: int = 25) -> dict[str, Any]:
        cleaned_query = " ".join((query or "").split())
        if not cleaned_query:
            raise WorkActivityValidationError("query is required")
        result = await self._store.search_activities(
            project_id=project_id,
            query=cleaned_query[:200],
            limit=max(1, min(int(limit), 100)),
        )
        memories = [self._to_memory_item(activity) for activity in result.activities]
        return {
            "project_id": project_id,
            "query": cleaned_query[:200],
            "search_mode": result.mode,
            "fts_available": result.fts_available,
            "count": len(memories),
            "memories": memories,
        }

    @staticmethod
    def _build_scenarios(activities: list[WorkActivity]) -> list[dict[str, Any]]:
        grouped: dict[str, list[WorkActivity]] = {}
        for activity in activities:
            grouped.setdefault(activity.event_type, []).append(activity)

        scenarios: list[dict[str, Any]] = []
        for event_type, members in grouped.items():
            ordered = sorted(members, key=lambda item: (item.occurred_at, item.activity_id), reverse=True)
            status_counts: dict[str, int] = {}
            tag_counts: dict[str, int] = {}
            for item in ordered:
                if item.status:
                    status_counts[item.status] = status_counts.get(item.status, 0) + 1
                for tag in item.tags:
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1
            scenarios.append(
                {
                    "scenario": event_type,
                    "memory_count": len(ordered),
                    "latest_occurred_at": ordered[0].occurred_at.isoformat().replace("+00:00", "Z"),
                    "status_counts": dict(sorted(status_counts.items())),
                    "top_tags": [tag for tag, _ in sorted(tag_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:5]],
                    "memory_ids": [item.activity_id for item in ordered],
                }
            )
        scenarios.sort(key=lambda item: (-item["memory_count"], item["scenario"]))
        return scenarios

    @classmethod
    def _to_memory_item(cls, activity: WorkActivity) -> dict[str, Any]:
        return {
            "memory_id": activity.activity_id,
            "summary": activity.summary.strip() or activity.title.strip(),
            "event_type": activity.event_type,
            "status": activity.status,
            "tags": list(activity.tags),
            "confidence": None,
            # Derived items are visible only inside the authenticated project; nothing is shared until reviewed.
            "governance": {
                "owner": activity.owner,
                "visibility": "project",
                "review_status": "unreviewed",
                "version": activity.content_hash[:12],
            },
            "provenance": {
                "source_activity_id": activity.activity_id,
                "source_item_id": activity.source_item_id,
                "project_id": activity.project_id,
                "source_url": cls._safe_source_url(activity.source_url),
                "commit_sha": cls._commit_sha(activity.source_url),
                "occurred_at": activity.occurred_at.isoformat().replace("+00:00", "Z"),
            },
        }

    @staticmethod
    def _safe_source_url(value: str | None) -> str | None:
        if not value:
            return None
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        try:
            port = parsed.port
        except ValueError:
            return None
        netloc = f"{host}:{port}" if port is not None else host
        return urlunsplit((parsed.scheme.lower(), netloc, parsed.path, "", ""))

    @staticmethod
    def _commit_sha(source_url: str | None) -> str | None:
        if not source_url:
            return None
        match = re.search(r"/(?:commit|commits)/([0-9a-f]{7,40})(?:[/?#]|$)", source_url, re.IGNORECASE)
        return match.group(1).lower() if match else None

    @staticmethod
    def _required_string(payload: dict[str, Any], field_name: str) -> str:
        value = payload.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise WorkActivityValidationError(f"{field_name} is required")
        return value.strip()

    @staticmethod
    def _optional_string(payload: dict[str, Any], field_name: str) -> str | None:
        value = payload.get(field_name)
        if value is None:
            return None
        if not isinstance(value, str):
            raise WorkActivityValidationError(f"{field_name} must be a string")
        cleaned = value.strip()
        return cleaned or None

    @staticmethod
    def _normalize_tags(value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, list):
            raise WorkActivityValidationError("tags must be a list of strings")

        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, str):
                raise WorkActivityValidationError("tags must be a list of strings")
            cleaned = item.strip()
            if cleaned and cleaned not in seen:
                normalized.append(cleaned)
                seen.add(cleaned)
        return tuple(normalized)

    @staticmethod
    def _normalize_metadata(value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise WorkActivityValidationError("metadata must be an object")
        try:
            json.dumps(value, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise WorkActivityValidationError("metadata must be JSON serializable") from exc
        return value

    @staticmethod
    def _content_hash(payload: dict[str, Any]) -> str:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _normalize_duplicate_title(title: str) -> str:
        return " ".join((title or "").strip().casefold().split())

    @classmethod
    def _detect_suspected_duplicates(cls, activities: list[WorkActivity]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
        grouped: dict[tuple[str, str, str, str], list[WorkActivity]] = {}
        for activity in activities:
            key = (
                activity.project_id,
                activity.event_type,
                cls._normalize_duplicate_title(activity.title),
                activity.occurred_at.date().isoformat(),
            )
            grouped.setdefault(key, []).append(activity)

        activity_map: dict[str, dict[str, Any]] = {}
        duplicate_groups: list[dict[str, Any]] = []
        for (project_id, event_type, normalized_title, occurred_on), group in grouped.items():
            if len(group) < 2:
                continue
            unique_sources = {item.source_item_id for item in group}
            unique_hashes = {item.content_hash for item in group}
            if len(unique_sources) < 2 and len(unique_hashes) < 2:
                continue
            sorted_group = sorted(group, key=lambda item: (item.occurred_at, item.synced_at), reverse=True)
            group_key = f"{project_id}|{event_type}|{normalized_title}|{occurred_on}"
            duplicate_groups.append(
                {
                    "group_key": group_key,
                    "project_id": project_id,
                    "event_type": event_type,
                    "title": sorted_group[0].title,
                    "occurred_on": occurred_on,
                    "duplicate_count": len(sorted_group),
                    "source_item_ids": [item.source_item_id for item in sorted_group],
                    "content_hashes": len(unique_hashes),
                    "members": [
                        {
                            "activity_id": item.activity_id,
                            "source_item_id": item.source_item_id,
                            "content_hash": item.content_hash,
                            "occurred_at": item.occurred_at.isoformat().replace("+00:00", "Z"),
                            "synced_at": item.synced_at.isoformat().replace("+00:00", "Z"),
                            "summary": item.summary,
                            "status": item.status,
                        }
                        for item in sorted_group
                    ],
                    "reason": "same project/event/title/day with multiple source items or payload variants",
                }
            )
            for item in sorted_group:
                activity_map[item.activity_id] = {
                    "suspected_duplicate": True,
                    "duplicate_group_key": group_key,
                    "duplicate_group_count": len(sorted_group),
                    "duplicate_reason": "same project/event/title/day",
                }

        duplicate_groups.sort(key=lambda item: (item["occurred_on"], item["duplicate_count"]), reverse=True)
        return activity_map, duplicate_groups

    @staticmethod
    def _build_embedding_text(
        project_id: str,
        event_type: str,
        title: str,
        summary: str,
        body_text: str,
        tags: tuple[str, ...],
        status: str | None,
    ) -> str:
        sections = [
            f"Project: {project_id}",
            f"Event: {event_type}",
            f"Title: {title}",
        ]
        if status:
            sections.append(f"Status: {status}")
        if summary:
            sections.append(f"Summary: {summary}")
        if body_text:
            sections.append(f"Body: {body_text}")
        if tags:
            sections.append(f"Tags: {', '.join(tags)}")
        return "\n".join(sections)