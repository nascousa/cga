"""Dependency-free Azure Management REST adapter for Policy monitoring."""
from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Protocol


ARM_RESOURCE_SCOPE = "https://management.azure.com/.default"
POLICY_API_VERSION = "2023-04-01"
POLICY_INSIGHTS_API_VERSION = "2019-10-01"
ACTIVITY_LOG_API_VERSION = "2015-04-01"


class AzureRestError(RuntimeError):
    pass


class AzureAccessTokenProvider(Protocol):
    def get_token(self, resource: str) -> str: ...


class AzureHttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json_body: dict[str, Any] | None,
        timeout_seconds: float,
    ) -> "AzureHttpResponse": ...


@dataclass(frozen=True)
class AzureHttpResponse:
    status_code: int
    headers: dict[str, str]
    payload: dict[str, Any]


class UrllibAzureTransport:
    def __init__(self, *, ssl_context: ssl.SSLContext | None = None) -> None:
        self._ssl_context = ssl_context or ssl.create_default_context()

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json_body: dict[str, Any] | None,
        timeout_seconds: float,
    ) -> AzureHttpResponse:
        body = None
        request_headers = dict(headers)
        if json_body is not None:
            body = json.dumps(json_body, sort_keys=True, separators=(",", ":")).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds, context=self._ssl_context) as response:
                return AzureHttpResponse(
                    status_code=int(response.status),
                    headers={key.lower(): value for key, value in response.headers.items()},
                    payload=_decode_payload(response.read()),
                )
        except urllib.error.HTTPError as exc:
            return AzureHttpResponse(
                status_code=int(exc.code),
                headers={key.lower(): value for key, value in exc.headers.items()} if exc.headers else {},
                payload=_decode_payload(exc.read()),
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AzureRestError(f"Azure Management request failed: {_safe_error_text(exc)}") from exc


class AzurePolicyRestApi:
    def __init__(
        self,
        *,
        subscription_id: str,
        token_provider: AzureAccessTokenProvider,
        transport: AzureHttpTransport | None = None,
        management_endpoint: str = "https://management.azure.com",
        token_scope: str = ARM_RESOURCE_SCOPE,
        activity_subscription_ids: list[str] | None = None,
        timeout_seconds: float = 30.0,
        max_attempts: int = 4,
        max_collection_items: int = 50_000,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        endpoint = management_endpoint.rstrip("/")
        parsed = urllib.parse.urlparse(endpoint)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("Azure Management endpoint must be an absolute HTTPS URL.")
        self.subscription_id = subscription_id.strip()
        self.token_provider = token_provider
        self.transport = transport or UrllibAzureTransport()
        self.management_endpoint = endpoint
        self.token_scope = str(token_scope or ARM_RESOURCE_SCOPE).strip()
        self.activity_subscription_ids = [value.strip() for value in activity_subscription_ids or [] if value.strip()]
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.max_attempts = max(1, min(8, int(max_attempts)))
        self.max_collection_items = max(1, int(max_collection_items))
        self.sleeper = sleeper
        self._access_token = ""
        self._trusted_origin = (parsed.scheme.lower(), parsed.netloc.lower())

    def list_policy_definitions(self, scope: str) -> list[dict[str, Any]]:
        return self._list(f"{_scope(scope)}/providers/Microsoft.Authorization/policyDefinitions", POLICY_API_VERSION)

    def list_policy_set_definitions(self, scope: str) -> list[dict[str, Any]]:
        return self._list(f"{_scope(scope)}/providers/Microsoft.Authorization/policySetDefinitions", POLICY_API_VERSION)

    def list_policy_assignments(self, scope: str) -> list[dict[str, Any]]:
        return self._list(f"{_scope(scope)}/providers/Microsoft.Authorization/policyAssignments", POLICY_API_VERSION)

    def query_policy_states(self, scope: str) -> list[dict[str, Any]]:
        path = f"{_scope(scope)}/providers/Microsoft.PolicyInsights/policyStates/latest/queryResults"
        return self._list(path, POLICY_INSIGHTS_API_VERSION, method="POST", json_body={})

    def list_policy_activity(self, scope: str, since: datetime) -> list[dict[str, Any]]:
        subscriptions = self._activity_subscriptions(scope)
        until = datetime.now(timezone.utc)
        since_utc = _as_utc(since)
        filter_text = (
            f"eventTimestamp ge '{_iso_utc(since_utc)}' and "
            f"eventTimestamp le '{_iso_utc(until)}' and resourceProvider eq 'Microsoft.Authorization'"
        )
        events: list[dict[str, Any]] = []
        for subscription_id in subscriptions:
            path = f"/subscriptions/{subscription_id}/providers/Microsoft.Insights/eventtypes/management/values"
            events.extend(
                self._list(
                    path,
                    ACTIVITY_LOG_API_VERSION,
                    query={"$filter": filter_text},
                )
            )
            if len(events) > self.max_collection_items:
                raise AzureRestError("Azure Activity Log result exceeded the configured collection limit.")
        return events

    def _activity_subscriptions(self, scope: str) -> list[str]:
        parts = [part for part in _scope(scope).split("/") if part]
        if len(parts) >= 2 and parts[0].lower() == "subscriptions":
            return [parts[1]]
        if self.activity_subscription_ids:
            return list(dict.fromkeys(self.activity_subscription_ids))
        if self.subscription_id:
            return [self.subscription_id]
        raise AzureRestError("Management-group Activity Log monitoring requires activity_subscription_ids.")

    def _list(
        self,
        path: str,
        api_version: str,
        *,
        method: str = "GET",
        query: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        params = {"api-version": api_version, **(query or {})}
        url = f"{self.management_endpoint}{path}?{urllib.parse.urlencode(params)}"
        items: list[dict[str, Any]] = []
        page_count = 0
        while url:
            self._validate_url(url)
            payload = self._request(method, url, json_body=json_body)
            values = payload.get("value", [])
            if not isinstance(values, list):
                raise AzureRestError("Azure Management response did not contain a list value.")
            items.extend(value for value in values if isinstance(value, dict))
            if len(items) > self.max_collection_items:
                raise AzureRestError("Azure Management result exceeded the configured collection limit.")
            page_count += 1
            if page_count > 1000:
                raise AzureRestError("Azure Management pagination exceeded the page limit.")
            next_link = payload.get("nextLink") or payload.get("@odata.nextLink")
            url = str(next_link).strip() if next_link else ""
        return items

    def _request(self, method: str, url: str, *, json_body: dict[str, Any] | None) -> dict[str, Any]:
        token = self._token()
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "CGA-AzurePolicyMonitor/1.0",
        }
        response: AzureHttpResponse | None = None
        for attempt in range(1, self.max_attempts + 1):
            response = self.transport.request(
                method,
                url,
                headers=headers,
                json_body=json_body,
                timeout_seconds=self.timeout_seconds,
            )
            if response.status_code < 400:
                return response.payload
            if response.status_code not in {408, 429, 500, 502, 503, 504} or attempt == self.max_attempts:
                break
            retry_after = _retry_after(response.headers)
            self.sleeper(retry_after if retry_after is not None else min(2 ** (attempt - 1), 8))
        if response is None:
            raise AzureRestError("Azure Management request failed without a response.")
        error = response.payload.get("error") if isinstance(response.payload, dict) else None
        error_data = error if isinstance(error, dict) else {}
        code = _safe_error_text(error_data.get("code") or "AzureRequestFailed")
        message = _safe_error_text(error_data.get("message") or "Azure Management request failed.")
        raise AzureRestError(f"Azure Management request failed ({response.status_code}, {code}): {message}")

    def _token(self) -> str:
        if not self._access_token:
            self._access_token = str(self.token_provider.get_token(self.token_scope)).strip()
        if not self._access_token:
            raise AzureRestError("Azure credential returned an empty access token.")
        return self._access_token

    def _validate_url(self, url: str) -> None:
        parsed = urllib.parse.urlparse(url)
        if (parsed.scheme.lower(), parsed.netloc.lower()) != self._trusted_origin:
            raise AzureRestError("Azure Management returned an untrusted pagination URL.")


def _scope(value: str) -> str:
    text = "/" + str(value or "").strip().strip("/")
    if text == "/":
        raise ValueError("Azure resource scope is required.")
    return text


def _decode_payload(raw: bytes) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"error": {"code": "InvalidResponse", "message": "Azure returned a non-JSON response."}}
    return value if isinstance(value, dict) else {}


def _safe_error_text(value: Any) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    return text[:500]


def _retry_after(headers: dict[str, str]) -> float | None:
    value = headers.get("retry-after") or headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, min(float(value), 60.0))
    except (TypeError, ValueError):
        return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")