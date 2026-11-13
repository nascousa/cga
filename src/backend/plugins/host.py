"""Persistent plugin selection and fail-closed execution leases."""
from __future__ import annotations

import os
import json
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict
from threading import RLock
from pathlib import Path
from typing import Iterator

from fastapi import HTTPException

from backend import runtime_config
from backend.plugins.catalog import CATALOG, TASK_PLUGINS

_lock = RLock()
_active: Counter[str] = Counter()


class PluginUnavailableError(ValueError):
    pass


def _ids(value: object, field: str) -> set[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise runtime_config.RuntimeConfigError(f"{field} must be an array of plugin IDs")
    if len(set(value)) != len(value):
        raise runtime_config.RuntimeConfigError(f"{field} contains duplicate plugin IDs")
    unknown = set(value) - CATALOG.keys()
    if unknown:
        raise runtime_config.RuntimeConfigError(f"Unknown plugins in {field}: {', '.join(sorted(unknown))}")
    return set(value)


def available_plugins() -> set[str]:
    manifest = Path(__file__).resolve().parents[3] / "plugin-bundle.json"
    bundled = _ids(json.loads(manifest.read_text(encoding="utf-8")), "plugin bundle") if manifest.is_file() else set(CATALOG)
    configured = os.getenv("CGA_AVAILABLE_PLUGINS")
    if configured is None:
        return bundled
    requested = _ids([item.strip() for item in configured.split(",") if item.strip()], "CGA_AVAILABLE_PLUGINS")
    if not requested <= bundled:
        raise runtime_config.RuntimeConfigError("CGA_AVAILABLE_PLUGINS includes plugins absent from the distribution")
    return requested


def selection(raw: dict | None = None) -> dict[str, list[str]]:
    data = runtime_config._read_raw_config() if raw is None else raw
    value = data.get("plugins")
    if value is None:
        initial = os.getenv("CGA_PLUGINS")
        installed = available_plugins() if initial is None else _ids(
            [item.strip() for item in initial.split(",") if item.strip()], "CGA_PLUGINS"
        )
        value = {"installed": sorted(installed), "enabled": sorted(installed)}
        return validate_selection(value)
    saved = validate_selection(value, available_only=False)
    available = available_plugins()
    return {key: sorted(set(ids) & available) for key, ids in saved.items()}


def validate_selection(value: object, *, available_only: bool = True) -> dict[str, list[str]]:
    if not isinstance(value, dict) or set(value) != {"installed", "enabled"}:
        raise runtime_config.RuntimeConfigError("plugins requires exactly installed and enabled arrays")
    installed = _ids(value["installed"], "plugins.installed")
    enabled = _ids(value["enabled"], "plugins.enabled")
    if not enabled <= installed:
        raise runtime_config.RuntimeConfigError("Enabled plugins must be installed")
    if available_only and not installed <= available_plugins():
        raise runtime_config.RuntimeConfigError("Selected plugins are not included in this distribution")
    return {"installed": sorted(installed), "enabled": sorted(enabled)}


def is_enabled(plugin_id: str) -> bool:
    if plugin_id not in CATALOG:
        raise PluginUnavailableError(f"Unknown plugin: {plugin_id}")
    return plugin_id in selection()["enabled"]


def require_enabled(plugin_id: str) -> None:
    if not is_enabled(plugin_id):
        raise PluginUnavailableError(f"Plugin '{plugin_id}' is not installed or is disabled")


@contextmanager
def execution(plugin_id: str) -> Iterator[None]:
    with _lock:
        require_enabled(plugin_id)
        _active[plugin_id] += 1
    try:
        yield
    finally:
        with _lock:
            _active[plugin_id] -= 1


def require_plugin(plugin_id: str):
    async def dependency():
        try:
            with execution(plugin_id):
                yield
        except PluginUnavailableError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return dependency


def require_installed(plugin_id: str):
    async def dependency():
        if plugin_id not in selection()["installed"]:
            raise HTTPException(status_code=409, detail=f"Plugin '{plugin_id}' is not installed")
    return dependency


def catalog_status() -> dict:
    with _lock:
        current = selection()
        available = available_plugins()
        saved = runtime_config._read_raw_config().get("plugins")
        retained = validate_selection(saved, available_only=False) if saved is not None else current
        return {
            "items": [
                {
                    **asdict(plugin), "version": "1.0.0",
                    "available": plugin.id in available,
                    "installed": plugin.id in current["installed"],
                    "enabled": plugin.id in current["enabled"],
                    "active_executions": _active[plugin.id],
                }
                for plugin in CATALOG.values()
            ],
            "selection": current,
            "unavailable_selection": {
                key: sorted(set(ids) - available) for key, ids in retained.items()
            },
            "core": ["adc", "audit", "identity", "projects", "graph", "mcp", "scheduler", "backup"],
        }


def update_selection(value: dict) -> dict:
    with _lock:
        desired = validate_selection(value)
        raw = runtime_config._read_raw_config()
        current = selection(raw)
        stopping = set(current["enabled"]) - set(desired["enabled"])
        busy = sorted(plugin_id for plugin_id in stopping if _active[plugin_id])
        if busy:
            raise PluginUnavailableError(f"Wait for active executions before disabling: {', '.join(busy)}")
        retained = validate_selection(raw["plugins"], available_only=False) if "plugins" in raw else current
        available = available_plugins()
        raw["plugins"] = {
            key: sorted(set(desired[key]) | (set(retained[key]) - available))
            for key in desired
        }
        runtime_config._write_raw_config(raw)
        return catalog_status()


def task_plugin(task_type: str, payload: dict) -> str:
    if task_type == "extension_task":
        plugin_id = str(payload.get("extension_id") or "")
        if plugin_id not in CATALOG:
            raise PluginUnavailableError(f"Unknown extension task plugin: {plugin_id}")
        return plugin_id
    try:
        return TASK_PLUGINS[task_type]
    except KeyError as exc:
        raise PluginUnavailableError(f"Unknown task type: {task_type}") from exc
