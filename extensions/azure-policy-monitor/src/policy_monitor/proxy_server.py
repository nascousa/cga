"""Minimal managed-identity HTTP service for read-only Azure Policy collection."""
from __future__ import annotations

import hmac
import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from uuid import UUID

from .azure_auth import AzureTokenError, DefaultAzureAccessTokenProvider
from .azure_proxy import PROXY_KEY_HEADER, PROXY_PATH
from .azure_rest import AzurePolicyRestApi, AzureRestError
from .azure_state import AzurePolicyApi

LOGGER = logging.getLogger("cga.azure_policy_proxy")
MAX_REQUEST_BYTES = 16 * 1024


@dataclass(frozen=True)
class AzurePolicyProxyServerConfig:
    subscription_id: str
    shared_key: str
    managed_identity_client_id: str = ""
    port: int = 8080
    max_activity_lookback_minutes: int = 1440
    max_response_bytes: int = 25 * 1024 * 1024
    azure_timeout_seconds: int = 30
    azure_max_attempts: int = 4
    max_collection_items: int = 50_000

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> AzurePolicyProxyServerConfig:
        values = environment if environment is not None else os.environ
        subscription_id = _canonical_uuid(values.get("TARGET_SUBSCRIPTION_ID", ""), "TARGET_SUBSCRIPTION_ID")
        shared_key = str(values.get("PROXY_SHARED_KEY", "") or "").strip()
        if len(shared_key) < 32 or len(shared_key) > 512 or "\r" in shared_key or "\n" in shared_key:
            raise ValueError("PROXY_SHARED_KEY must contain between 32 and 512 safe characters.")
        return cls(
            subscription_id=subscription_id,
            shared_key=shared_key,
            managed_identity_client_id=str(values.get("AZURE_CLIENT_ID", "") or "").strip(),
            port=_bounded_integer(values.get("PORT"), 8080, 1, 65_535, "PORT"),
            max_activity_lookback_minutes=_bounded_integer(
                values.get("MAX_ACTIVITY_LOOKBACK_MINUTES"),
                1440,
                1,
                43_200,
                "MAX_ACTIVITY_LOOKBACK_MINUTES",
            ),
            max_response_bytes=_bounded_integer(
                values.get("MAX_RESPONSE_BYTES"),
                25 * 1024 * 1024,
                1024,
                50 * 1024 * 1024,
                "MAX_RESPONSE_BYTES",
            ),
            azure_timeout_seconds=_bounded_integer(
                values.get("AZURE_TIMEOUT_SECONDS"), 30, 1, 120, "AZURE_TIMEOUT_SECONDS"
            ),
            azure_max_attempts=_bounded_integer(values.get("AZURE_MAX_ATTEMPTS"), 4, 1, 8, "AZURE_MAX_ATTEMPTS"),
            max_collection_items=_bounded_integer(
                values.get("MAX_COLLECTION_ITEMS"), 50_000, 1, 250_000, "MAX_COLLECTION_ITEMS"
            ),
        )

    @property
    def scope(self) -> str:
        return f"/subscriptions/{self.subscription_id}"


