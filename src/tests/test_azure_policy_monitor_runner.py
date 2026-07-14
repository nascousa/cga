# ruff: noqa: E402
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest


EXTENSION_SRC = Path(__file__).resolve().parents[2] / "extensions" / "azure-policy-monitor" / "src"
if str(EXTENSION_SRC) not in sys.path:
    sys.path.insert(0, str(EXTENSION_SRC))

import policy_monitor.runner as runner_module
from policy_monitor.runner import run_policy_monitor


class MutableAzurePolicyApi:
    def __init__(self, *, enforcement_mode: str = "DoNotEnforce", non_compliant: int = 1) -> None:
        self.enforcement_mode = enforcement_mode
        self.non_compliant = non_compliant

    def list_policy_definitions(self, scope: str) -> list[dict[str, Any]]:
        return []

    def list_policy_set_definitions(self, scope: str) -> list[dict[str, Any]]:
        return []

    def list_policy_assignments(self, scope: str) -> list[dict[str, Any]]:
        return [
            {
                "id": f"{scope}/providers/Microsoft.Authorization/policyAssignments/baseline",
                "name": "baseline",
                "properties": {
                    "policyDefinitionId": "/providers/Microsoft.Authorization/policyDefinitions/baseline",
                    "enforcementMode": self.enforcement_mode,
                    "parameters": {"serviceToken": {"value": "never-persist-this"}},
                },
            }
        ]

    def query_policy_states(self, scope: str) -> list[dict[str, Any]]:
        assignment_id = f"{scope}/providers/Microsoft.Authorization/policyAssignments/baseline"
        return [
            {"policyAssignmentId": assignment_id, "complianceState": "NonCompliant"}
            for _ in range(self.non_compliant)
        ]

    def list_policy_activity(self, scope: str, since: datetime) -> list[dict[str, Any]]:
        return []


def test_live_runner_creates_baseline_then_reports_drift() -> None:
    config = {
        "repo_scan_enabled": False,
        "azure_monitor_enabled": True,
        "subscription_id": "00000000-0000-0000-0000-000000000001",
        "include_activity": False,
        "read_only": True,
    }
    baseline = run_policy_monitor(config, azure_api=MutableAzurePolicyApi())

    assert baseline["summary"]["azure"]["baseline_created"] is True
    assert baseline["summary"]["finding_count"] == 0
    assert baseline["_snapshot_scope"] == "/subscriptions/00000000-0000-0000-0000-000000000001"
    assert "never-persist-this" not in json.dumps(baseline, sort_keys=True)

    drift = run_policy_monitor(
        config,
        previous_snapshot=baseline["_snapshot"],
        azure_api=MutableAzurePolicyApi(enforcement_mode="Default", non_compliant=3),
    )
    checks = {finding["check"] for finding in drift["findings"]}

    assert {"enforcement_mode_change", "compliance_regression"} <= checks
    assert drift["severity"] == "critical"
    assert drift["summary"]["azure"]["non_compliant_delta"] == 2


def test_runner_rejects_non_read_only_configuration() -> None:
    with pytest.raises(ValueError, match="read-only"):
        run_policy_monitor(
            {"repo_scan_enabled": False, "azure_monitor_enabled": True, "read_only": False},
            azure_api=MutableAzurePolicyApi(),
        )


def test_model_summary_failure_is_reported_without_failing_monitor(monkeypatch) -> None:
    def fail_summary(*args, **kwargs):
        raise RuntimeError("provider response contained a secret that must not be persisted")

    monkeypatch.setattr(runner_module, "generate_grounded_summary", fail_summary)

    result = run_policy_monitor(
        {
            "repo_scan_enabled": False,
            "azure_monitor_enabled": True,
            "subscription_id": "00000000-0000-0000-0000-000000000001",
            "include_activity": False,
            "model_summary_enabled": True,
            "read_only": True,
        },
        azure_api=MutableAzurePolicyApi(),
    )

    assert result["status"] == "completed"
    assert result["outputs"]["summary"] == {
        "status": "failed",
        "grounded": False,
        "error_type": "RuntimeError",
    }
    assert "secret" not in json.dumps(result, sort_keys=True)
    assert result["_snapshot"]