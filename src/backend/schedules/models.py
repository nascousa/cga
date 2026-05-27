"""Pydantic models for admin scheduled automation."""
from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

ScheduledTaskType = Literal["agent_activation", "browseragent_task", "http_request"]


class ScheduledTaskBase(BaseModel):
    name: str = Field(min_length=3, max_length=100)
    description: str = Field(default="", max_length=1000)
    task_type: ScheduledTaskType = "browseragent_task"
    project_id: int | None = Field(default=None, ge=1)
    agent_id: str = Field(default="", max_length=128)
    target_url: str = Field(default="", max_length=512)
    payload: dict[str, Any] = Field(default_factory=dict)
    cadence_minutes: int = Field(default=60, ge=1, le=43_200)
    timeout_seconds: int = Field(default=30, ge=1, le=600)
    enabled: bool = True

    @field_validator("name", "description", "agent_id", "target_url")
    @classmethod
    def trim_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("target_url")
    @classmethod
    def validate_target_url(cls, value: str) -> str:
        if not value:
            return value
        if not (value.startswith("http://") or value.startswith("https://")):
            raise ValueError("target_url must start with http:// or https://")
        return value

    @field_validator("payload")
    @classmethod
    def validate_payload_json(cls, value: dict[str, Any]) -> dict[str, Any]:
        try:
            json.dumps(value, sort_keys=True, ensure_ascii=True)
        except TypeError as exc:
            raise ValueError("payload must be JSON serializable") from exc
        return value

    @model_validator(mode="after")
    def validate_enabled_target(self) -> "ScheduledTaskBase":
        if self.enabled and not self.target_url:
            raise ValueError("target_url is required for enabled scheduled tasks")
        return self


class ScheduledTaskCreate(ScheduledTaskBase):
    pass


class ScheduledTaskUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=3, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    task_type: ScheduledTaskType | None = None
    project_id: int | None = Field(default=None, ge=1)
    agent_id: str | None = Field(default=None, max_length=128)
    target_url: str | None = Field(default=None, max_length=512)
    payload: dict[str, Any] | None = None
    cadence_minutes: int | None = Field(default=None, ge=1, le=43_200)
    timeout_seconds: int | None = Field(default=None, ge=1, le=600)
    enabled: bool | None = None

    @field_validator("name", "description", "agent_id", "target_url")
    @classmethod
    def trim_optional_text(cls, value: str | None) -> str | None:
        return value.strip() if isinstance(value, str) else value

    @field_validator("target_url")
    @classmethod
    def validate_optional_target_url(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return value
        if not (value.startswith("http://") or value.startswith("https://")):
            raise ValueError("target_url must start with http:// or https://")
        return value

    @field_validator("payload")
    @classmethod
    def validate_optional_payload_json(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return value
        try:
            json.dumps(value, sort_keys=True, ensure_ascii=True)
        except TypeError as exc:
            raise ValueError("payload must be JSON serializable") from exc
        return value


class ScheduledTaskOut(BaseModel):
    id: int
    task_id: str
    name: str
    description: str = ""
    task_type: ScheduledTaskType
    project_id: int | None = None
    project_name: str | None = None
    project_external_id: str | None = None
    agent_id: str = ""
    target_url: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    cadence_minutes: int
    timeout_seconds: int
    enabled: bool
    created_at: str
    updated_at: str
    next_run_at: str
    last_run_at: str | None = None
    last_run_status: str = ""
    last_run_error: str = ""


class ScheduledTaskRunOut(BaseModel):
    id: int
    schedule_id: int
    task_id: str | None = None
    started_at: str
    finished_at: str
    status: str
    status_code: int | None = None
    duration_ms: int = 0
    error: str = ""
    response: dict[str, Any] = Field(default_factory=dict)


class ScheduledTaskList(BaseModel):
    items: list[ScheduledTaskOut]
    recent_runs: list[ScheduledTaskRunOut] = Field(default_factory=list)
