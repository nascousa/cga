"""Orchestrate repository checks and deployed Azure Policy drift checks."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from .azure_auth import DefaultAzureAccessTokenProvider
from .azure_rest import AzureAccessTokenProvider, AzurePolicyRestApi
from .azure_state import AzurePolicyApi, AzurePolicyStateConfig, collect_azure_policy_state
from .diff import SEVERITY_ORDER, diff_policy_snapshots
from .summary import ModelTransport, generate_grounded_summary
from .scanner import PolicyMonitorConfig, scan_policy_repository


EXTENSION_ID = "azure_policy_change_monitor"


def run_policy_monitor(
    config: dict[str, Any],
    *,
    previous_snapshot: dict[str, Any] | None = None,
    azure_api: AzurePolicyApi | None = None,
    token_provider: AzureAccessTokenProvider | None = None,
    model_transport: ModelTransport | None = None,
    environment: Mapping[str, str] | None = None,
    captured_at: datetime | None = None,
) -> dict[str, Any]:
    read_only = _bool(config.get("read_only"), True)
    if not read_only:
        raise ValueError("Azure Policy Change Monitor is read-only and cannot run with read_only=false.")

    repository_enabled = _bool(config.get("repo_scan_enabled"), True)
    azure_enabled = _bool(config.get("azure_monitor_enabled"), False)
    findings: list[dict[str, Any]] = []
    repository_summary: dict[str, Any] = {"enabled": False}
    azure_summary: dict[str, Any] = {"enabled": False}
    snapshot: dict[str, Any] | None = None
    snapshot_scope = ""

    if repository_enabled:
        repository_result = scan_policy_repository(
            PolicyMonitorConfig(
                repo_path=str(config.get("repo_path") or ""),
                policy_root=str(config.get("policy_root") or "settings/BuiltInPoliciesV2"),
                cloud_folders=_string_list(
                    config.get("cloud_folders"),
                    ["AllEnvironments", "USNat", "USSec"],
                ),
                baseline_folder=str(config.get("baseline_folder") or "AllEnvironments"),
            )
        )
        repository_summary = {"enabled": True, **_mapping(repository_result.get("summary"))}
        findings.extend(_tag_findings(repository_result.get("findings"), "repository"))

    if azure_enabled:
        state_config = AzurePolicyStateConfig(
            subscription_id=str(config.get("subscription_id") or ""),
            scope=str(config.get("azure_scope") or config.get("scope") or ""),
            management_group_id=str(config.get("management_group_id") or ""),
            activity_lookback_minutes=_integer(config.get("activity_lookback_minutes"), 1440, 1, 43_200),
            include_compliance=_bool(config.get("include_compliance"), True),
            include_activity=_bool(config.get("include_activity"), True),
        )
        resolved_scope = state_config.resolved_scope()
        api = azure_api or _create_azure_api(config, token_provider=token_provider)
        snapshot = collect_azure_policy_state(api, state_config, captured_at=captured_at)
        drift = diff_policy_snapshots(previous_snapshot, snapshot)
        findings.extend(_tag_findings(drift.get("findings"), "azure"))
        snapshot_scope = resolved_scope.lower()
        azure_summary = {
            "enabled": True,
            "scope": resolved_scope,
            **_mapping(drift.get("summary")),
            "definition_count": len(_mapping(snapshot.get("definitions"))),
            "initiative_count": len(_mapping(snapshot.get("initiatives"))),
            "assignment_count": len(_mapping(snapshot.get("assignments"))),
            "policy_state_count": int(_mapping(snapshot.get("compliance")).get("state_count") or 0),
            "activity_count": len(snapshot.get("activity") or []),
        }

    findings.sort(
        key=lambda item: (
            -SEVERITY_ORDER.get(str(item.get("severity") or "info"), 0),
            str(item.get("source") or ""),
            str(item.get("check") or ""),
            str(item.get("resource_id") or item.get("file") or ""),
        )
    )
    result = {
        "extension_id": EXTENSION_ID,
        "status": "completed",
        "severity": _worst_severity(findings),
        "summary": {
            "finding_count": len(findings),
            "severity_counts": _severity_counts(findings),
            "repository": repository_summary,
            "azure": azure_summary,
        },
        "monitoring": {
            "read_only": True,
            "repository_enabled": repository_enabled,
            "azure_enabled": azure_enabled,
        },
        "findings": findings,
    }
    if snapshot is not None:
        result["_snapshot"] = snapshot
        result["_snapshot_scope"] = snapshot_scope
    if _bool(config.get("model_summary_enabled"), False):
        try:
            summary_output = generate_grounded_summary(
                result,
                config,
                environment=environment,
                transport=model_transport,
                token_provider=token_provider,
            )
        except Exception as exc:
            summary_output = {
                "status": "failed",
                "grounded": False,
                "error_type": type(exc).__name__,
            }
    else:
        summary_output = {"status": "disabled", "grounded": False}
    result["outputs"] = {"summary": summary_output}
    return result


def _create_azure_api(
    config: dict[str, Any],
    *,
    token_provider: AzureAccessTokenProvider | None,
) -> AzurePolicyRestApi:
    management_endpoint = str(config.get("management_endpoint") or "https://management.azure.com").rstrip("/")
    authority_host = str(config.get("authority_host") or "https://login.microsoftonline.com")
    provider = token_provider or DefaultAzureAccessTokenProvider(
        auth_mode=str(config.get("auth_mode") or "auto"),
        authority_host=authority_host,
        managed_identity_client_id=str(config.get("managed_identity_client_id") or ""),
        timeout_seconds=float(_integer(config.get("azure_timeout_seconds"), 30, 1, 120)),
    )
    token_scope = str(config.get("arm_token_scope") or f"{management_endpoint}/.default")
    return AzurePolicyRestApi(
        subscription_id=str(config.get("subscription_id") or ""),
        token_provider=provider,
        management_endpoint=management_endpoint,
        token_scope=token_scope,
        activity_subscription_ids=_string_list(config.get("activity_subscription_ids"), []),
        timeout_seconds=float(_integer(config.get("azure_timeout_seconds"), 30, 1, 120)),
        max_attempts=_integer(config.get("azure_max_attempts"), 4, 1, 8),
        max_collection_items=_integer(config.get("max_collection_items"), 50_000, 1, 250_000),
    )


def _tag_findings(value: Any, source: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [{"source": source, **item} for item in value if isinstance(item, dict)]


def _severity_counts(findings: list[dict[str, Any]]) -> dict[str, int]:
    counts = {severity: 0 for severity in SEVERITY_ORDER}
    for finding in findings:
        severity = str(finding.get("severity") or "info")
        counts[severity] = counts.get(severity, 0) + 1
    return counts


def _worst_severity(findings: list[dict[str, Any]]) -> str:
    return max(
        (str(item.get("severity") or "info") for item in findings),
        key=lambda severity: SEVERITY_ORDER.get(severity, 0),
        default="info",
    )


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    raise ValueError("Boolean extension configuration value is invalid.")


def _integer(value: Any, default: int, minimum: int, maximum: int) -> int:
    if value is None or value == "":
        number = default
    elif isinstance(value, bool):
        raise ValueError("Integer extension configuration value is invalid.")
    else:
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("Integer extension configuration value is invalid.") from exc
    if number < minimum or number > maximum:
        raise ValueError(f"Integer extension configuration value must be between {minimum} and {maximum}.")
    return number


def _string_list(value: Any, default: list[str]) -> list[str]:
    if value is None:
        return list(default)
    if isinstance(value, str):
        values = [item.strip() for item in value.split(",")]
    elif isinstance(value, list):
        values = [str(item).strip() for item in value]
    else:
        raise ValueError("List extension configuration value is invalid.")
    return [item for item in values if item]