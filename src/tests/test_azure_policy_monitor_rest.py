# ruff: noqa: E402
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest


EXTENSION_SRC = Path(__file__).resolve().parents[2] / "extensions" / "azure-policy-monitor" / "src"
if str(EXTENSION_SRC) not in sys.path:
    sys.path.insert(0, str(EXTENSION_SRC))

from policy_monitor.azure_rest import AzureHttpResponse, AzurePolicyRestApi, AzureRestError


class FakeTokenProvider:
    def __init__(self) -> None:
        self.resources: list[str] = []

    def get_token(self, resource: str) -> str:
        self.resources.append(resource)
        return "sensitive-access-token"


class RecordingTransport:
    def __init__(self, responses: list[AzureHttpResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json_body: dict[str, Any] | None,
        timeout_seconds: float,
    ) -> AzureHttpResponse:
        self.requests.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "json_body": json_body,
                "timeout_seconds": timeout_seconds,
            }
        )
        return self.responses.pop(0)


def test_rest_api_follows_same_origin_pagination_with_bearer_auth() -> None:
    token_provider = FakeTokenProvider()
    transport = RecordingTransport(
        [
            AzureHttpResponse(
                status_code=200,
                headers={},
                payload={
                    "value": [{"id": "/definitions/one"}],
                    "nextLink": "https://management.azure.com/next/definitions?page=2",
                },
            ),
            AzureHttpResponse(status_code=200, headers={}, payload={"value": [{"id": "/definitions/two"}]}),
        ]
    )
    api = AzurePolicyRestApi(
        subscription_id="00000000-0000-0000-0000-000000000001",
        token_provider=token_provider,
        transport=transport,
    )

    definitions = api.list_policy_definitions("/subscriptions/00000000-0000-0000-0000-000000000001")

    assert definitions == [{"id": "/definitions/one"}, {"id": "/definitions/two"}]
    assert token_provider.resources == ["https://management.azure.com/.default"]
    assert [request["method"] for request in transport.requests] == ["GET", "GET"]
    assert all(request["headers"]["Authorization"] == "Bearer sensitive-access-token" for request in transport.requests)
    assert "api-version=2023-04-01" in transport.requests[0]["url"]


def test_rest_api_calls_policy_insights_and_activity_log_contracts() -> None:
    token_provider = FakeTokenProvider()
    transport = RecordingTransport(
        [
            AzureHttpResponse(status_code=200, headers={}, payload={"value": []}),
            AzureHttpResponse(status_code=200, headers={}, payload={"value": []}),
            AzureHttpResponse(status_code=200, headers={}, payload={"value": []}),
            AzureHttpResponse(status_code=200, headers={}, payload={"value": []}),
            AzureHttpResponse(status_code=200, headers={}, payload={"value": []}),
        ]
    )
    subscription_id = "00000000-0000-0000-0000-000000000001"
    scope = f"/subscriptions/{subscription_id}"
    api = AzurePolicyRestApi(
        subscription_id=subscription_id,
        token_provider=token_provider,
        transport=transport,
    )

    api.list_policy_definitions(scope)
    api.list_policy_set_definitions(scope)
    api.list_policy_assignments(scope)
    api.query_policy_states(scope)
    api.list_policy_activity(scope, datetime(2026, 7, 14, 7, 0, tzinfo=timezone.utc))

    urls = [request["url"] for request in transport.requests]
    assert "/providers/Microsoft.Authorization/policyDefinitions?" in urls[0]
    assert "/providers/Microsoft.Authorization/policySetDefinitions?" in urls[1]
    assert "/providers/Microsoft.Authorization/policyAssignments?" in urls[2]
    assert "/providers/Microsoft.PolicyInsights/policyStates/latest/queryResults?" in urls[3]
    assert transport.requests[3]["method"] == "POST"
    assert transport.requests[3]["json_body"] == {}
    assert "api-version=2019-10-01" in urls[3]
    assert "/providers/Microsoft.Insights/eventtypes/management/values?" in urls[4]
    assert "api-version=2015-04-01" in urls[4]
    assert "Microsoft.Authorization" in urls[4]
    assert transport.requests[4]["method"] == "GET"


def test_rest_api_rejects_cross_origin_next_link_without_leaking_token() -> None:
    token_provider = FakeTokenProvider()
    transport = RecordingTransport(
        [
            AzureHttpResponse(
                status_code=200,
                headers={},
                payload={"value": [], "nextLink": "https://attacker.example/steal"},
            )
        ]
    )
    api = AzurePolicyRestApi(
        subscription_id="00000000-0000-0000-0000-000000000001",
        token_provider=token_provider,
        transport=transport,
    )

    with pytest.raises(AzureRestError, match="untrusted pagination URL") as exc_info:
        api.list_policy_assignments("/subscriptions/00000000-0000-0000-0000-000000000001")

    assert "sensitive-access-token" not in str(exc_info.value)
    assert len(transport.requests) == 1
