"""Compare Azure Policy snapshots and produce evidence-backed findings."""
from __future__ import annotations

from typing import Any


SEVERITY_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
RISKY_EFFECTS = {"deny", "deployifnotexists", "modify", "append"}


def diff_policy_snapshots(previous: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, Any]:
    if not previous:
        return {
            "severity": "info",
            "summary": {
                "baseline_created": True,
                "changed_definitions": 0,
                "changed_initiatives": 0,
                "changed_assignments": 0,
                "non_compliant_delta": 0,
            },
            "findings": [],
        }

    findings: list[dict[str, Any]] = []
    definition_changes = _diff_resources(
        "definitions", previous, current, "policy_definition_change", "Azure Policy definition changed.", findings
    )
    initiative_changes = _diff_resources(
        "initiatives", previous, current, "policy_initiative_change", "Azure Policy initiative changed.", findings
    )
    assignment_changes = _diff_assignments(previous, current, findings)
    _detect_risky_effect_changes(previous, current, findings)
    non_compliant_delta = _detect_compliance_drift(previous, current, findings)
    _detect_activity(previous, current, findings)
    findings.sort(key=lambda item: (-SEVERITY_ORDER.get(item["severity"], 0), item["check"], item.get("resource_id", "")))
    return {
        "severity": _worst_severity(findings),
        "summary": {
            "baseline_created": False,
            "changed_definitions": definition_changes,
            "changed_initiatives": initiative_changes,
            "changed_assignments": assignment_changes,
            "non_compliant_delta": non_compliant_delta,
        },
        "findings": findings,
    }


def _diff_resources(
    collection: str,
    previous: dict[str, Any],
    current: dict[str, Any],
    check: str,
    message: str,
    findings: list[dict[str, Any]],
) -> int:
    old_items = _mapping(previous.get(collection))
    new_items = _mapping(current.get(collection))
    changed = 0
    for resource_id in sorted(set(old_items) | set(new_items)):
        old = _mapping(old_items.get(resource_id))
        new = _mapping(new_items.get(resource_id))
        change_type = _change_type(old, new)
        if not change_type or (change_type == "modified" and old.get("content_hash") == new.get("content_hash")):
            continue
        changed += 1
        findings.append(
            _finding(
                check,
                "high" if change_type in {"modified", "deleted"} else "medium",
                message,
                resource_id,
                {
                    "change_type": change_type,
                    "previous_hash": old.get("content_hash"),
                    "current_hash": new.get("content_hash"),
                    "previous_version": old.get("version"),
                    "current_version": new.get("version"),
                },
            )
        )
    return changed


def _diff_assignments(
    previous: dict[str, Any],
    current: dict[str, Any],
    findings: list[dict[str, Any]],
) -> int:
    old_items = _mapping(previous.get("assignments"))
    new_items = _mapping(current.get("assignments"))
    changed = 0
    for resource_id in sorted(set(old_items) | set(new_items)):
        old = _mapping(old_items.get(resource_id))
        new = _mapping(new_items.get(resource_id))
        change_type = _change_type(old, new)
        if not change_type:
            continue
        if change_type != "modified":
            changed += 1
            findings.append(
                _finding(
                    "policy_assignment_change",
                    "critical" if change_type == "deleted" else "high",
                    "Azure Policy assignment changed.",
                    resource_id,
                    {"change_type": change_type},
                )
            )
            continue
        if old.get("content_hash") == new.get("content_hash"):
            continue
        changed += 1
        if _is_scope_expansion(str(old.get("scope") or ""), str(new.get("scope") or "")):
            findings.append(
                _finding(
                    "assignment_scope_expansion",
                    "critical",
                    "Azure Policy assignment scope expanded.",
                    resource_id,
                    {"previous_scope": old.get("scope"), "current_scope": new.get("scope")},
                )
            )
        if old.get("enforcement_mode") != new.get("enforcement_mode"):
            enabled = str(new.get("enforcement_mode") or "").lower() not in {"donotenforce", "disabled"}
            findings.append(
                _finding(
                    "enforcement_mode_change",
                    "critical" if enabled else "high",
                    "Azure Policy assignment enforcement mode changed.",
                    resource_id,
                    {
                        "previous_mode": old.get("enforcement_mode"),
                        "current_mode": new.get("enforcement_mode"),
                    },
                )
            )
        if old.get("parameters_hash") != new.get("parameters_hash"):
            findings.append(
                _finding(
                    "assignment_parameter_change",
                    "high",
                    "Azure Policy assignment parameters changed.",
                    resource_id,
                    {
                        "previous_hash": old.get("parameters_hash"),
                        "current_hash": new.get("parameters_hash"),
                    },
                )
            )
        if old.get("identity_hash") != new.get("identity_hash"):
            findings.append(
                _finding(
                    "assignment_identity_change",
                    "high",
                    "Azure Policy assignment managed identity changed.",
                    resource_id,
                    {
                        "previous_hash": old.get("identity_hash"),
                        "current_hash": new.get("identity_hash"),
                    },
                )
            )
        if not any(item.get("resource_id") == resource_id for item in findings):
            findings.append(
                _finding(
                    "policy_assignment_change",
                    "high",
                    "Azure Policy assignment changed.",
                    resource_id,
                    {"change_type": "modified"},
                )
            )
    return changed


