"""Pydantic models for CGA platform extensions."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


class ExtensionDefinition(BaseModel):
    extension_id: str
    name: str
    description: str = ""
    version: str = "0.1.0"
    capabilities: list[str] = Field(default_factory=list)
    default_config: dict[str, Any] = Field(default_factory=dict)


class ExtensionConfigBase(BaseModel):
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)


class ExtensionConfigUpdate(ExtensionConfigBase):
    @field_validator("config")
    @classmethod
    def validate_config(cls, value: dict[str, Any]) -> dict[str, Any]:
        return dict(value or {})


class ExtensionConfigOut(ExtensionConfigBase):
    extension_id: str
    project_id: int | None = None
    project_name: str | None = None
    project_external_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class ExtensionRunRequest(BaseModel):
    config_override: dict[str, Any] = Field(default_factory=dict)


class ExtensionRunOut(BaseModel):
    id: int
    extension_id: str
    project_id: int | None = None
    project_name: str | None = None
    project_external_id: str | None = None
    schedule_id: int | None = None
    status: str
    severity: str = "info"
    started_at: str
    finished_at: str
    duration_ms: int = 0
    summary: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)


class ExtensionProjectOverview(BaseModel):
    definition: ExtensionDefinition
    config: ExtensionConfigOut | None = None
    last_run: ExtensionRunOut | None = None
    recent_runs: list[ExtensionRunOut] = Field(default_factory=list)


class ExtensionList(BaseModel):
    items: list[ExtensionDefinition]
