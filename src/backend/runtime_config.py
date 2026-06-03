"""Persistent admin runtime configuration helpers."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable


class RuntimeConfigError(ValueError):
    """Raised when a runtime configuration patch is invalid."""


MAX_REPOS_ROOT_LENGTH = 1024
MAX_SMTP_HOST_LENGTH = 255
MAX_SMTP_TEXT_LENGTH = 1024
MAX_SMTP_SECRET_LENGTH = 4096
MAX_SMTP_EMAIL_LENGTH = 320
SMTP_SECURITY_MODES = {"none", "starttls", "ssl"}
RUNTIME_MODULE_DEFAULTS = {
    "indexer_consumer": True,
    "schedule_worker": True,
    "backup_scheduler": True,
}
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


def _normalize_bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if cleaned in {"1", "true", "yes", "on"}:
            return True
        if cleaned in {"0", "false", "no", "off"}:
            return False
    raise RuntimeConfigError(f"{field} must be a boolean")


def _normalize_text(value: Any, field: str, *, max_length: int, strip: bool = True) -> str:
    if not isinstance(value, str):
        raise RuntimeConfigError(f"{field} must be a string")
    cleaned = value.strip() if strip else value
    if len(cleaned) > max_length:
        raise RuntimeConfigError(f"{field} must be <= {max_length} characters")
    return cleaned


def _normalize_port(value: Any) -> int:
    if isinstance(value, bool):
        raise RuntimeConfigError("smtp.port must be an integer")
    if isinstance(value, int):
        port = value
    elif isinstance(value, str) and value.strip().isdigit():
        port = int(value.strip())
    else:
        raise RuntimeConfigError("smtp.port must be an integer")
    if port < 1 or port > 65535:
        raise RuntimeConfigError("smtp.port must be between 1 and 65535")
    return port


def _normalize_smtp_security(value: Any) -> str:
    if not isinstance(value, str):
        raise RuntimeConfigError("smtp.security must be a string")
    cleaned = value.strip().lower()
    if cleaned not in SMTP_SECURITY_MODES:
        raise RuntimeConfigError("smtp.security must be one of: none, starttls, ssl")
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


def get_module_config(raw: dict[str, Any] | None = None) -> dict[str, dict[str, bool]]:
    data = raw if raw is not None else _read_raw_config()
    modules = data.get("modules") if isinstance(data.get("modules"), dict) else {}
    result: dict[str, dict[str, bool]] = {}
    for key, default_enabled in RUNTIME_MODULE_DEFAULTS.items():
        raw_value = modules.get(key)
        if raw_value is None:
            enabled = default_enabled
        elif isinstance(raw_value, dict):
            enabled = _normalize_bool(raw_value.get("enabled", default_enabled), f"modules.{key}.enabled")
        else:
            enabled = _normalize_bool(raw_value, f"modules.{key}.enabled")
        result[key] = {"enabled": enabled, "default_enabled": default_enabled}
    return result


def is_module_enabled(key: str, raw: dict[str, Any] | None = None) -> bool:
    if key not in RUNTIME_MODULE_DEFAULTS:
        raise RuntimeConfigError(f"unknown runtime module: {key}")
    return get_module_config(raw).get(key, {"enabled": True})["enabled"]


def _normalize_modules_patch(value: Any) -> dict[str, dict[str, bool]]:
    if not isinstance(value, dict):
        raise RuntimeConfigError("modules must be an object")
    normalized: dict[str, dict[str, bool]] = {}
    for key, raw_value in value.items():
        if key not in RUNTIME_MODULE_DEFAULTS:
            raise RuntimeConfigError(f"unknown runtime module: {key}")
        if isinstance(raw_value, dict):
            if "enabled" not in raw_value:
                continue
            enabled = _normalize_bool(raw_value.get("enabled"), f"modules.{key}.enabled")
        else:
            enabled = _normalize_bool(raw_value, f"modules.{key}.enabled")
        normalized[key] = {"enabled": enabled}
    return normalized


def _smtp_raw(raw: dict[str, Any]) -> dict[str, Any]:
    smtp = raw.get("smtp")
    if smtp is None:
        return {}
    if not isinstance(smtp, dict):
        raise RuntimeConfigError("smtp must be an object")
    return smtp


def _public_smtp_config(raw: dict[str, Any]) -> dict[str, Any]:
    smtp = _smtp_raw(raw)
    enabled = _normalize_bool(smtp.get("enabled", False), "smtp.enabled") if "enabled" in smtp else False
    return {
        "enabled": enabled,
        "host": _normalize_text(smtp.get("host", ""), "smtp.host", max_length=MAX_SMTP_HOST_LENGTH),
        "port": _normalize_port(smtp.get("port", 587)),
        "security": _normalize_smtp_security(smtp.get("security", "starttls")),
        "username": _normalize_text(smtp.get("username", ""), "smtp.username", max_length=MAX_SMTP_TEXT_LENGTH),
        "from_email": _normalize_text(smtp.get("from_email", ""), "smtp.from_email", max_length=MAX_SMTP_EMAIL_LENGTH),
        "from_name": _normalize_text(smtp.get("from_name", "ContextGraphAgent"), "smtp.from_name", max_length=MAX_SMTP_TEXT_LENGTH),
        "password_set": bool(smtp.get("password")),
    }


def _normalize_smtp_patch(value: Any, existing: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeConfigError("smtp must be an object")

    smtp = dict(existing)
    if "enabled" in value:
        smtp["enabled"] = _normalize_bool(value.get("enabled"), "smtp.enabled")
    if "host" in value:
        smtp["host"] = _normalize_text(value.get("host"), "smtp.host", max_length=MAX_SMTP_HOST_LENGTH)
    if "port" in value:
        smtp["port"] = _normalize_port(value.get("port"))
    if "security" in value:
        smtp["security"] = _normalize_smtp_security(value.get("security"))
    if "username" in value:
        smtp["username"] = _normalize_text(value.get("username"), "smtp.username", max_length=MAX_SMTP_TEXT_LENGTH)
    if "from_email" in value:
        smtp["from_email"] = _normalize_text(
            value.get("from_email"), "smtp.from_email", max_length=MAX_SMTP_EMAIL_LENGTH
        )
    if "from_name" in value:
        smtp["from_name"] = _normalize_text(value.get("from_name"), "smtp.from_name", max_length=MAX_SMTP_TEXT_LENGTH)
    if "password" in value:
        password = _normalize_text(
            value.get("password"), "smtp.password", max_length=MAX_SMTP_SECRET_LENGTH, strip=False
        )
        if password:
            smtp["password"] = password
    if _normalize_bool(value.get("clear_password"), "smtp.clear_password") if "clear_password" in value else False:
        smtp.pop("password", None)

    public = _public_smtp_config({"smtp": smtp})
    if public["enabled"]:
        if not public["host"]:
            raise RuntimeConfigError("smtp.host is required when SMTP is enabled")
        if not public["from_email"]:
            raise RuntimeConfigError("smtp.from_email is required when SMTP is enabled")
    smtp.update(
        {
            "enabled": public["enabled"],
            "host": public["host"],
            "port": public["port"],
            "security": public["security"],
            "username": public["username"],
            "from_email": public["from_email"],
            "from_name": public["from_name"],
        }
    )
    return smtp


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
        "modules": get_module_config(raw),
        "smtp": _public_smtp_config(raw),
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

    if "modules" in patch:
        modules = raw.get("modules") if isinstance(raw.get("modules"), dict) else {}
        for key, module_patch in _normalize_modules_patch(patch.get("modules")).items():
            existing = modules.get(key) if isinstance(modules.get(key), dict) else {}
            modules[key] = {**existing, **module_patch}
        raw["modules"] = modules

    if "smtp" in patch:
        raw["smtp"] = _normalize_smtp_patch(patch.get("smtp"), _smtp_raw(raw))

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