"""Import-free definitions of the optional first-party capabilities."""
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Plugin:
    id: str
    name: str
    description: str
    route: str = ""
    resources: tuple[str, ...] = ()
    pricing: Literal["free", "paid"] = "free"


CATALOG = {
    item.id: item
    for item in (
        Plugin("automation", "Automation", "Task management and execution history.", "/admin/schedule",
               ("src/backend/plugins/automation.py",)),
        Plugin("browseragent", "BrowserAgent", "Browser page tests, screenshots and inspection.",
               "/admin/schedule", ("src/backend/plugins/browseragent.py",)),
        Plugin("workflows", "Agents and workflows", "External agent activation and HTTP workflows.",
               "/admin/schedule", ("src/backend/plugins/workflows.py",)),
        Plugin("azure_policy_change_monitor", "Azure Policy Change Monitor",
               "Read-only policy repository and deployed-state monitoring.", "/admin/extensions",
               ("extensions/azure-policy-monitor",)),
        Plugin("briefing", "Work briefing", "Cross-project activity dashboard.", "/admin/briefing",
               ("src/frontend/plugins/briefing.js",)),
        Plugin("reports", "Reports", "Filtered activity and AI-First reports, interactive charts and PDF exports.",
               "/admin/audit/report", ("src/frontend/plugins/reports.js", "src/frontend/report-vendor")),
        Plugin("notifications", "Notifications", "Optional severity-gated webhook and email delivery.",
               resources=("src/backend/plugins/notifications.py",)),
        Plugin("evaluation", "Evaluation and benchmarks", "Context quality, token efficiency and trace evaluation.",
               resources=("src/backend/perf", "src/backend/eval")),
    )
}

TASK_PLUGINS = {
    "browseragent_task": "browseragent",
    "agent_activation": "workflows",
    "http_request": "workflows",
}
