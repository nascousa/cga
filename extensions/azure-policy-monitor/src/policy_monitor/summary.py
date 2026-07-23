"""Optional evidence-grounded summaries for Azure Policy monitor results."""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from .azure_auth import DefaultAzureAccessTokenProvider
from .azure_rest import AzureAccessTokenProvider


ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
DEFAULT_MODEL_TOKEN_SCOPE = "https://cognitiveservices.azure.com/.default"
RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}
SENSITIVE_KEY_TERMS = (
    "accesskey",
    "apikey",
    "assertion",
    "credential",
    "password",
    "privatekey",
    "secret",
    "token",
)


class SummaryError(RuntimeError):
    pass


class ModelTransport(Protocol):
    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> "ModelHttpResponse": ...


@dataclass(frozen=True)
class ModelHttpResponse:
    status_code: int
    payload: dict[str, Any]


class UrllibModelTransport:
    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> ModelHttpResponse:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8"),
            headers={"Accept": "application/json", "Content-Type": "application/json", **headers},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                return ModelHttpResponse(int(response.status), _decode_payload(response.read()))
        except urllib.error.HTTPError as exc:
            return ModelHttpResponse(int(exc.code), _decode_payload(exc.read()))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SummaryError(f"Model endpoint is unavailable: {type(exc).__name__}.") from exc


def generate_grounded_summary(
    result: dict[str, Any],
    config: dict[str, Any],
    *,
    environment: Mapping[str, str] | None = None,
    transport: ModelTransport | None = None,
    token_provider: AzureAccessTokenProvider | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    findings = [item for item in result.get("findings", []) if isinstance(item, dict)][:100]
    if not findings:
        return {
            "status": "not_needed",
            "grounded": True,
            "headline": "No policy monitor findings were detected.",
            "items": [],
        }
    endpoint = str(config.get("model_endpoint") or "").strip()
    if not endpoint:
        raise SummaryError("model_endpoint is required when model summaries are enabled.")
    url = _model_url(endpoint, config)
    evidence = {
        f"finding-{index:03d}": {
            "source": item.get("source"),
            "check": item.get("check"),
            "severity": item.get("severity"),
            "message": item.get("message"),
            "resource_id": item.get("resource_id"),
            "file": item.get("file"),
            "evidence": _sanitize_for_model(item.get("evidence")),
        }
        for index, item in enumerate(findings, start=1)
    }
    request_payload: dict[str, Any] = {
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "Summarize Azure Policy monitor evidence. Return JSON with headline and items. "
                    "Each item must contain exactly evidence_id, explanation, and recommended_action. "
                    "Use only supplied evidence IDs and do not introduce facts absent from that evidence."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {"monitor_summary": _sanitize_for_model(result.get("summary", {})), "evidence": evidence},
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            },
        ],
    }
    model_name = str(config.get("model_name") or config.get("model_deployment") or "").strip()
    if model_name:
        request_payload["model"] = model_name
    headers = _model_headers(config, environment or os.environ, token_provider)
    resolved_transport = transport or UrllibModelTransport()
    timeout_seconds = float(
        _bounded_int(config.get("model_timeout_seconds"), 60, 1, 180, "model_timeout_seconds")
    )
    max_attempts = _bounded_int(config.get("model_max_attempts"), 3, 1, 5, "model_max_attempts")
    response: ModelHttpResponse | None = None
    for attempt in range(max_attempts):
        try:
            response = resolved_transport.post(
                url,
                headers=headers,
                payload=request_payload,
                timeout_seconds=timeout_seconds,
            )
        except SummaryError:
            if attempt + 1 >= max_attempts:
                raise
            sleeper(float(2**attempt))
            continue
        if response.status_code not in RETRYABLE_STATUS_CODES or attempt + 1 >= max_attempts:
            break
        sleeper(float(2**attempt))
    if response is None:
        raise SummaryError("Model summary request did not return a response.")
    if response.status_code >= 400:
        code = _error_code(response.payload)
        raise SummaryError(f"Model summary request failed ({response.status_code}, {code}).")
    content = _response_content(response.payload)
    parsed = _parse_json_object(content)
    valid_items: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in parsed.get("items", []):
        if not isinstance(item, dict):
            continue
        evidence_id = _text(item.get("evidence_id"), 80)
        if evidence_id not in evidence or evidence_id in seen:
            continue
        seen.add(evidence_id)
        valid_items.append(
            {
                "evidence_id": evidence_id,
                "explanation": _text(item.get("explanation"), 1000),
                "recommended_action": _text(item.get("recommended_action"), 1000),
            }
        )
        if len(valid_items) >= 25:
            break
    return {
        "status": "generated",
        "grounded": True,
        "headline": _text(parsed.get("headline"), 500),
        "items": valid_items,
    }


