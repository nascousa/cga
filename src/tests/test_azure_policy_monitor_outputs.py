# ruff: noqa: E402
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest


EXTENSION_SRC = Path(__file__).resolve().parents[2] / "extensions" / "azure-policy-monitor" / "src"
if str(EXTENSION_SRC) not in sys.path:
    sys.path.insert(0, str(EXTENSION_SRC))

from policy_monitor.notifications import deliver_notifications
from policy_monitor.summary import ModelHttpResponse, SummaryError, generate_grounded_summary


class RecordingModelTransport:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def post(self, url, *, headers, payload, timeout_seconds):
        self.requests.append({"url": url, "headers": headers, "payload": payload})
        return ModelHttpResponse(
            status_code=200,
            payload={
                "choices": [
                    {
                        "message": {
                            "content": """{
                                "headline": "A production assignment changed.",
                                "items": [
                                    {
                                        "evidence_id": "finding-001",
                                        "explanation": "Enforcement was enabled.",
                                        "recommended_action": "Review the assignment owner."
                                    },
                                    {
                                        "evidence_id": "invented-999",
                                        "explanation": "Invented claim.",
                                        "recommended_action": "Ignore."
                                    }
                                ]
                            }"""
                        }
                    }
                ]
            },
        )


class RecordingWebhookTransport:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def post(self, url, *, payload, timeout_seconds):
        self.requests.append({"url": url, "payload": payload, "timeout_seconds": timeout_seconds})


class SequenceModelTransport:
    def __init__(self, responses: list[ModelHttpResponse]) -> None:
        self.responses = responses
        self.requests: list[dict[str, Any]] = []
        self.call_count = 0

    def post(self, url, *, headers, payload, timeout_seconds):
        self.requests.append({"url": url, "headers": headers, "payload": payload})
        response = self.responses[self.call_count]
        self.call_count += 1
        return response


def _critical_result() -> dict[str, Any]:
    return {
        "extension_id": "azure_policy_change_monitor",
        "severity": "critical",
        "summary": {
            "finding_count": 1,
            "azure": {"scope": "/subscriptions/sub", "non_compliant_delta": 2},
        },
        "findings": [
            {
                "source": "azure",
                "check": "enforcement_mode_change",
                "severity": "critical",
                "message": "Azure Policy assignment enforcement mode changed.",
                "resource_id": "/subscriptions/sub/providers/microsoft.authorization/policyassignments/baseline",
                "evidence": {"previous_mode": "DoNotEnforce", "current_mode": "Default"},
            }
        ],
    }


def test_model_summary_discards_claims_without_known_evidence_ids() -> None:
    transport = RecordingModelTransport()

    summary = generate_grounded_summary(
        _critical_result(),
        {
            "model_endpoint": "https://foundry.example/openai/v1",
            "model_name": "review-model",
            "model_auth_mode": "api_key",
            "model_api_key_env": "POLICY_MODEL_KEY",
        },
        environment={"POLICY_MODEL_KEY": "never-persist-model-key"},
        transport=transport,
    )

    assert summary == {
        "status": "generated",
        "grounded": True,
        "headline": "A production assignment changed.",
        "items": [
            {
                "evidence_id": "finding-001",
                "explanation": "Enforcement was enabled.",
                "recommended_action": "Review the assignment owner.",
            }
        ],
    }
    assert transport.requests[0]["headers"]["api-key"] == "never-persist-model-key"
    assert "never-persist-model-key" not in str(summary)


def test_notifications_send_webhook_and_smtp_without_returning_secrets() -> None:
    webhook = RecordingWebhookTransport()
    sent_email: list[dict[str, Any]] = []

    status = deliver_notifications(
        _critical_result(),
        {
            "notifications_enabled": True,
            "notification_min_severity": "high",
            "notification_webhook_env": "POLICY_WEBHOOK_URL",
            "notification_email_recipients": ["policy-ops@example.com"],
        },
        environment={"POLICY_WEBHOOK_URL": "https://alerts.example/secret-hook"},
        webhook_transport=webhook,
        smtp_config={
            "enabled": True,
            "host": "smtp.example.com",
            "port": 587,
            "security": "starttls",
            "username": "mailer",
            "password": "never-persist-smtp-password",
            "from_email": "cga@example.com",
            "from_name": "CGA",
        },
        smtp_sender=lambda config, recipients, subject, text: sent_email.append(
            {"config": config, "recipients": recipients, "subject": subject, "text": text}
        ),
    )

    assert status == {
        "status": "sent",
        "threshold": "high",
        "channels": {"webhook": "sent", "email": "sent"},
    }
    assert webhook.requests[0]["url"] == "https://alerts.example/secret-hook"
    assert sent_email[0]["recipients"] == ["policy-ops@example.com"]
    assert "secret-hook" not in str(status)
    assert "never-persist-smtp-password" not in str(status)


