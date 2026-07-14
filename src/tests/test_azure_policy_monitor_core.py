# ruff: noqa: E402
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


EXTENSION_SRC = Path(__file__).resolve().parents[2] / "extensions" / "azure-policy-monitor" / "src"
if str(EXTENSION_SRC) not in sys.path:
    sys.path.insert(0, str(EXTENSION_SRC))

from policy_monitor.azure_state import AzurePolicyStateConfig, collect_azure_policy_state
from policy_monitor.diff import diff_policy_snapshots


class FakeAzurePolicyApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def list_policy_definitions(self, scope: str) -> list[dict[str, Any]]:
        self.calls.append(("definitions", scope))
        return [
            {
                "id": f"{scope}/providers/Microsoft.Authorization/policyDefinitions/deny-public-ip",
                "name": "deny-public-ip",
                "properties": {
                    "displayName": "Deny public IP addresses",
                    "metadata": {"version": "1.2.0", "category": "Network"},
                    "policyRule": {"then": {"effect": "Deny"}},
                },
            }
        ]

    def list_policy_set_definitions(self, scope: str) -> list[dict[str, Any]]:
        self.calls.append(("initiatives", scope))
        return [
            {
                "id": f"{scope}/providers/Microsoft.Authorization/policySetDefinitions/security-baseline",
                "name": "security-baseline",
                "properties": {
                    "displayName": "Security baseline",
                    "metadata": {"version": "2.0.0"},
                    "policyDefinitions": [
                        {
                            "policyDefinitionId": f"{scope}/providers/Microsoft.Authorization/policyDefinitions/deny-public-ip"
                        }
                    ],
                },
            }
        ]

    def list_policy_assignments(self, scope: str) -> list[dict[str, Any]]:
        self.calls.append(("assignments", scope))
        return [
            {
                "id": f"{scope}/providers/Microsoft.Authorization/policyAssignments/security-baseline-prod",
                "name": "security-baseline-prod",
                "identity": {"type": "SystemAssigned", "principalId": "principal-1", "tenantId": "tenant-1"},
                "properties": {
                    "displayName": "Security baseline production",
                    "policyDefinitionId": f"{scope}/providers/Microsoft.Authorization/policySetDefinitions/security-baseline",
                    "enforcementMode": "Default",
                    "parameters": {
                        "allowedLocations": {"value": ["westus2"]},
                        "serviceToken": {"value": "must-not-be-persisted"},
                    },
                },
            }
        ]

    def query_policy_states(self, scope: str) -> list[dict[str, Any]]:
        self.calls.append(("compliance", scope))
        assignment_id = f"{scope}/providers/Microsoft.Authorization/policyAssignments/security-baseline-prod"
        return [
            {
                "policyAssignmentId": assignment_id,
                "policyDefinitionId": f"{scope}/providers/Microsoft.Authorization/policyDefinitions/deny-public-ip",
                "resourceId": f"{scope}/resourceGroups/app/providers/Microsoft.Network/publicIPAddresses/ip-1",
                "complianceState": "NonCompliant",
                "timestamp": "2026-07-14T08:00:00Z",
            },
            {
                "policyAssignmentId": assignment_id,
                "policyDefinitionId": f"{scope}/providers/Microsoft.Authorization/policyDefinitions/deny-public-ip",
                "resourceId": f"{scope}/resourceGroups/app/providers/Microsoft.Network/virtualNetworks/vnet-1",
                "complianceState": "Compliant",
                "timestamp": "2026-07-14T08:01:00Z",
            },
        ]

    def list_policy_activity(self, scope: str, since: datetime) -> list[dict[str, Any]]:
        self.calls.append(("activity", scope))
        return [
            {
                "eventDataId": "activity-1",
                "eventTimestamp": "2026-07-14T08:02:00Z",
                "operationName": {"value": "Microsoft.Authorization/policyAssignments/write"},
                "status": {"value": "Succeeded"},
                "resourceId": f"{scope}/providers/Microsoft.Authorization/policyAssignments/security-baseline-prod",
                "caller": "operator@example.com",
                "claims": {"access_token": "must-not-be-persisted"},
            }
        ]


