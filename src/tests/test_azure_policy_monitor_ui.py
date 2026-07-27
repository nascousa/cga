from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path


FRONTEND = Path(__file__).resolve().parents[1] / "frontend" / "index.html"


class IdCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.footer_styles: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if attributes.get("id"):
            self.ids.add(str(attributes["id"]))
        if tag == "footer" and "footer" in str(attributes.get("class") or "").split():
            self.footer_styles.append(str(attributes.get("style") or ""))


def test_policy_monitor_admin_has_live_monitor_and_output_controls() -> None:
    markup = FRONTEND.read_text(encoding="utf-8")
    parser = IdCollector()
    parser.feed(markup)

    required_ids = {
        "extension-config-repo-enabled",
        "extension-config-azure-enabled",
        "extension-config-subscription-id",
        "extension-config-management-group-id",
        "extension-config-azure-scope",
        "extension-config-auth-mode",
        "extension-config-proxy-endpoint",
        "extension-config-proxy-key-env",
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
    assert '.topbar-spacer { display: none; }' in markup
    assert '.topbar-nav::-webkit-scrollbar { display: none; }' in markup
    assert parser.footer_styles
    assert "position:fixed" not in parser.footer_styles[-1].replace(" ", "").lower()
