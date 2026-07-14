# ruff: noqa: E402
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


EXTENSION_SRC = Path(__file__).resolve().parents[2] / "extensions" / "azure-policy-monitor" / "src"
if str(EXTENSION_SRC) not in sys.path:
    sys.path.insert(0, str(EXTENSION_SRC))

from policy_monitor.azure_auth import AzureTokenHttpResponse, DefaultAzureAccessTokenProvider


class RecordingTokenTransport:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.requests: list[dict[str, Any]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        form: dict[str, str] | None,
        timeout_seconds: float,
    ) -> AzureTokenHttpResponse:
        self.requests.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "form": dict(form or {}),
                "timeout_seconds": timeout_seconds,
            }
        )
        return AzureTokenHttpResponse(status_code=200, payload=self.payload)


def test_environment_client_secret_token_is_cached() -> None:
    transport = RecordingTokenTransport({"access_token": "arm-token", "expires_in": 3600})
    provider = DefaultAzureAccessTokenProvider(
        auth_mode="environment",
        environment={
            "AZURE_TENANT_ID": "tenant-id",
            "AZURE_CLIENT_ID": "client-id",
            "AZURE_CLIENT_SECRET": "must-not-be-logged",
        },
        transport=transport,
    )

    assert provider.get_token("https://management.azure.com/.default") == "arm-token"
    assert provider.get_token("https://management.azure.com/.default") == "arm-token"

    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request["method"] == "POST"
    assert request["url"] == "https://login.microsoftonline.com/tenant-id/oauth2/v2.0/token"
    assert request["form"] == {
        "client_id": "client-id",
        "client_secret": "must-not-be-logged",
        "grant_type": "client_credentials",
        "scope": "https://management.azure.com/.default",
    }


def test_environment_federated_identity_reads_assertion_file(tmp_path) -> None:
    assertion_file = tmp_path / "federated-token"
    assertion_file.write_text("signed-federated-assertion\n", encoding="utf-8")
    transport = RecordingTokenTransport({"access_token": "federated-arm-token", "expires_in": 900})
    provider = DefaultAzureAccessTokenProvider(
        auth_mode="environment",
        environment={
            "AZURE_TENANT_ID": "tenant-id",
            "AZURE_CLIENT_ID": "client-id",
            "AZURE_FEDERATED_TOKEN_FILE": str(assertion_file),
        },
        transport=transport,
    )

    assert provider.get_token("https://management.azure.com/.default") == "federated-arm-token"

    assert transport.requests[0]["form"] == {
        "client_assertion": "signed-federated-assertion",
        "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
        "client_id": "client-id",
        "grant_type": "client_credentials",
        "scope": "https://management.azure.com/.default",
    }