def test_collect_azure_policy_state_normalizes_and_redacts_sensitive_values() -> None:
    api = FakeAzurePolicyApi()
    config = AzurePolicyStateConfig(
        subscription_id="00000000-0000-0000-0000-000000000001",
        activity_lookback_minutes=120,
    )

    snapshot = collect_azure_policy_state(
        api,
        config,
        captured_at=datetime(2026, 7, 14, 8, 5, tzinfo=timezone.utc),
    )

    scope = "/subscriptions/00000000-0000-0000-0000-000000000001"
    assignment_id = f"{scope}/providers/microsoft.authorization/policyassignments/security-baseline-prod"
    serialized = json.dumps(snapshot, sort_keys=True)
    assert "must-not-be-persisted" not in serialized
    assert snapshot["scope"] == scope
    assert set(snapshot["definitions"]) == {
        f"{scope}/providers/microsoft.authorization/policydefinitions/deny-public-ip"
    }
    assert set(snapshot["initiatives"]) == {
        f"{scope}/providers/microsoft.authorization/policysetdefinitions/security-baseline"
    }
    assert snapshot["assignments"][assignment_id]["scope"] == scope
    assert snapshot["assignments"][assignment_id]["parameters"]["serviceToken"] == {
        "redacted": True,
        "value_hash": "42ef222afeb030f6502a8f4b309cb6d3cbe4b4d1a754e9b67b27c7bb2dfd6111",
    }
    assert snapshot["compliance"]["totals"] == {"compliant": 1, "non_compliant": 1, "unknown": 0}
    assert snapshot["activity"][0]["operation"] == "Microsoft.Authorization/policyAssignments/write"
    assert api.calls == [
        ("definitions", scope),
        ("initiatives", scope),
        ("assignments", scope),
        ("compliance", scope),
        ("activity", scope),
    ]


def test_diff_policy_snapshots_reports_policy_assignment_and_compliance_drift() -> None:
    scope = "/subscriptions/00000000-0000-0000-0000-000000000001"
    definition_id = f"{scope}/providers/microsoft.authorization/policydefinitions/deny-public-ip"
    assignment_id = f"{scope}/providers/microsoft.authorization/policyassignments/security-baseline-prod"
    previous = {
        "captured_at": "2026-07-13T08:00:00Z",
        "scope": scope,
        "definitions": {definition_id: {"version": "1.1.0", "effects": ["Audit"], "content_hash": "old"}},
        "initiatives": {},
        "assignments": {
            assignment_id: {
                "scope": f"{scope}/resourceGroups/app",
                "enforcement_mode": "DoNotEnforce",
                "parameters_hash": "parameters-old",
                "identity_hash": "identity-old",
                "content_hash": "assignment-old",
            }
        },
        "compliance": {"totals": {"compliant": 10, "non_compliant": 1, "unknown": 0}, "by_assignment": {}},
        "activity": [],
    }
    current = {
        "captured_at": "2026-07-14T08:00:00Z",
        "scope": scope,
        "definitions": {definition_id: {"version": "1.2.0", "effects": ["Deny"], "content_hash": "new"}},
        "initiatives": {},
        "assignments": {
            assignment_id: {
                "scope": scope,
                "enforcement_mode": "Default",
                "parameters_hash": "parameters-new",
                "identity_hash": "identity-new",
                "content_hash": "assignment-new",
            }
        },
        "compliance": {"totals": {"compliant": 8, "non_compliant": 3, "unknown": 0}, "by_assignment": {}},
        "activity": [
            {
                "id": "activity-1",
                "timestamp": "2026-07-14T07:58:00Z",
                "operation": "Microsoft.Authorization/policyAssignments/write",
                "status": "Succeeded",
                "resource_id": assignment_id,
                "caller": "operator@example.com",
            }
        ],
    }

    drift = diff_policy_snapshots(previous, current)
    checks = {finding["check"] for finding in drift["findings"]}

    assert {
        "policy_definition_change",
        "risky_effect_change",
        "assignment_scope_expansion",
        "enforcement_mode_change",
        "assignment_parameter_change",
        "assignment_identity_change",
        "compliance_regression",
        "azure_activity_change",
    } <= checks
    assert drift["severity"] == "critical"
    assert drift["summary"]["changed_definitions"] == 1
    assert drift["summary"]["changed_assignments"] == 1
    assert drift["summary"]["non_compliant_delta"] == 2


def test_diff_policy_snapshots_does_not_repeat_activity_from_previous_snapshot() -> None:
    event = {
        "id": "activity-1",
        "timestamp": "2026-07-14T07:58:00Z",
        "operation": "Microsoft.Authorization/policyAssignments/write",
        "status": "Succeeded",
        "resource_id": "/subscriptions/sub/providers/microsoft.authorization/policyassignments/baseline",
        "caller": "operator@example.com",
    }
    previous = {
        "scope": "/subscriptions/sub",
        "definitions": {},
        "initiatives": {},
        "assignments": {},
        "compliance": {"totals": {}},
        "activity": [event],
    }
    current = {**previous, "captured_at": "2026-07-14T08:05:00Z", "activity": [event]}

    drift = diff_policy_snapshots(previous, current)

    assert not any(finding["check"] == "azure_activity_change" for finding in drift["findings"])