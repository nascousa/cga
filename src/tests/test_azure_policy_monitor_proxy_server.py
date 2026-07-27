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

from policy_monitor.azure_rest import AzureRestError
from policy_monitor.proxy_server import AzurePolicyProxyServerConfig, AzurePolicyProxyService


class RecordingAzureApi:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, str, datetime | None]] = []

    def _items(self, operation: str, scope: str, since: datetime | None = None) -> list[dict[str, Any]]:
        self.calls.append((operation, scope, since))
        if self.fail:
            raise AzureRestError("upstream included sensitive-access-token")
        return [{"id": f"{scope}/providers/Microsoft.Authorization/{operation}/one"}]

    def list_policy_definitions(self, scope: str) -> list[dict[str, Any]]:
        return self._items("policyDefinitions", scope)

    def list_policy_set_definitions(self, scope: str) -> list[dict[str, Any]]:
        return self._items("policySetDefinitions", scope)

    def list_policy_assignments(self, scope: str) -> list[dict[str, Any]]:
        return self._items("policyAssignments", scope)

    def query_policy_states(self, scope: str) -> list[dict[str, Any]]:
        raise AssertionError("Compliance must never be called by the proxy.")

    def list_policy_activity(self, scope: str, since: datetime) -> list[dict[str, Any]]:
        return self._items("activity", scope, since)


def _service(*, fail: bool = False, max_response_bytes: int = 1024 * 1024):
    config = AzurePolicyProxyServerConfig(
        subscription_id="00000000-0000-0000-0000-000000000001",
        shared_key="a" * 48,
        max_activity_lookback_minutes=180,
        max_response_bytes=max_response_bytes,
    )
    api = RecordingAzureApi(fail=fail)
    return AzurePolicyProxyService(config, api), api


def test_proxy_service_allows_only_fixed_read_operations_and_scope() -> None:
    service, api = _service()
    scope = "/subscriptions/00000000-0000-0000-0000-000000000001"

    status, response = service.execute(
        "a" * 48,
        {"operation": "list_policy_assignments", "scope": scope},
    )

    assert status == 200
    assert response["items"][0]["id"].endswith("/policyAssignments/one")
    assert api.calls == [("policyAssignments", scope, None)]

    denied_status, denied = service.execute(
        "a" * 48,
        {"operation": "query_policy_states", "scope": scope},
    )
    assert denied_status == 400
    assert denied["error"]["code"] == "OperationNotAllowed"
    assert len(api.calls) == 1


def test_proxy_service_rejects_bad_key_scope_and_extra_fields_without_azure_calls() -> None:
    service, api = _service()
    scope = "/subscriptions/00000000-0000-0000-0000-000000000001"

    assert service.execute("wrong", {"operation": "list_policy_definitions", "scope": scope})[0] == 401
    assert (
        service.execute(
            "a" * 48,
            {
                "operation": "list_policy_definitions",
                "scope": "/subscriptions/00000000-0000-0000-0000-000000000002",
            },
        )[0]
        == 403
    )
    assert service.execute(
        "a" * 48,
        {"operation": "list_policy_definitions", "scope": scope, "unexpected": True},
    )[0] == 400
    assert api.calls == []


def test_proxy_service_bounds_activity_window() -> None:
    service, api = _service()
    scope = "/subscriptions/00000000-0000-0000-0000-000000000001"
    now = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)

    rejected_status, rejected = service.execute(
        "a" * 48,
        {"operation": "list_policy_activity", "scope": scope, "since": "2026-07-27T08:00:00Z"},
        now=now,
    )
    accepted_status, _accepted = service.execute(
        "a" * 48,
        {"operation": "list_policy_activity", "scope": scope, "since": "2026-07-27T10:00:00Z"},
        now=now,
    )

    assert rejected_status == 400
    assert rejected["error"]["code"] == "InvalidRequest"
    assert accepted_status == 200
    assert api.calls == [("activity", scope, datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc))]


def test_proxy_service_redacts_upstream_error_details(caplog) -> None:
    service, _api = _service(fail=True)
    scope = "/subscriptions/00000000-0000-0000-0000-000000000001"

    status, response = service.execute(
        "a" * 48,
        {"operation": "list_policy_definitions", "scope": scope},
    )

    assert status == 502
    assert response == {"error": {"code": "AzureUpstreamError", "message": "Azure read operation failed."}}
    assert "sensitive-access-token" not in json.dumps(response)
    assert "sensitive-access-token" not in caplog.text
    assert "AzureRestError" in caplog.text


def test_proxy_server_config_requires_managed_scope_and_strong_key() -> None:
    config = AzurePolicyProxyServerConfig.from_environment(
        {
            "TARGET_SUBSCRIPTION_ID": "00000000-0000-0000-0000-000000000001",
            "PROXY_SHARED_KEY": "a" * 48,
            "AZURE_CLIENT_ID": "managed-identity-client-id",
            "MAX_ACTIVITY_LOOKBACK_MINUTES": "180",
        }
    )

    assert config.scope == "/subscriptions/00000000-0000-0000-0000-000000000001"
    assert config.managed_identity_client_id == "managed-identity-client-id"
    assert config.max_activity_lookback_minutes == 180