def _detect_risky_effect_changes(
    previous: dict[str, Any],
    current: dict[str, Any],
    findings: list[dict[str, Any]],
) -> None:
    old_items = _mapping(previous.get("definitions"))
    new_items = _mapping(current.get("definitions"))
    for resource_id in sorted(set(old_items) & set(new_items)):
        old_effects = {str(value).lower() for value in _mapping(old_items.get(resource_id)).get("effects", [])}
        new_effects = {str(value).lower() for value in _mapping(new_items.get(resource_id)).get("effects", [])}
        introduced = sorted((new_effects - old_effects) & RISKY_EFFECTS)
        if introduced:
            findings.append(
                _finding(
                    "risky_effect_change",
                    "critical" if "deny" in introduced else "high",
                    "Azure Policy definition introduced a high-impact effect.",
                    resource_id,
                    {"previous_effects": sorted(old_effects), "current_effects": sorted(new_effects)},
                )
            )


def _detect_compliance_drift(
    previous: dict[str, Any],
    current: dict[str, Any],
    findings: list[dict[str, Any]],
) -> int:
    old_totals = _mapping(_mapping(previous.get("compliance")).get("totals"))
    new_totals = _mapping(_mapping(current.get("compliance")).get("totals"))
    delta = int(new_totals.get("non_compliant") or 0) - int(old_totals.get("non_compliant") or 0)
    if delta > 0:
        findings.append(
            _finding(
                "compliance_regression",
                "high",
                "Azure Policy non-compliant resource count increased.",
                str(current.get("scope") or ""),
                {
                    "previous_non_compliant": int(old_totals.get("non_compliant") or 0),
                    "current_non_compliant": int(new_totals.get("non_compliant") or 0),
                    "delta": delta,
                },
            )
        )
    return delta


def _detect_activity(
    previous: dict[str, Any],
    current: dict[str, Any],
    findings: list[dict[str, Any]],
) -> None:
    previous_keys = {
        _activity_key(event)
        for event in previous.get("activity", [])
        if isinstance(event, dict)
    }
    for event in current.get("activity", []):
        if not isinstance(event, dict):
            continue
        if _activity_key(event) in previous_keys:
            continue
        operation = str(event.get("operation") or "")
        if "microsoft.authorization/polic" not in operation.lower():
            continue
        findings.append(
            _finding(
                "azure_activity_change",
                "medium",
                "Azure Activity Log recorded a Policy control-plane operation.",
                str(event.get("resource_id") or ""),
                {
                    "event_id": event.get("id"),
                    "timestamp": event.get("timestamp"),
                    "operation": operation,
                    "status": event.get("status"),
                    "caller": event.get("caller"),
                },
            )
        )


def _activity_key(event: dict[str, Any]) -> tuple[str, ...]:
    event_id = str(event.get("id") or "").strip()
    if event_id:
        return ("id", event_id)
    return (
        "event",
        str(event.get("timestamp") or ""),
        str(event.get("operation") or ""),
        str(event.get("resource_id") or ""),
        str(event.get("correlation_id") or ""),
    )


def _change_type(old: dict[str, Any], new: dict[str, Any]) -> str:
    if not old and new:
        return "created"
    if old and not new:
        return "deleted"
    if old and new:
        return "modified"
    return ""


def _is_scope_expansion(previous_scope: str, current_scope: str) -> bool:
    previous = previous_scope.rstrip("/").lower()
    current = current_scope.rstrip("/").lower()
    return bool(previous and current and previous != current and previous.startswith(f"{current}/"))


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _finding(
    check: str,
    severity: str,
    message: str,
    resource_id: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    return {
        "check": check,
        "severity": severity,
        "message": message,
        "resource_id": resource_id,
        "evidence": evidence,
    }


def _worst_severity(findings: list[dict[str, Any]]) -> str:
    return max(
        (str(item.get("severity") or "info") for item in findings),
        key=lambda severity: SEVERITY_ORDER.get(severity, 0),
        default="info",
    )