def test_model_summary_redacts_sensitive_evidence_and_retries_throttling() -> None:
    result = _critical_result()
    result["findings"][0]["evidence"]["client_secret"] = "never-send-this-secret"
    transport = SequenceModelTransport(
        [
            ModelHttpResponse(429, {"error": {"code": "TooManyRequests"}}),
            ModelHttpResponse(
                200,
                {
                    "choices": [
                        {
                            "message": {
                                "content": '{"headline":"Review drift.","items":[]}'
                            }
                        }
                    ]
                },
            ),
        ]
    )
    sleeps: list[float] = []

    summary = generate_grounded_summary(
        result,
        {
            "model_endpoint": "https://foundry.example/openai/v1",
            "model_auth_mode": "api_key",
            "model_api_key_env": "POLICY_MODEL_KEY",
            "model_max_attempts": 2,
        },
        environment={"POLICY_MODEL_KEY": "model-key"},
        transport=transport,
        sleeper=sleeps.append,
    )

    assert summary["status"] == "generated"
    assert transport.call_count == 2
    assert sleeps == [1.0]
    assert "never-send-this-secret" not in str(transport.requests)
    assert "<redacted>" in str(transport.requests)


def test_model_summary_rejects_non_https_endpoint_before_transport() -> None:
    transport = RecordingModelTransport()

    with pytest.raises(SummaryError, match="HTTPS"):
        generate_grounded_summary(
            _critical_result(),
            {
                "model_endpoint": "http://model.example/v1",
                "model_auth_mode": "api_key",
                "model_api_key_env": "POLICY_MODEL_KEY",
            },
            environment={"POLICY_MODEL_KEY": "model-key"},
            transport=transport,
        )

    assert transport.requests == []


def test_model_summary_supports_bearer_api_key_header() -> None:
    transport = RecordingModelTransport()

    generate_grounded_summary(
        _critical_result(),
        {
            "model_endpoint": "https://api.openai.example/v1",
            "model_auth_mode": "api_key",
            "model_api_key_header": "authorization",
            "model_api_key_env": "POLICY_MODEL_KEY",
        },
        environment={"POLICY_MODEL_KEY": "model-key"},
        transport=transport,
    )

    assert transport.requests[0]["headers"] == {"Authorization": "Bearer model-key"}


def test_notifications_below_threshold_do_not_send() -> None:
    webhook = RecordingWebhookTransport()
    result = _critical_result()
    result["severity"] = "medium"

    status = deliver_notifications(
        result,
        {
            "notifications_enabled": True,
            "notification_min_severity": "high",
            "notification_webhook_env": "POLICY_WEBHOOK_URL",
        },
        environment={"POLICY_WEBHOOK_URL": "https://alerts.example/hook"},
        webhook_transport=webhook,
    )

    assert status == {"status": "below_threshold", "threshold": "high", "channels": {}}
    assert webhook.requests == []


def test_notifications_reject_non_https_webhook_without_disclosing_url() -> None:
    status = deliver_notifications(
        _critical_result(),
        {
            "notifications_enabled": True,
            "notification_min_severity": "high",
            "notification_webhook_env": "POLICY_WEBHOOK_URL",
        },
        environment={"POLICY_WEBHOOK_URL": "http://alerts.example/secret-hook"},
        webhook_transport=RecordingWebhookTransport(),
    )

    assert status == {
        "status": "failed",
        "threshold": "high",
        "channels": {"webhook": "failed:NotificationError"},
    }
    assert "secret-hook" not in str(status)
