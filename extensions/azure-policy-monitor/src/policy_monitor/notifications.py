"""Severity-gated webhook and SMTP notifications for monitor results."""
from __future__ import annotations

import json
import os
import re
import smtplib
import ssl
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Any, Callable, Mapping, Protocol


SEVERITY_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")


class NotificationError(RuntimeError):
    pass


class WebhookTransport(Protocol):
    def post(self, url: str, *, payload: dict[str, Any], timeout_seconds: float) -> None: ...


class UrllibWebhookTransport:
    def post(self, url: str, *, payload: dict[str, Any], timeout_seconds: float) -> None:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "CGA-AzurePolicyMonitor/1.0"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                if int(response.status) >= 400:
                    raise NotificationError(f"Webhook returned HTTP {int(response.status)}.")
        except urllib.error.HTTPError as exc:
            raise NotificationError(f"Webhook returned HTTP {int(exc.code)}.") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise NotificationError(f"Webhook is unavailable: {type(exc).__name__}.") from exc


SmtpSender = Callable[[dict[str, Any], list[str], str, str], None]


def deliver_notifications(
    result: dict[str, Any],
    config: dict[str, Any],
    *,
    environment: Mapping[str, str] | None = None,
    webhook_transport: WebhookTransport | None = None,
    smtp_config: dict[str, Any] | None = None,
    smtp_sender: SmtpSender | None = None,
) -> dict[str, Any]:
    threshold = str(config.get("notification_min_severity") or "high").strip().lower()
    if threshold not in SEVERITY_ORDER:
        raise NotificationError("notification_min_severity is invalid.")
    if not _bool(config.get("notifications_enabled"), False):
        return {"status": "disabled", "threshold": threshold, "channels": {}}
    severity = str(result.get("severity") or "info").lower()
    if SEVERITY_ORDER.get(severity, 0) < SEVERITY_ORDER[threshold]:
        return {"status": "below_threshold", "threshold": threshold, "channels": {}}

    channels: dict[str, str] = {}
    env = environment or os.environ
    webhook_env = str(config.get("notification_webhook_env") or "").strip()
    if webhook_env:
        try:
            if not ENV_NAME.fullmatch(webhook_env):
                raise NotificationError("notification_webhook_env is invalid.")
            webhook_url = str(env.get(webhook_env, "") or "").strip()
            _validate_webhook_url(webhook_url)
            (webhook_transport or UrllibWebhookTransport()).post(
                webhook_url,
                payload=_webhook_payload(result),
                timeout_seconds=float(_bounded_int(config.get("notification_timeout_seconds"), 20, 1, 60)),
            )
            channels["webhook"] = "sent"
        except Exception as exc:
            channels["webhook"] = f"failed:{type(exc).__name__}"

    recipients = _email_recipients(config.get("notification_email_recipients"))
    if recipients:
        try:
            delivery_config = smtp_config or {}
            if not _bool(delivery_config.get("enabled"), False):
                raise NotificationError("SMTP is not enabled.")
            subject, text = _email_content(result)
            (smtp_sender or _send_smtp)(delivery_config, recipients, subject, text)
            channels["email"] = "sent"
        except Exception as exc:
            channels["email"] = f"failed:{type(exc).__name__}"

    sent = sum(value == "sent" for value in channels.values())
    failed = sum(value.startswith("failed:") for value in channels.values())
    if sent and not failed:
        status = "sent"
    elif sent:
        status = "partial"
    elif failed:
        status = "failed"
    else:
        status = "no_channels"
    return {"status": status, "threshold": threshold, "channels": channels}


def _webhook_payload(result: dict[str, Any]) -> dict[str, Any]:
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    azure = summary.get("azure") if isinstance(summary.get("azure"), dict) else {}
    findings = [item for item in result.get("findings", []) if isinstance(item, dict)][:10]
    lines = [
        f"[{str(item.get('severity') or 'info').upper()}] {str(item.get('message') or item.get('check') or '')[:300]}"
        for item in findings
    ]
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {
                            "type": "TextBlock",
                            "weight": "Bolder",
                            "size": "Medium",
                            "text": "Azure Policy Change Monitor",
                        },
                        {
                            "type": "FactSet",
                            "facts": [
                                {"title": "Severity", "value": str(result.get("severity") or "info")},
                                {"title": "Findings", "value": str(summary.get("finding_count") or 0)},
                                {"title": "Scope", "value": str(azure.get("scope") or "Repository")[:500]},
                            ],
                        },
                        {"type": "TextBlock", "wrap": True, "text": "\n".join(lines) or "No findings."},
                    ],
                },
            }
        ],
    }


def _email_content(result: dict[str, Any]) -> tuple[str, str]:
    severity = str(result.get("severity") or "info").upper()
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    azure = summary.get("azure") if isinstance(summary.get("azure"), dict) else {}
    lines = [
        "Azure Policy Change Monitor",
        f"Severity: {severity}",
        f"Scope: {azure.get('scope') or 'Repository'}",
        f"Findings: {summary.get('finding_count') or 0}",
        "",
    ]
    for item in [value for value in result.get("findings", []) if isinstance(value, dict)][:25]:
        lines.append(
            f"[{str(item.get('severity') or 'info').upper()}] "
            f"{str(item.get('message') or item.get('check') or '')[:500]}"
        )
    return f"[{severity}] Azure Policy Change Monitor", "\n".join(lines)


def _send_smtp(config: dict[str, Any], recipients: list[str], subject: str, text: str) -> None:
    host = str(config.get("host") or "").strip()
    from_email = str(config.get("from_email") or "").strip()
    if not host or not from_email:
        raise NotificationError("SMTP host and from_email are required.")
    port = _bounded_int(config.get("port"), 587, 1, 65535)
    security = str(config.get("security") or "starttls").strip().lower()
    if security not in {"none", "starttls", "ssl"}:
        raise NotificationError("SMTP security mode is invalid.")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = f"{str(config.get('from_name') or '').strip()} <{from_email}>" if config.get("from_name") else from_email
    message["To"] = ", ".join(recipients)
    message.set_content(text)
    context = ssl.create_default_context()
    smtp_type = smtplib.SMTP_SSL if security == "ssl" else smtplib.SMTP
    with smtp_type(host, port, timeout=20, context=context) if security == "ssl" else smtp_type(host, port, timeout=20) as client:
        if security == "starttls":
            client.starttls(context=context)
        username = str(config.get("username") or "")
        password = str(config.get("password") or "")
        if username:
            client.login(username, password)
        client.send_message(message)


def _validate_webhook_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise NotificationError("Webhook URL must be an absolute HTTPS URL without embedded credentials.")


def _email_recipients(value: Any) -> list[str]:
    if value is None:
        return []
    raw_values = value.split(",") if isinstance(value, str) else value if isinstance(value, list) else []
    recipients: list[str] = []
    for raw in raw_values:
        address = parseaddr(str(raw).strip())[1]
        if address and "@" in address and len(address) <= 320:
            recipients.append(address)
    return list(dict.fromkeys(recipients))


def _bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "1", "yes", "on"}:
        return True
    if isinstance(value, str) and value.strip().lower() in {"false", "0", "no", "off"}:
        return False
    raise NotificationError("Boolean notification configuration value is invalid.")


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value if value not in {None, ""} else default)
    except (TypeError, ValueError) as exc:
        raise NotificationError("Integer notification configuration value is invalid.") from exc
    if number < minimum or number > maximum:
        raise NotificationError(f"Notification value must be between {minimum} and {maximum}.")
    return number