"""Normalize deployed Azure Policy state into deterministic snapshots."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol


SENSITIVE_NAME = re.compile(r"(?:secret|token|password|credential|connectionstring|privatekey|api[_-]?key)", re.IGNORECASE)


class AzurePolicyApi(Protocol):
    def list_policy_definitions(self, scope: str) -> list[dict[str, Any]]: ...

    def list_policy_set_definitions(self, scope: str) -> list[dict[str, Any]]: ...

    def list_policy_assignments(self, scope: str) -> list[dict[str, Any]]: ...

    def query_policy_states(self, scope: str) -> list[dict[str, Any]]: ...

    def list_policy_activity(self, scope: str, since: datetime) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class AzurePolicyStateConfig:
    subscription_id: str = ""
    scope: str = ""
    management_group_id: str = ""
    activity_lookback_minutes: int = 1440
    include_compliance: bool = True
    include_activity: bool = True

    def resolved_scope(self) -> str:
        if self.scope.strip():
            scope = "/" + self.scope.strip().strip("/")
        elif self.management_group_id.strip():
            scope = f"/providers/Microsoft.Management/managementGroups/{self.management_group_id.strip()}"
        elif self.subscription_id.strip():
            scope = f"/subscriptions/{self.subscription_id.strip()}"
        else:
            raise ValueError("Azure Policy monitoring requires subscription_id, management_group_id, or scope.")
        lowered = scope.lower()
        if not (
            lowered.startswith("/subscriptions/")
            or lowered.startswith("/providers/microsoft.management/managementgroups/")
        ):
            raise ValueError("Azure Policy scope must be a subscription or management-group resource ID.")
        return scope


def collect_azure_policy_state(
    api: AzurePolicyApi,
    config: AzurePolicyStateConfig,
    *,
    captured_at: datetime | None = None,
) -> dict[str, Any]:
    timestamp = _as_utc(captured_at or datetime.now(timezone.utc))
    scope = config.resolved_scope()
    definitions = _normalize_resource_map(api.list_policy_definitions(scope), _normalize_definition)
    initiatives = _normalize_resource_map(api.list_policy_set_definitions(scope), _normalize_initiative)
    assignments = _normalize_resource_map(api.list_policy_assignments(scope), _normalize_assignment)
    policy_states = api.query_policy_states(scope) if config.include_compliance else []
    activity = (
        api.list_policy_activity(scope, timestamp - timedelta(minutes=max(1, config.activity_lookback_minutes)))
        if config.include_activity
        else []
    )
    return {
        "schema_version": 1,
        "captured_at": _iso_utc(timestamp),
        "scope": scope,
        "definitions": definitions,
        "initiatives": initiatives,
        "assignments": assignments,
        "compliance": _normalize_compliance(policy_states),
        "activity": _normalize_activity(activity),
    }


def _normalize_resource_map(
    resources: list[dict[str, Any]],
    normalizer: Any,
) -> dict[str, dict[str, Any]]:
    normalized: dict[str, dict[str, Any]] = {}
    for resource in resources:
        resource_id = _resource_id(resource)
        if resource_id:
            normalized[resource_id] = normalizer(resource, resource_id)
    return dict(sorted(normalized.items()))


def _normalize_definition(resource: dict[str, Any], resource_id: str) -> dict[str, Any]:
    properties = _mapping(resource.get("properties"))
    metadata = _mapping(properties.get("metadata"))
    policy_rule = properties.get("policyRule")
    content = {
        "display_name": _text(properties.get("displayName")),
        "description": _text(properties.get("description")),
        "mode": _text(properties.get("mode")),
        "version": _text(metadata.get("version")),
        "category": _text(metadata.get("category")),
        "parameters": _sanitize(properties.get("parameters", {})),
        "policy_rule": _sanitize(policy_rule),
    }
    return {
        "id": resource_id,
        "name": _text(resource.get("name")),
        "display_name": content["display_name"],
        "version": content["version"],
        "effects": sorted(_extract_effects(policy_rule)),
        "content_hash": _stable_hash(content),
    }


def _normalize_initiative(resource: dict[str, Any], resource_id: str) -> dict[str, Any]:
    properties = _mapping(resource.get("properties"))
    metadata = _mapping(properties.get("metadata"))
    references = properties.get("policyDefinitions", [])
    content = {
        "display_name": _text(properties.get("displayName")),
        "description": _text(properties.get("description")),
        "version": _text(metadata.get("version")),
        "parameters": _sanitize(properties.get("parameters", {})),
        "policy_definitions": _sanitize(references),
        "groups": _sanitize(properties.get("policyDefinitionGroups", [])),
    }
    return {
        "id": resource_id,
        "name": _text(resource.get("name")),
        "display_name": content["display_name"],
        "version": content["version"],
        "definition_count": len(references) if isinstance(references, list) else 0,
        "content_hash": _stable_hash(content),
    }


def _normalize_assignment(resource: dict[str, Any], resource_id: str) -> dict[str, Any]:
    properties = _mapping(resource.get("properties"))
    parameters = _sanitize(properties.get("parameters", {}))
    identity = _sanitize_identity(resource.get("identity"))
    assignment_scope = _text(properties.get("scope")) or _assignment_scope(resource_id)
    content = {
        "display_name": _text(properties.get("displayName")),
        "description": _text(properties.get("description")),
        "policy_definition_id": _text(properties.get("policyDefinitionId")).lower(),
        "scope": assignment_scope.lower(),
        "not_scopes": sorted(str(item).lower() for item in properties.get("notScopes", []) if item),
        "enforcement_mode": _text(properties.get("enforcementMode")) or "Default",
        "parameters": parameters,
        "identity": identity,
        "location": _text(resource.get("location")),
    }
    return {
        "id": resource_id,
        "name": _text(resource.get("name")),
        "display_name": content["display_name"],
        "policy_definition_id": content["policy_definition_id"],
        "scope": content["scope"],
        "not_scopes": content["not_scopes"],
        "enforcement_mode": content["enforcement_mode"],
        "parameters": parameters,
        "parameters_hash": _stable_hash(parameters),
        "identity": identity,
        "identity_hash": _stable_hash(identity),
        "content_hash": _stable_hash(content),
    }


def _normalize_compliance(states: list[dict[str, Any]]) -> dict[str, Any]:
    totals = {"compliant": 0, "non_compliant": 0, "unknown": 0}
    by_assignment: dict[str, dict[str, int]] = {}
    for state in states:
        compliance = _text(state.get("complianceState")).lower().replace("noncompliant", "non_compliant")
        key = compliance if compliance in totals else "unknown"
        totals[key] += 1
        assignment_id = _text(state.get("policyAssignmentId")).lower()
        if assignment_id:
            counts = by_assignment.setdefault(assignment_id, {"compliant": 0, "non_compliant": 0, "unknown": 0})
            counts[key] += 1
    return {
        "totals": totals,
        "by_assignment": dict(sorted(by_assignment.items())),
        "state_count": len(states),
    }


def _normalize_activity(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for event in events:
        normalized.append(
            {
                "id": _text(event.get("eventDataId") or event.get("id")),
                "timestamp": _text(event.get("eventTimestamp") or event.get("submissionTimestamp")),
                "operation": _nested_text(event.get("operationName")),
                "status": _nested_text(event.get("status")),
                "resource_id": _text(event.get("resourceId")).lower(),
                "caller": _text(event.get("caller")),
                "correlation_id": _text(event.get("correlationId")),
            }
        )
    return sorted(normalized, key=lambda item: (item["timestamp"], item["id"]))


def _sanitize(value: Any, *, key: str = "") -> Any:
    if key and SENSITIVE_NAME.search(key):
        return {"redacted": True, "value_hash": _stable_hash(value)}
    if isinstance(value, dict):
        return {str(item_key): _sanitize(item_value, key=str(item_key)) for item_key, item_value in sorted(value.items())}
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _sanitize_identity(value: Any) -> dict[str, Any]:
    identity = _mapping(value)
    return {
        "type": _text(identity.get("type")),
        "principal_id": _text(identity.get("principalId")),
        "tenant_id": _text(identity.get("tenantId")),
        "user_assigned_identities": sorted(str(key).lower() for key in _mapping(identity.get("userAssignedIdentities"))),
    }


def _extract_effects(value: Any) -> set[str]:
    effects: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() == "effect" and isinstance(item, str):
                effects.add(item)
            else:
                effects.update(_extract_effects(item))
    elif isinstance(value, list):
        for item in value:
            effects.update(_extract_effects(item))
    return effects


def _resource_id(resource: dict[str, Any]) -> str:
    return _text(resource.get("id")).rstrip("/").lower()


def _assignment_scope(resource_id: str) -> str:
    marker = "/providers/microsoft.authorization/policyassignments/"
    return resource_id.split(marker, 1)[0] if marker in resource_id else ""


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _nested_text(value: Any) -> str:
    if isinstance(value, dict):
        return _text(value.get("value") or value.get("localizedValue"))
    return _text(value)


def _stable_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")