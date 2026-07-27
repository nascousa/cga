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

from policy_monitor.azure_proxy import AzurePolicyProxyApi, AzureProxyError, AzureProxyHttpResponse


class RecordingProxyTransport:
    def __init__(self, responses: list[AzureProxyHttpResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def request(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json_body: dict[str, Any],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> AzureProxyHttpResponse:
        self.requests.append(
            {
                "url": url,
                "headers": dict(headers),
                "json_body": dict(json_body),
                "timeout_seconds": timeout_seconds,
                "max_response_bytes": max_response_bytes,
            }
        )
        return self.responses.pop(0)


def test_proxy_api_sends_key_in_header_and_fixed_operations() -> None:
    transport = RecordingProxyTransport(
        [
            AzureProxyHttpResponse(200, {}, {"items": [{"id": "/definitions/one"}]}),
            AzureProxyHttpResponse(200, {}, {"items": []}),
        ]
    )
    api = AzurePolicyProxyApi(
        endpoint="https://cga-policy-proxy.example",
        environment={"CGA_PROXY_KEY": "a" * 48},
        key_environment_name="CGA_PROXY_KEY",
        transport=transport,
    )
    scope = "/subscriptions/00000000-0000-0000-0000-000000000001"

    assert api.list_policy_definitions(scope) == [{"id": "/definitions/one"}]
    assert api.list_policy_activity(scope, datetime(2026, 7, 27, 8, 5, tzinfo=timezone.utc)) == []

    assert transport.requests[0]["url"] == "https://cga-policy-proxy.example/v1/azure-policy/query"
    assert transport.requests[0]["headers"]["X-CGA-Proxy-Key"] == "a" * 48
    assert transport.requests[0]["json_body"] == {
        "operation": "list_policy_definitions",
        "scope": scope,
    }
    assert transport.requests[1]["json_body"] == {
        "operation": "list_policy_activity",
        "scope": scope,
        "since": "2026-07-27T08:05:00Z",
    }


def test_proxy_api_disables_compliance_without_network_request() -> None:
    transport = RecordingProxyTransport([])
    api = AzurePolicyProxyApi(
        endpoint="https://cga-policy-proxy.example",
        environment={"CGA_PROXY_KEY": "a" * 48},
        key_environment_name="CGA_PROXY_KEY",
        transport=transport,
    )

    with pytest.raises(AzureProxyError, match="compliance queries are disabled"):
        api.query_policy_states("/subscriptions/00000000-0000-0000-0000-000000000001")

    assert transport.requests == []


def test_proxy_api_retries_transient_status_and_honors_limits() -> None:
    sleeps: list[float] = []
    transport = RecordingProxyTransport(
        [
            AzureProxyHttpResponse(429, {"retry-after": "0.25"}, {"error": {"code": "Busy"}}),
            AzureProxyHttpResponse(200, {}, {"items": [{"id": "one"}, {"id": "two"}]}),
        ]
    )
    api = AzurePolicyProxyApi(
        endpoint="https://cga-policy-proxy.example",
        environment={"CGA_PROXY_KEY": "a" * 48},
        key_environment_name="CGA_PROXY_KEY",
        transport=transport,
        max_collection_items=1,
        sleeper=sleeps.append,
    )

    with pytest.raises(AzureProxyError, match="collection limit"):
        api.list_policy_assignments("/subscriptions/00000000-0000-0000-0000-000000000001")

    assert sleeps == [0.25]
    assert len(transport.requests) == 2


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://cga-policy-proxy.example",
        "https://user:password@cga-policy-proxy.example",
        "https://cga-policy-proxy.example?key=secret",
    ],
)
def test_proxy_api_rejects_unsafe_endpoint(endpoint: str) -> None:
    with pytest.raises(ValueError, match="HTTPS URL"):
        AzurePolicyProxyApi(endpoint=endpoint)


def test_proxy_api_rejects_missing_key_before_network_request() -> None:
    transport = RecordingProxyTransport([])
    api = AzurePolicyProxyApi(
        endpoint="https://cga-policy-proxy.example",
        environment={},
        transport=transport,
    )

    with pytest.raises(AzureProxyError, match="missing or invalid"):
        api.list_policy_set_definitions("/subscriptions/00000000-0000-0000-0000-000000000001")

    assert transport.requests == []
