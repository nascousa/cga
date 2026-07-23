"""Registry and adapters for CGA extensions."""
from __future__ import annotations

import sys
import re
import urllib.parse
from email.utils import parseaddr
from pathlib import Path
from typing import Any

from backend.extensions.models import ExtensionDefinition

AZURE_POLICY_EXTENSION_ID = "azure_policy_change_monitor"
WINDOWS_REPOS_PATH = re.compile(r"^[A-Za-z]:[\\/]+Repos(?:[\\/]+(?P<tail>.*))?$")
FORBIDDEN_INLINE_SECRET_KEYS = {
    "access_token",
    "api_key",
    "assertion",
    "azure_client_secret",
    "client_assertion",
    "client_secret",
    "credential",
    "credentials",
    "model_api_key",
    "notification_webhook_url",
    "password",
    "refresh_token",
    "secret",
    "smtp_password",
    "token",
    "webhook_url",
}
ENVIRONMENT_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
CONFIG_ENUMS = {
    "auth_mode": {"auto", "environment", "workload_identity", "managed_identity", "azure_cli"},
    "model_auth_mode": {"auto", "api_key", "azure"},
    "model_api_key_header": {"api-key", "authorization"},
    "notification_min_severity": {"info", "low", "medium", "high", "critical"},
}
CONFIG_INTEGER_RANGES = {
    "activity_lookback_minutes": (1, 43_200),
    "snapshot_retention_count": (2, 1_000),
    "azure_timeout_seconds": (1, 120),
    "azure_max_attempts": (1, 8),
    "max_collection_items": (1, 250_000),
    "model_timeout_seconds": (1, 180),
    "model_max_attempts": (1, 5),
    "notification_timeout_seconds": (1, 60),
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _azure_policy_src_path() -> Path:
    return _repo_root() / "extensions" / "azure-policy-monitor" / "src"


def _ensure_extension_path(path: Path) -> None:
    text = str(path)
    if text not in sys.path:
        sys.path.insert(0, text)


def _normalize_repo_path(repo_path: str, repos_mount: Path = Path("/repos")) -> str:
    text = str(repo_path or "").strip()
    match = WINDOWS_REPOS_PATH.match(text)
    if not match or not repos_mount.exists():
        return text
    mapped = repos_mount
    tail = (match.group("tail") or "").replace("\\", "/").strip("/")
    for part in tail.split("/"):
        if part:
            mapped = mapped / part
    return str(mapped)


EXTENSIONS: dict[str, ExtensionDefinition] = {
    AZURE_POLICY_EXTENSION_ID: ExtensionDefinition(
        extension_id=AZURE_POLICY_EXTENSION_ID,
        name="Azure Policy Change Monitor",
        description="Monitors Azure Policy repositories, deployed policy drift, compliance, and control-plane activity.",
        version="1.0.0",
        capabilities=[
            "policy_repo_scan",
            "folder_parity",
            "guid_consistency",
            "version_consistency",
            "effect_risk",
            "azure_policy_inventory",
            "assignment_drift",
            "compliance_drift",
            "activity_log_monitoring",
            "snapshot_history",
            "evidence_grounded_summary",
            "severity_notifications",
        ],
        default_config={
            "repo_scan_enabled": True,
            "policy_root": "settings/BuiltInPoliciesV2",
            "cloud_folders": ["AllEnvironments", "USNat", "USSec"],
            "baseline_folder": "AllEnvironments",
            "azure_monitor_enabled": False,
            "subscription_id": "",
            "management_group_id": "",
            "azure_scope": "",
            "auth_mode": "auto",
            "management_endpoint": "https://management.azure.com",
            "authority_host": "https://login.microsoftonline.com",
            "arm_token_scope": "https://management.azure.com/.default",
            "managed_identity_client_id": "",
            "activity_subscription_ids": [],
            "include_compliance": True,
            "include_activity": True,
            "activity_lookback_minutes": 1440,
            "snapshot_retention_count": 90,
            "azure_timeout_seconds": 30,
            "azure_max_attempts": 4,
            "max_collection_items": 50000,
            "model_summary_enabled": False,
            "model_endpoint": "",
            "model_name": "",
            "model_deployment": "",
            "model_api_version": "",
            "model_auth_mode": "auto",
            "model_api_key_env": "AZURE_POLICY_MONITOR_MODEL_API_KEY",
            "model_api_key_header": "api-key",
            "model_token_scope": "https://cognitiveservices.azure.com/.default",
            "model_timeout_seconds": 60,
            "model_max_attempts": 3,
            "notifications_enabled": False,
            "notification_min_severity": "high",
            "notification_webhook_env": "",
            "notification_email_recipients": [],
            "notification_timeout_seconds": 20,
            "read_only": True,
        },
    )
}


def list_extension_definitions() -> list[ExtensionDefinition]:
    return list(EXTENSIONS.values())


def get_extension_definition(extension_id: str) -> ExtensionDefinition | None:
    return EXTENSIONS.get(extension_id)


def validate_extension_config(
    extension_id: str,
    config: dict[str, Any],
    *,
    require_complete: bool = False,
) -> None:
    if extension_id != AZURE_POLICY_EXTENSION_ID:
        return
    _reject_inline_secrets(config, path="config")
    for field, allowed in CONFIG_ENUMS.items():
        if field in config and str(config.get(field) or "").strip().lower() not in allowed:
            raise ValueError(f"Extension configuration field {field} is invalid.")
    for field, (minimum, maximum) in CONFIG_INTEGER_RANGES.items():
        if field in config:
            _config_integer(config.get(field), field, minimum, maximum)
    for field in ("model_api_key_env", "notification_webhook_env"):
        value = str(config.get(field) or "").strip()
        if value and not ENVIRONMENT_NAME.fullmatch(value):
            raise ValueError(f"Extension configuration field {field} must be an uppercase environment variable name.")
    for field in ("management_endpoint", "authority_host", "model_endpoint"):
        value = str(config.get(field) or "").strip()
        if value:
            _validate_https_config_url(value, field)

    if not require_complete:
        return
    if _config_bool(config.get("azure_monitor_enabled"), False) and not any(
        str(config.get(field) or "").strip()
        for field in ("subscription_id", "management_group_id", "azure_scope")
    ):
        raise ValueError("Azure monitoring requires subscription_id, management_group_id, or azure_scope.")
    azure_scope = str(config.get("azure_scope") or "").strip().lower()
    management_group_scope = bool(str(config.get("management_group_id") or "").strip()) or azure_scope.startswith(
        "/providers/microsoft.management/managementgroups/"
    )
    if (
        _config_bool(config.get("azure_monitor_enabled"), False)
        and _config_bool(config.get("include_activity"), True)
        and management_group_scope
        and not _string_values(config.get("activity_subscription_ids"))
    ):
        raise ValueError("Management-group Activity Log monitoring requires activity_subscription_ids.")
    if _config_bool(config.get("model_summary_enabled"), False):
        if not str(config.get("model_endpoint") or "").strip():
            raise ValueError("Model summaries require model_endpoint.")
        if str(config.get("model_auth_mode") or "auto").strip().lower() == "api_key" and not str(
            config.get("model_api_key_env") or ""
        ).strip():
            raise ValueError("API-key model authentication requires model_api_key_env.")
    if _config_bool(config.get("notifications_enabled"), False):
        recipients = _email_addresses(config.get("notification_email_recipients"))
        if not str(config.get("notification_webhook_env") or "").strip() and not recipients:
            raise ValueError("Notifications require at least one notification channel.")


def _reject_inline_secrets(value: Any, *, path: str) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            text_key = str(key)
            normalized = re.sub(r"(?<!^)(?=[A-Z])", "_", text_key).replace("-", "_").lower()
            child_path = f"{path}.{text_key}"
            if normalized in FORBIDDEN_INLINE_SECRET_KEYS:
                raise ValueError(f"Extension configuration cannot persist inline secret field: {child_path}")
            _reject_inline_secrets(child, path=child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_inline_secrets(child, path=f"{path}[{index}]")


def _validate_https_config_url(value: str, field: str) -> None:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError(f"Extension configuration field {field} must be an HTTPS URL without credentials.")
    for query_key, _query_value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        compact = re.sub(r"[^a-z0-9]", "", query_key.lower())
        if any(term in compact for term in ("key", "password", "secret", "token", "assertion", "credential")):
            raise ValueError(f"Extension configuration field {field} cannot contain credential query parameters.")


def _config_integer(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"Extension configuration field {field} must be an integer.")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Extension configuration field {field} must be an integer.") from exc
    if number < minimum or number > maximum:
        raise ValueError(f"Extension configuration field {field} must be between {minimum} and {maximum}.")
    return number


def _config_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    raise ValueError("Extension configuration boolean value is invalid.")


def _string_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, list):
        values = value
    else:
        raise ValueError("Extension configuration list value is invalid.")
    return [str(item).strip() for item in values if str(item).strip()]


def _email_addresses(value: Any) -> list[str]:
    raw_values = _string_values(value)
    addresses: list[str] = []
    for raw in raw_values:
        address = parseaddr(raw)[1]
        if not address or "@" not in address or len(address) > 320:
            raise ValueError("notification_email_recipients must contain valid email addresses.")
        addresses.append(address)
    return list(dict.fromkeys(addresses))


def get_extension_snapshot_scope(extension_id: str, config: dict[str, Any]) -> str | None:
    if extension_id != AZURE_POLICY_EXTENSION_ID or not bool(config.get("azure_monitor_enabled")):
        return None
    _ensure_extension_path(_azure_policy_src_path())
    from policy_monitor.azure_state import AzurePolicyStateConfig

    state_config = AzurePolicyStateConfig(
        subscription_id=str(config.get("subscription_id") or ""),
        scope=str(config.get("azure_scope") or config.get("scope") or ""),
        management_group_id=str(config.get("management_group_id") or ""),
    )
    return state_config.resolved_scope().lower()


def run_extension(
    extension_id: str,
    config: dict[str, Any],
    *,
    previous_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if extension_id != AZURE_POLICY_EXTENSION_ID:
        raise KeyError(f"Unknown extension: {extension_id}")
    _ensure_extension_path(_azure_policy_src_path())
    from policy_monitor import run_policy_monitor

    effective_config = dict(config)
    effective_config["repo_path"] = _normalize_repo_path(str(config.get("repo_path") or ""))
    return run_policy_monitor(effective_config, previous_snapshot=previous_snapshot)


def deliver_extension_notifications(
    extension_id: str,
    result: dict[str, Any],
    config: dict[str, Any],
    *,
    smtp_config: dict[str, Any],
) -> dict[str, Any]:
    if extension_id != AZURE_POLICY_EXTENSION_ID:
        raise KeyError(f"Unknown extension: {extension_id}")
    _ensure_extension_path(_azure_policy_src_path())
    from policy_monitor.notifications import deliver_notifications

    return deliver_notifications(result, config, smtp_config=smtp_config)
