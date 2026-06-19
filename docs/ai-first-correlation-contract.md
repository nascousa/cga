# AI-First Correlation Contract

Version: 1.30.99
Date: 2026-06-19

This contract defines how AI-first tasks should connect MCP tool calls, Work Briefing activity, evidence packs, PRs, and review/audit records.

## Canonical Fields

Use these field names whenever a caller can provide them:

- `task_id`: the stable task, story, or local work item id for the AI-first unit of work.
- `issue_id`: the external issue, PBI, bug, or work item id.
- `pr_id`: the pull request id or number.
- `activity_id`: the CGA Work Briefing activity id when linking to a specific activity.

The minimum recommended correlation for an AI-assisted change is `task_id`. Add `issue_id` and `pr_id` when they exist.

## MCP Tool Calls

Agent calls to CGA MCP read tools should include correlation fields when available:

```json
{
  "query": "how readiness evidence is generated",
  "limit": 10,
  "task_id": "TASK-123",
  "issue_id": "PBI-456",
  "pr_id": "789"
}
```

The initial supported tools are `retrieve_context`, `find_symbol`, and `find_call_graph`. The trace recorder stores the explicit correlation fields with the tool args, while evidence export only includes sanitized trace summaries.

## Work Briefing Activity

Work Briefing activities should include the same ids in `metadata`, and may also include the ids as tags for easier human scanning:

```json
{
  "project_id": "CGA123",
  "event_type": "validation",
  "external_id": "TASK-123-validation",
  "title": "Validated AI-first task",
  "status": "done",
  "tags": ["ai-first", "task:TASK-123", "pr:789"],
  "metadata": {
    "task_id": "TASK-123",
    "issue_id": "PBI-456",
    "pr_id": "789"
  }
}
```

## Evidence Packs

Task-bound evidence packs should be generated with the same filters:

```text
GET /api/admin/ai-first/evidence?project_id=CGA123&task_id=TASK-123&pr_id=789
```

Persist the pack when it is used for review, release evidence, retro, or audit:

```text
POST /api/admin/ai-first/evidence-packs
```

## Redaction Rules

- Do not store tokens, API keys, cookies, passwords, or raw secret-bearing tool arguments in evidence packs.
- Work Briefing metadata values are not exported; only metadata keys are shown.
- MCP trace export includes argument keys, explicit correlation fields, result counts, and short result previews only.

## Status

This contract is observe/warn in `1.30.99`. Current policy gates report saved evidence, CI, PR/review, benchmark, and Work Briefing status without blocking actions; future policy profiles can promote missing evidence from a warning to an enforced review gate.