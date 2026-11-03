from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class AgentLearningRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str | None = None
    title: str = Field(min_length=1, max_length=200)
    lesson: str = Field(min_length=1, max_length=2000)
    category: Literal[
        "successful_pattern",
        "failure_correction",
        "preference",
        "decision",
        "environment_fact",
    ]
    outcome: Literal["success", "failure", "corrected", "neutral"]
    external_id: str | None = Field(default=None, max_length=200)
    harness: str | None = Field(default=None, max_length=80)
    task_id: str | None = Field(default=None, max_length=160)
    session_id: str | None = Field(default=None, max_length=160)
    evidence_activity_ids: list[str] | None = Field(default=None, max_length=20)


@dataclass(frozen=True)
class WorkActivity:
    activity_id: str
    plugin_id: str
    source_system: str
    source_type: str
    source_item_id: str
    project_id: str
    workspace_name: str | None
    event_type: str
    title: str
    summary: str
    body_text: str
    status: str | None
    priority: str | None
    owner: str | None
    source_url: str | None
    tags: tuple[str, ...]
    occurred_at: datetime
    synced_at: datetime
    content_hash: str
    raw_metadata: dict[str, Any]
    embedding_text: str

    def to_dict(self) -> dict[str, Any]:
        activity = asdict(self)
        activity["tags"] = list(self.tags)
        activity["occurred_at"] = self.occurred_at.isoformat().replace("+00:00", "Z")
        activity["synced_at"] = self.synced_at.isoformat().replace("+00:00", "Z")
        return activity


def parse_utc_datetime(value: Any, field_name: str) -> datetime:
    if value is None or value == "":
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        normalized = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError(f"{field_name} must be an ISO 8601 datetime") from exc
    else:
        raise ValueError(f"{field_name} must be an ISO 8601 datetime")

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)