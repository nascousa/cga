from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path


FRONTEND = Path(__file__).resolve().parents[1] / "frontend" / "index.html"


class IdCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if attributes.get("id"):
            self.ids.add(str(attributes["id"]))


def test_policy_monitor_admin_has_live_monitor_and_output_controls() -> None:
    parser = IdCollector()
    parser.feed(FRONTEND.read_text(encoding="utf-8"))

    required_ids = {
        "extension-config-repo-enabled",
        "extension-config-azure-enabled",
        "extension-config-subscription-id",
        "extension-config-management-group-id",
        "extension-config-azure-scope",
        "extension-config-auth-mode",
        "extension-config-management-endpoint",
        "extension-config-authority-host",
        "extension-config-managed-identity-client-id",
        "extension-config-activity-subscriptions",
        "extension-config-include-compliance",
        "extension-config-include-activity",
        "extension-config-activity-lookback",
        "extension-config-retention",
        "extension-config-model-enabled",
        "extension-config-model-endpoint",
        "extension-config-model-auth-mode",
        "extension-config-model-api-key-env",
        "extension-config-notifications-enabled",
        "extension-config-notification-threshold",
        "extension-config-webhook-env",
        "extension-config-email-recipients",
        "extension-output-summary",
        "extension-output-notifications",
    }

    assert required_ids <= parser.ids