"""Bounded HTTPS client for the managed Azure Policy read proxy."""
from __future__ import annotations

import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

PROXY_PATH = "/v1/azure-policy/query"
PROXY_KEY_HEADER = "X-CGA-Proxy-Key"
ENVIRONMENT_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")


class AzureProxyError(RuntimeError):
    pass


class AzureProxyTransport(Protocol):
    def request(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json_body: dict[str, Any],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> AzureProxyHttpResponse: ...


@dataclass(frozen=True)
class AzureProxyHttpResponse:
    status_code: int
    headers: dict[str, str]
    payload: dict[str, Any]


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UrllibAzureProxyTransport:
    def __init__(self, *, ssl_context: ssl.SSLContext | None = None) -> None:
        https_handler = urllib.request.HTTPSHandler(context=ssl_context or ssl.create_default_context())
        self._opener = urllib.request.build_opener(https_handler, _NoRedirectHandler())

    def request(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json_body: dict[str, Any],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> AzureProxyHttpResponse:
        body = json.dumps(json_body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with self._opener.open(request, timeout=timeout_seconds) as response:
                return AzureProxyHttpResponse(
                    status_code=int(response.status),
                    headers={key.lower(): value for key, value in response.headers.items()},
                    payload=_decode_bounded_payload(response, max_response_bytes),
                )
        except urllib.error.HTTPError as exc:
            return AzureProxyHttpResponse(
                status_code=int(exc.code),
                headers={key.lower(): value for key, value in exc.headers.items()} if exc.headers else {},
                payload=_decode_bounded_payload(exc, max_response_bytes),
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AzureProxyError(f"Azure Policy proxy request failed: {type(exc).__name__}") from exc


class AzurePolicyProxyApi:
    def __init__(
        self,
        *,
        endpoint: str,
        key_environment_name: str = "AZURE_POLICY_MONITOR_PROXY_KEY",
        environment: Mapping[str, str] | None = None,
        transport: AzureProxyTransport | None = None,
        timeout_seconds: float = 30.0,
        max_attempts: int = 4,
        max_collection_items: int = 50_000,
        max_response_bytes: int = 25 * 1024 * 1024,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        base_endpoint = str(endpoint or "").strip().rstrip("/")
        parsed = urllib.parse.urlparse(base_endpoint)
        if (
            parsed.scheme.lower() != "https"
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Azure Policy proxy endpoint must be an HTTPS URL without credentials or query data.")
        environment_name = str(key_environment_name or "").strip()
        if not ENVIRONMENT_NAME.fullmatch(environment_name):
            raise ValueError("Azure Policy proxy key environment name is invalid.")
        self.endpoint = f"{base_endpoint}{PROXY_PATH}"
        self.key_environment_name = environment_name
        self.environment = environment if environment is not None else os.environ
        self.transport = transport or UrllibAzureProxyTransport()
        self.timeout_seconds = max(1.0, min(float(timeout_seconds), 120.0))
        self.max_attempts = max(1, min(int(max_attempts), 8))
        self.max_collection_items = max(1, int(max_collection_items))
        self.max_response_bytes = max(1024, min(int(max_response_bytes), 50 * 1024 * 1024))
        self.sleeper = sleeper

    def list_policy_definitions(self, scope: str) -> list[dict[str, Any]]:
        return self._list("list_policy_definitions", scope)

    def list_policy_set_definitions(self, scope: str) -> list[dict[str, Any]]:
        return self._list("list_policy_set_definitions", scope)

    def list_policy_assignments(self, scope: str) -> list[dict[str, Any]]:
        return self._list("list_policy_assignments", scope)

    def query_policy_states(self, scope: str) -> list[dict[str, Any]]:
        raise AzureProxyError("Azure Policy compliance queries are disabled for proxy authentication.")

    def list_policy_activity(self, scope: str, since: datetime) -> list[dict[str, Any]]:
        timestamp = since if since.tzinfo is not None else since.replace(tzinfo=timezone.utc)
        return self._list(
            "list_policy_activity",
            scope,
            since=timestamp.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        )

    def _list(self, operation: str, scope: str, *, since: str = "") -> list[dict[str, Any]]:
        body = {"operation": operation, "scope": str(scope or "").strip()}
        if since:
            body["since"] = since
        payload = self._request(body)
        items = payload.get("items")
        if not isinstance(items, list):
            raise AzureProxyError("Azure Policy proxy response did not contain a list of items.")
        if len(items) > self.max_collection_items:
            raise AzureProxyError("Azure Policy proxy result exceeded the configured collection limit.")
        return [item for item in items if isinstance(item, dict)]

    def _request(self, body: dict[str, Any]) -> dict[str, Any]:
        key = str(self.environment.get(self.key_environment_name, "") or "").strip()
        if len(key) < 32 or len(key) > 512 or "\r" in key or "\n" in key:
            raise AzureProxyError("Azure Policy proxy key is missing or invalid.")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            PROXY_KEY_HEADER: key,
            "User-Agent": "CGA-AzurePolicyMonitor/1.0",
        }
        response: AzureProxyHttpResponse | None = None
        transport_error: AzureProxyError | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.transport.request(
                    self.endpoint,
                    headers=headers,
                    json_body=body,
                    timeout_seconds=self.timeout_seconds,
                    max_response_bytes=self.max_response_bytes,
                )
                transport_error = None
            except AzureProxyError as exc:
                transport_error = exc
                if attempt == self.max_attempts:
                    raise
            else:
                if response.status_code < 400:
                    return response.payload
                if response.status_code not in {408, 429, 500, 502, 503, 504} or attempt == self.max_attempts:
                    break
            if attempt < self.max_attempts:
                retry_after = _retry_after(response.headers) if response is not None and transport_error is None else None
                self.sleeper(retry_after if retry_after is not None else min(2 ** (attempt - 1), 8))
        if response is None:
            raise transport_error or AzureProxyError("Azure Policy proxy request failed without a response.")
        error = response.payload.get("error") if isinstance(response.payload, dict) else None
        error_data = error if isinstance(error, dict) else {}
        code = _safe_text(error_data.get("code") or "ProxyRequestFailed")
        message = _safe_text(error_data.get("message") or "Azure Policy proxy request failed.")
        raise AzureProxyError(f"Azure Policy proxy request failed ({response.status_code}, {code}): {message}")


def _decode_bounded_payload(response: Any, max_response_bytes: int) -> dict[str, Any]:
    raw = response.read(max_response_bytes + 1)
    if len(raw) > max_response_bytes:
        raise AzureProxyError("Azure Policy proxy response exceeded the configured byte limit.")
    if not raw:
        return {}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"error": {"code": "InvalidResponse", "message": "Proxy returned a non-JSON response."}}
    return payload if isinstance(payload, dict) else {}


def _safe_text(value: Any) -> str:
    return str(value or "").replace("\r", " ").replace("\n", " ").strip()[:500]


def _retry_after(headers: dict[str, str]) -> float | None:
    value = headers.get("retry-after") or headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, min(float(value), 60.0))
    except (TypeError, ValueError):
        return None
