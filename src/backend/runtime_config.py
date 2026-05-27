"""Persistent admin runtime configuration helpers."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable


class RuntimeConfigError(ValueError):
    """Raised when a runtime configuration patch is invalid."""


MAX_REPOS_ROOT_LENGTH = 1024
_REPO_ROOT = Path(__file__).resolve().parents[2]
_LOCAL_REPOS_ROOT = _REPO_ROOT.parent


def _default_data_dir() -> Path:
    configured = os.getenv("CGA_RUNTIME_DATA_DIR", "").strip()
    if configured:
        return Path(configured)
    if Path("/app").exists():
        return Path("/app/data")
    return _REPO_ROOT / "data"


def _default_indexing_repos_root() -> Path:
    if Path("/app").exists() or Path("/repos").exists():
        return Path("/repos")
    return _LOCAL_REPOS_ROOT


RUNTIME_CONFIG_PATH = Path(
    os.getenv("CGA_RUNTIME_CONFIG_PATH", str(_default_data_dir() / "runtime-config.json"))
)


def _normalize_repos_root(value: Any) -> str:
    if not isinstance(value, str):
        raise RuntimeConfigError("indexing.repos_root must be a string")
    cleaned = value.strip()
    if not cleaned:
        raise RuntimeConfigError("indexing.repos_root is required")
    if len(cleaned) > MAX_REPOS_ROOT_LENGTH:
        raise RuntimeConfigError(f"indexing.repos_root must be <= {MAX_REPOS_ROOT_LENGTH} characters")
    return cleaned


def _read_raw_config() -> dict[str, Any]:
    if not RUNTIME_CONFIG_PATH.is_file():
        return {}
    try:
        with RUNTIME_CONFIG_PATH.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise RuntimeConfigError(f"runtime config is not valid JSON: {exc}") from exc
    except OSError as exc:
        raise RuntimeConfigError(f"runtime config could not be read: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeConfigError("runtime config root must be an object")
    return data


def _write_raw_config(data: dict[str, Any]) -> None:
    try:
        RUNTIME_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = RUNTIME_CONFIG_PATH.with_name(f"{RUNTIME_CONFIG_PATH.name}.tmp")
        with tmp_path.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        tmp_path.replace(RUNTIME_CONFIG_PATH)
    except OSError as exc:
        raise RuntimeConfigError(f"runtime config could not be saved: {exc}") from exc


def _environment_repos_root() -> str | None:
    for name in ("CGA_INDEXING_REPOS_ROOT", "CONTEXTGRAPH_REPOS_ROOT"):
        value = os.getenv(name, "").strip()
        if value:
            return _normalize_repos_root(value)
    return None


def _configured_repos_root(raw: dict[str, Any] | None = None) -> tuple[str | None, str]:
    data = raw if raw is not None else _read_raw_config()
    indexing = data.get("indexing") if isinstance(data, dict) else None
    if isinstance(indexing, dict) and "repos_root" in indexing:
        return _normalize_repos_root(indexing.get("repos_root")), "saved"

    env_root = _environment_repos_root()
    if env_root:
        return env_root, "environment"
    return None, "default"


def get_runtime_config(default_repos_root: str | Path | None = None) -> dict[str, Any]:
    raw = _read_raw_config()
    configured_root, source = _configured_repos_root(raw)
    default_root = str(default_repos_root or _default_indexing_repos_root())
    repos_root = configured_root or default_root
    return {
        "indexing": {
            "repos_root": repos_root,
            "repos_root_source": source,
            "default_repos_root": default_root,
            "repos_root_exists": Path(repos_root).exists(),
        },
        "runtime_config_path": str(RUNTIME_CONFIG_PATH),
    }


def update_runtime_config(patch: dict[str, Any] | None) -> dict[str, Any]:
    if patch is None:
        patch = {}
    if not isinstance(patch, dict):
        raise RuntimeConfigError("runtime config patch must be an object")

    raw = _read_raw_config()
    if "indexing" in patch:
        indexing_patch = patch.get("indexing")
        if not isinstance(indexing_patch, dict):
            raise RuntimeConfigError("indexing must be an object")
        indexing = raw.get("indexing") if isinstance(raw.get("indexing"), dict) else {}
        if "repos_root" in indexing_patch:
            indexing["repos_root"] = _normalize_repos_root(indexing_patch.get("repos_root"))
        raw["indexing"] = indexing

    _write_raw_config(raw)
    return get_runtime_config()


def get_indexing_repo_search_roots(default_roots: Iterable[Path] | None = None) -> list[Path]:
    roots: list[Path] = []
    configured_root, _ = _configured_repos_root()
    if configured_root:
        roots.append(Path(configured_root))
    if default_roots:
        roots.extend(Path(root) for root in default_roots)

    deduped: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root).replace("\\", "/").lower()
        if key in seen:
            continue
        deduped.append(root)
        seen.add(key)
    return deduped