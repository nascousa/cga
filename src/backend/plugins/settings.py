"""Validated plugin defaults, separate from installation and existing shared settings."""
from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend import runtime_config
from backend.extensions.registry import _reject_inline_secrets
from backend.plugins import host


class SettingsModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class BrowserSettings(SettingsModel):
    target_url: str = "http://localhost:9876/command/batch"
    timeout_seconds: int = Field(default=30, ge=1, le=600)
    screenshot: bool = True
    console: bool = True
    metrics: bool = True
    dom_snapshot: bool = False

    @field_validator("target_url")
    @classmethod
    def endpoint(cls, value: str) -> str:
        endpoint = validate_endpoint(value)
        if not endpoint:
            raise ValueError("BrowserAgent endpoint is required")
        return endpoint


class WorkflowSettings(SettingsModel):
    agent_url: str = "http://localhost:3002/activate"
    http_url: str = ""
    timeout_seconds: int = Field(default=30, ge=1, le=600)
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("agent_url", "http_url")
    @classmethod
    def endpoint(cls, value: str) -> str:
        return validate_endpoint(value)

    @field_validator("payload")
    @classmethod
    def request_template(cls, value: dict[str, Any]) -> dict[str, Any]:
        _reject_inline_secrets(value, path="payload")
        if len(json.dumps(value, allow_nan=False).encode("utf-8")) > 16384:
            raise ValueError("Request template exceeds 16 KiB")
        return value


class BriefingSettings(SettingsModel):
    project_id: str = Field(default="", max_length=128)
    refresh_seconds: int = Field(default=5, ge=0, le=3600)
    batch_size: int = Field(default=25, ge=1, le=25)


class EvaluationSettings(SettingsModel):
    trace_enabled: bool = True


MODELS: dict[str, type[SettingsModel]] = {
    "browseragent": BrowserSettings, "workflows": WorkflowSettings,
    "briefing": BriefingSettings, "evaluation": EvaluationSettings,
}


def validate_endpoint(value: str) -> str:
    value = value.strip()
    if not value:
        return value
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("Use an HTTP(S) endpoint without credentials, query strings or fragments")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("Endpoint port is invalid") from exc
    return value


def get_settings(plugin_id: str) -> dict[str, Any]:
    model = MODELS[plugin_id]
    raw = runtime_config._read_raw_config()
    settings = raw.get("plugin_settings", {})
    if not isinstance(settings, dict):
        raise runtime_config.RuntimeConfigError("plugin_settings must be an object")
    return model.model_validate(settings.get(plugin_id, {})).model_dump()


def save_settings(plugin_id: str, values: dict[str, Any]) -> dict[str, Any]:
    validated = MODELS[plugin_id].model_validate(values).model_dump()
    with host._lock:
        raw = runtime_config._read_raw_config()
        settings = raw.get("plugin_settings", {})
        if not isinstance(settings, dict):
            raise runtime_config.RuntimeConfigError("plugin_settings must be an object")
        raw["plugin_settings"] = {**settings, plugin_id: validated}
        runtime_config._write_raw_config(raw)
    return validated


def tracing_enabled() -> bool:
    return os.getenv("TRACE_ENABLED", "true").lower() == "true" and get_settings("evaluation")["trace_enabled"]
