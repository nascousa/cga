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
_LEARNING_CREDENTIAL_RE = re.compile(
    r"(?i)(?:password|passwd|secret|token|api[_-]?key|access[_-]?key)\s*[:=]\s*\S{4,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{20,})\b"
)


class WorkActivityValidationError(ValueError):
    pass


class WorkBriefingService:
    def __init__(self, store: ActivityStore | None = None) -> None:
        self._store: ActivityStore = store or PgVectorActivityStore()

    async def record_activity(self, payload: dict[str, Any]) -> ActivityUpsertResult:
        event_type = self._optional_string(payload, "event_type")
        external_id = self._optional_string(payload, "external_id")
        if event_type == "agent_learning" or (external_id and external_id.startswith("learning:")):
            raise WorkActivityValidationError("learning records must use the dedicated learning endpoint")
        return await self._upsert_activity(payload)

    async def _upsert_activity(self, payload: dict[str, Any]) -> ActivityUpsertResult:
        event_type = self._required_string(payload, "event_type")
        external_id = self._optional_string(payload, "external_id")
        if event_type == "agent_learning":
            self._validate_learning_payload(payload)
        elif external_id and external_id.startswith("learning:"):
            raise WorkActivityValidationError("learning identity namespace is reserved")

        project_id = self._required_string(payload, "project_id")
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
        requested_source_item_id = external_id or f"generated:{content_hash[:24]}"
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

    async def record_learning(
        self,
        *,
        project_id: str,
        title: str,
        lesson: str,
        category: str,
        outcome: str,
        external_id: str | None = None,
        harness: str | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        evidence_activity_ids: list[str] | None = None,
    ) -> ActivityUpsertResult:
        normalized = self._normalize_learning_fields(
            project_id=project_id,
            title=title,
            lesson=lesson,
            category=category,
            outcome=outcome,
            external_id=external_id,
            harness=harness,
            task_id=task_id,
            session_id=session_id,
            evidence_activity_ids=evidence_activity_ids,
        )

        metadata_learning = {
            "schema_version": 1,
            "category": normalized["category"],
            "outcome": normalized["outcome"],
            "harness": normalized["harness"],
            "task_id": normalized["task_id"],
            "session_id": normalized["session_id"],
            "evidence_activity_ids": normalized["evidence_activity_ids"],
            "lifecycle": "candidate",
        }
        return await self._upsert_activity(
            {
                "project_id": normalized["project_id"],
                "event_type": "agent_learning",
                "external_id": f"learning:{normalized['learning_identity']}",
                "title": normalized["title"],
                "summary": normalized["lesson"],
                "body_text": "",
                "status": "candidate",
                "tags": [
                    "learning",
                    f"category:{normalized['category']}",
                    f"outcome:{normalized['outcome']}",
                ],
                "metadata": {"learning": metadata_learning},
            }
        )

    @classmethod
    def _normalize_learning_fields(
        cls,
        *,
        project_id: str,
        title: str,
        lesson: str,
        category: str,
        outcome: str,
        external_id: str | None,
        harness: str | None,
        task_id: str | None,
        session_id: str | None,
        evidence_activity_ids: list[str] | None,
    ) -> dict[str, Any]:
        project_id = cls._required_string({"project_id": project_id}, "project_id")
        title = cls._required_string({"title": title}, "title")
        lesson = cls._required_string({"lesson": lesson}, "lesson")
        category = cls._required_string({"category": category}, "category")
        outcome = cls._required_string({"outcome": outcome}, "outcome")
        if len(project_id) > 200:
            raise WorkActivityValidationError("project_id must be at most 200 characters")
        if len(title) > 200:
            raise WorkActivityValidationError("title must be at most 200 characters")
        if len(lesson) > 2000:
            raise WorkActivityValidationError("lesson must be at most 2000 characters")
        if _LEARNING_CREDENTIAL_RE.search(project_id) or _LEARNING_CREDENTIAL_RE.search(title) or _LEARNING_CREDENTIAL_RE.search(lesson):
            raise WorkActivityValidationError("learning text appears to contain a credential")
        if category not in {"successful_pattern", "failure_correction", "preference", "decision", "environment_fact"}:
            raise WorkActivityValidationError("category is not supported")
        if outcome not in {"success", "failure", "corrected", "neutral"}:
            raise WorkActivityValidationError("outcome is not supported")

        normalized: dict[str, str | None] = {}
        for field_name, value, max_length in (
            ("external_id", external_id, 200),
            ("harness", harness, 80),
            ("task_id", task_id, 160),
            ("session_id", session_id, 160),
        ):
            cleaned = cls._optional_string({field_name: value}, field_name)
            if cleaned and len(cleaned) > max_length:
                raise WorkActivityValidationError(f"{field_name} must be at most {max_length} characters")
            if cleaned and _LEARNING_CREDENTIAL_RE.search(cleaned):
                raise WorkActivityValidationError(f"{field_name} appears to contain a credential")
            normalized[field_name] = cleaned

        evidence: list[str] = []
        if evidence_activity_ids is not None:
            if not isinstance(evidence_activity_ids, list) or len(evidence_activity_ids) > 20:
                raise WorkActivityValidationError("evidence_activity_ids must contain at most 20 activity IDs")
            for activity_id in evidence_activity_ids:
                if not isinstance(activity_id, str) or not activity_id.strip() or len(activity_id.strip()) > 160:
                    raise WorkActivityValidationError(
                        "evidence_activity_ids must contain non-empty strings up to 160 characters"
                    )
                if _LEARNING_CREDENTIAL_RE.search(activity_id):
                    raise WorkActivityValidationError("evidence_activity_ids appears to contain a credential")
                if activity_id.strip() not in evidence:
                    evidence.append(activity_id.strip())

        learning_identity = normalized["external_id"]
        if learning_identity is None:
            identity_payload = json.dumps(
                [
                    project_id,
                    category,
                    title.casefold(),
                    normalized["harness"],
                    normalized["task_id"],
                    normalized["session_id"],
                ],
                separators=(",", ":"),
            )
            learning_identity = hashlib.sha256(identity_payload.encode("utf-8")).hexdigest()[:32]

        return {
            "project_id": project_id,
            "title": title,
            "lesson": lesson,
            "category": category,
            "outcome": outcome,
            "harness": normalized["harness"],
            "task_id": normalized["task_id"],
            "session_id": normalized["session_id"],
            "evidence_activity_ids": evidence,
            "learning_identity": learning_identity,
        }

    @classmethod
    def _validate_learning_payload(cls, payload: dict[str, Any]) -> None:
        expected_fields = {
            "project_id",
            "event_type",
            "external_id",
            "title",
            "summary",
            "body_text",
            "status",
            "tags",
            "metadata",
        }
        if set(payload) != expected_fields:
            raise WorkActivityValidationError("learning record fields do not match the supported schema")
        if payload.get("event_type") != "agent_learning":
            raise WorkActivityValidationError("event_type is not a learning record")
        if payload.get("status") != "candidate" or payload.get("body_text") != "":
            raise WorkActivityValidationError("learning records must remain bounded candidates")

        external_id = cls._required_string(payload, "external_id")
        if not external_id.startswith("learning:") or not external_id[len("learning:"):]:
            raise WorkActivityValidationError("learning identity is invalid")
        learning_id = external_id[len("learning:"):]
        metadata = payload.get("metadata")
        learning = metadata.get("learning") if isinstance(metadata, dict) else None
        expected_learning_fields = {
            "schema_version",
            "category",
            "outcome",
            "harness",
            "task_id",
            "session_id",
            "evidence_activity_ids",
            "lifecycle",
        }
        if (
            not isinstance(metadata, dict)
            or set(metadata) != {"learning"}
            or not isinstance(learning, dict)
            or set(learning) != expected_learning_fields
            or type(learning.get("schema_version")) is not int
            or learning["schema_version"] != 1
            or learning.get("lifecycle") != "candidate"
        ):
            raise WorkActivityValidationError("learning metadata does not match the supported candidate schema")

        normalized = cls._normalize_learning_fields(
            project_id=payload.get("project_id"),
            title=payload.get("title"),
            lesson=payload.get("summary"),
            category=learning.get("category"),
            outcome=learning.get("outcome"),
            external_id=learning_id,
            harness=learning.get("harness"),
            task_id=learning.get("task_id"),
            session_id=learning.get("session_id"),
            evidence_activity_ids=learning.get("evidence_activity_ids"),
        )
        if (
            normalized["learning_identity"] != learning_id
            or payload.get("tags")
            != [
                "learning",
                f"category:{normalized['category']}",
                f"outcome:{normalized['outcome']}",
            ]
            or learning
            != {
                "schema_version": 1,
                "category": normalized["category"],
                "outcome": normalized["outcome"],
                "harness": normalized["harness"],
                "task_id": normalized["task_id"],
                "session_id": normalized["session_id"],
                "evidence_activity_ids": normalized["evidence_activity_ids"],
                "lifecycle": "candidate",
            }
        ):
            raise WorkActivityValidationError("learning metadata or provenance is invalid")

    @classmethod
    def _is_valid_learning_activity(cls, activity: WorkActivity) -> bool:
        if activity.event_type != "agent_learning" or activity.workspace_name is not None:
            return False
        if activity.owner is not None or activity.priority is not None or activity.source_url is not None:
            return False
        try:
            cls._validate_learning_payload(
                {
                    "project_id": activity.project_id,
                    "event_type": activity.event_type,
                    "external_id": activity.source_item_id,
                    "title": activity.title,
                    "summary": activity.summary,
                    "body_text": activity.body_text,
                    "status": activity.status,
                    "tags": list(activity.tags),
                    "metadata": activity.raw_metadata,
                }
            )
        except WorkActivityValidationError:
            return False
        return True

    @classmethod
    def _is_safe_activity(cls, activity: WorkActivity) -> bool:
        if not isinstance(activity.source_item_id, str):
            return False
        if activity.event_type == "agent_learning" or activity.source_item_id.startswith("learning:"):
            return cls._is_valid_learning_activity(activity)
        return True

    async def list_recent(self, project_id: str | None = None, limit: int = 25) -> list[WorkActivity]:
        activities = await self._store.list_recent(project_id=project_id, limit=limit)
        return [activity for activity in activities if self._is_safe_activity(activity)]

    async def get_learning_context(self, project_id: str, limit: int = 25) -> dict[str, Any]:
        activities = await self._store.list_recent(
            project_id=project_id,
            limit=max(1, min(int(limit), 100)),
            event_type="agent_learning",
        )
        learnings = []
        for activity in activities:
            if not self._is_valid_learning_activity(activity):
                continue
            item = self._to_memory_item(activity)
            learning = activity.raw_metadata.get("learning")
            if isinstance(learning, dict):
                item["learning"] = {
                    key: learning.get(key)
                    for key in (
                        "schema_version",
                        "category",
                        "outcome",
                        "harness",
                        "task_id",
                        "session_id",
                        "evidence_activity_ids",
                        "lifecycle",
                    )
                }
            learnings.append(item)
        return {"project_id": project_id, "count": len(learnings), "learnings": learnings}

    async def count_recent(self, project_id: str | None = None) -> int:
        return await self._store.count_recent(project_id=project_id)

    async def report_rows(
        self, *, start: datetime, end: datetime, project_ids: list[str],
        event_type: str, status: str, search: str, limit: int,
    ) -> tuple[list[WorkActivity], bool]:
        rows = await self._store.report_rows(
            start=start, end=end, project_ids=project_ids, event_type=event_type,
            status=status, search=search, limit=limit + 1,
        )
        return [row for row in rows[:limit] if self._is_safe_activity(row)], len(rows) > limit
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
        memories = [
            self._to_memory_item(activity)
            for activity in result.activities
            if self._is_safe_activity(activity)
        ]
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