def _model_url(endpoint: str, config: dict[str, Any]) -> str:
    parsed = urllib.parse.urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise SummaryError("model_endpoint must be an absolute HTTPS URL without embedded credentials.")
    base = endpoint.rstrip("/")
    if base.endswith("/chat/completions"):
        url = base
    elif config.get("model_deployment") and "/openai/deployments/" not in base:
        deployment = urllib.parse.quote(str(config["model_deployment"]), safe="")
        url = f"{base}/openai/deployments/{deployment}/chat/completions"
    else:
        url = f"{base}/chat/completions"
    api_version = str(config.get("model_api_version") or "").strip()
    if api_version and "api-version=" not in url:
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}{urllib.parse.urlencode({'api-version': api_version})}"
    return url


def _model_headers(
    config: dict[str, Any],
    environment: Mapping[str, str],
    token_provider: AzureAccessTokenProvider | None,
) -> dict[str, str]:
    mode = str(config.get("model_auth_mode") or "auto").strip().lower()
    if mode not in {"auto", "api_key", "azure"}:
        raise SummaryError("model_auth_mode must be auto, api_key, or azure.")
    env_name = str(config.get("model_api_key_env") or "AZURE_POLICY_MONITOR_MODEL_API_KEY").strip()
    if not ENV_NAME.fullmatch(env_name):
        raise SummaryError("model_api_key_env must be an uppercase environment variable name.")
    api_key = str(environment.get(env_name, "") or "").strip()
    if mode == "api_key" or (mode == "auto" and api_key):
        if not api_key:
            raise SummaryError(f"Model API key environment variable {env_name} is not set.")
        header = str(config.get("model_api_key_header") or "api-key").strip().lower()
        if header == "api-key":
            return {"api-key": api_key}
        if header == "authorization":
            return {"Authorization": f"Bearer {api_key}"}
        raise SummaryError("model_api_key_header must be api-key or authorization.")
    provider = token_provider or DefaultAzureAccessTokenProvider(
        auth_mode=str(config.get("auth_mode") or "auto"),
        authority_host=str(config.get("authority_host") or "https://login.microsoftonline.com"),
        managed_identity_client_id=str(config.get("managed_identity_client_id") or ""),
    )
    scope = str(config.get("model_token_scope") or DEFAULT_MODEL_TOKEN_SCOPE)
    return {"Authorization": f"Bearer {provider.get_token(scope)}"}


def _response_content(payload: dict[str, Any]) -> str:
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise SummaryError("Model response did not contain choices[0].message.content.") from exc
    if not isinstance(content, str):
        raise SummaryError("Model response content must be text.")
    return content


def _parse_json_object(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SummaryError("Model summary was not valid JSON.") from exc
    if not isinstance(value, dict):
        raise SummaryError("Model summary must be a JSON object.")
    return value


def _decode_payload(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _error_code(payload: dict[str, Any]) -> str:
    error = payload.get("error")
    if isinstance(error, dict):
        value = _text(error.get("code"), 100).lower()
        known_codes = {
            "badrequest",
            "content_filter",
            "invalidrequesterror",
            "ratelimitexceeded",
            "resourcenotfound",
            "toomanyrequests",
            "unauthorized",
        }
        return value if value in known_codes else "model_error"
    return "model_error"


def _text(value: Any, limit: int) -> str:
    return str(value or "").replace("\r", " ").replace("\n", " ").strip()[:limit]


def _sanitize_for_model(value: Any, *, depth: int = 0) -> Any:
    if depth >= 6:
        return "<truncated>"
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key in sorted(value, key=str)[:100]:
            text_key = str(key)[:200]
            compact_key = re.sub(r"[^a-z0-9]", "", text_key.lower())
            sanitized[text_key] = (
                "<redacted>"
                if any(term in compact_key for term in SENSITIVE_KEY_TERMS)
                else _sanitize_for_model(value[key], depth=depth + 1)
            )
        return sanitized
    if isinstance(value, list):
        return [_sanitize_for_model(item, depth=depth + 1) for item in value[:100]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value[:2000] if isinstance(value, str) else value
    return str(value)[:2000]


def _bounded_int(value: Any, default: int, minimum: int, maximum: int, field: str) -> int:
    try:
        number = int(value if value not in {None, ""} else default)
    except (TypeError, ValueError) as exc:
        raise SummaryError(f"{field} must be an integer.") from exc
    if number < minimum or number > maximum:
        raise SummaryError(f"{field} must be between {minimum} and {maximum}.")
    return number