class AzurePolicyProxyService:
    def __init__(self, config: AzurePolicyProxyServerConfig, api: AzurePolicyApi) -> None:
        self.config = config
        self.api = api

    def execute(
        self,
        supplied_key: str,
        payload: Any,
        *,
        now: datetime | None = None,
    ) -> tuple[int, dict[str, Any]]:
        if not hmac.compare_digest(str(supplied_key or ""), self.config.shared_key):
            return _error(401, "Unauthorized", "Authentication failed.")
        if not isinstance(payload, dict) or set(payload) - {"operation", "scope", "since"}:
            return _error(400, "InvalidRequest", "Request body is invalid.")
        operation = str(payload.get("operation") or "").strip()
        scope = "/" + str(payload.get("scope") or "").strip().strip("/")
        if scope.lower() != self.config.scope.lower():
            return _error(403, "ScopeNotAllowed", "The requested Azure scope is not allowed.")

        try:
            if operation == "list_policy_definitions":
                items = self.api.list_policy_definitions(self.config.scope)
            elif operation == "list_policy_set_definitions":
                items = self.api.list_policy_set_definitions(self.config.scope)
            elif operation == "list_policy_assignments":
                items = self.api.list_policy_assignments(self.config.scope)
            elif operation == "list_policy_activity":
                since = self._activity_since(payload.get("since"), now=now)
                items = self.api.list_policy_activity(self.config.scope, since)
            else:
                return _error(400, "OperationNotAllowed", "The requested operation is not allowed.")
        except ValueError:
            return _error(400, "InvalidRequest", "Request parameters are invalid.")
        except (AzureRestError, AzureTokenError) as exc:
            LOGGER.error("Azure Policy proxy upstream request failed error_type=%s", type(exc).__name__)
            return _error(502, "AzureUpstreamError", "Azure read operation failed.")

        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            return _error(502, "InvalidAzureResponse", "Azure read operation returned invalid data.")
        response = {"items": items}
        encoded = json.dumps(response, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        if len(encoded) > self.config.max_response_bytes:
            return _error(502, "ResponseTooLarge", "Azure read operation exceeded the response limit.")
        return 200, response

    def _activity_since(self, value: Any, *, now: datetime | None) -> datetime:
        text = str(value or "").strip()
        if not text:
            raise ValueError("Activity Log queries require since.")
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("Activity Log since is invalid.") from exc
        if parsed.tzinfo is None:
            raise ValueError("Activity Log since must include a timezone.")
        since = parsed.astimezone(timezone.utc)
        current = now or datetime.now(timezone.utc)
        current = current if current.tzinfo is not None else current.replace(tzinfo=timezone.utc)
        current = current.astimezone(timezone.utc)
        if since > current + timedelta(minutes=5):
            raise ValueError("Activity Log since cannot be in the future.")
        if since < current - timedelta(minutes=self.config.max_activity_lookback_minutes):
            raise ValueError("Activity Log since exceeds the configured lookback.")
        return since


class AzurePolicyProxyRequestHandler(BaseHTTPRequestHandler):
    server_version = "CGA-AzurePolicyProxy"
    sys_version = ""
    service: AzurePolicyProxyService

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send_json(200, {"status": "ok"})
            return
        self._send_json(404, _error_payload("NotFound", "Resource not found."))

    def do_POST(self) -> None:
        if self.path != PROXY_PATH:
            self._send_json(404, _error_payload("NotFound", "Resource not found."))
            return
        try:
            content_length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._send_json(400, _error_payload("InvalidRequest", "Content length is invalid."))
            return
        if content_length < 1 or content_length > MAX_REQUEST_BYTES:
            self._send_json(413, _error_payload("RequestTooLarge", "Request body exceeds the allowed size."))
            return
        raw = self.rfile.read(content_length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, _error_payload("InvalidRequest", "Request body must be JSON."))
            return
        status, response = self.service.execute(self.headers.get(PROXY_KEY_HEADER, ""), payload)
        self._send_json(status, response)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format_text: str, *args: Any) -> None:
        LOGGER.info("http_request " + format_text, *args)


def create_service(config: AzurePolicyProxyServerConfig) -> AzurePolicyProxyService:
    token_provider = DefaultAzureAccessTokenProvider(
        auth_mode="managed_identity",
        managed_identity_client_id=config.managed_identity_client_id,
        timeout_seconds=config.azure_timeout_seconds,
    )
    api = AzurePolicyRestApi(
        subscription_id=config.subscription_id,
        token_provider=token_provider,
        timeout_seconds=config.azure_timeout_seconds,
        max_attempts=config.azure_max_attempts,
        max_collection_items=config.max_collection_items,
    )
    return AzurePolicyProxyService(config, api)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config = AzurePolicyProxyServerConfig.from_environment()
    handler = type(
        "ConfiguredAzurePolicyProxyRequestHandler",
        (AzurePolicyProxyRequestHandler,),
        {"service": create_service(config)},
    )
    server = ThreadingHTTPServer(("0.0.0.0", config.port), handler)
    LOGGER.info("Azure Policy proxy listening on port %d for one subscription scope", config.port)
    server.serve_forever()


def _canonical_uuid(value: Any, field: str) -> str:
    try:
        parsed = UUID(str(value or "").strip())
    except ValueError as exc:
        raise ValueError(f"{field} must be a UUID.") from exc
    return str(parsed)


def _bounded_integer(value: Any, default: int, minimum: int, maximum: int, field: str) -> int:
    try:
        number = default if value is None or value == "" else int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer.") from exc
    if number < minimum or number > maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}.")
    return number


def _error(status: int, code: str, message: str) -> tuple[int, dict[str, Any]]:
    return status, _error_payload(code, message)


def _error_payload(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


if __name__ == "__main__":
    main()
