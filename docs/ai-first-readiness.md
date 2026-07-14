# AI-First Readiness And Evidence MVP

Version: 1.30.112
Date: 2026-07-01

This MVP starts the CGA AI-first control-plane work with two admin-only APIs. The first measures whether a project has the basic engineering conditions for AI-first work. The second exports an observe-only evidence pack for review, team planning, or retrospectives.

Admin UI:

```text
http://localhost:18001/admin/ai-first
```

The cross-project readiness view renders project cards collapsed by default so the page stays scan-friendly when many projects are active. Expanding a project is remembered per browser session as an explicit expanded-project preference.

## Readiness Snapshot

Endpoint:

```text
GET /api/admin/ai-first/readiness?project_id=<project_id_or_name>
```

If `project_id` is omitted, the response includes all active projects visible to the admin database query.

The snapshot currently scores five dimensions:

- Context Readiness: repository path, ADC core files, graph counts, and latest index job.
- Verification Readiness: local test surface and CI workflow files.
- Workflow Readiness: Work Briefing activity and Learn / Prepare / Execute evidence placeholders.
- Governance Readiness: active MCP token and observe-only policy profile.
- ROI & Outcome Readiness: graph availability for token/HPS benchmarking and placeholder outcome metrics.

Signals are explainable and may be `ok`, `warn`, `fail`, or `unknown`. Unknown data is not treated as fake precision; it means CGA does not yet have that evidence.

## Evidence Pack V0

Endpoint:

```text
GET /api/admin/ai-first/evidence?project_id=<project_id_or_name>&limit=25
```

Optional task-bound correlation filters:

```text
task_id=<task-or-story-id>
issue_id=<issue-or-pbi-id>
pr_id=<pull-request-id>
activity_id=<work-briefing-activity-id>
```

The evidence pack includes:

- `schema_version`: currently `ai-first-evidence-pack.v0`.
- `policy_profile`: observe-only / L0 record-only defaults.
- `correlation`: project-level or task-bound mode plus filters and match counts.
- `project`: project identity and repository path.
- `readiness`: readiness summary, dimensions, and next actions.
- `activity_evidence`: sanitized recent Work Briefing activities.
- `signal_evidence`: sanitized matching PR, CI, and benchmark signals.
- `trace_evidence`: sanitized matching MCP trace summaries when tools carry matching correlation fields.
- `markdown`: a copy-ready Markdown rendering for planning notes, PRs, or retrospectives.

Activity export is summary-only. Raw metadata values are not exported; only metadata keys are included so evidence packs do not leak token-like fields. Trace export is also summary-only: evidence packs include argument keys, explicit correlation values, result counts, and short result previews rather than raw trace payloads.

## Persisted Evidence Packs

Save a generated evidence pack:

```text
POST /api/admin/ai-first/evidence-packs
```

Body:

```json
{
	"project_id": "CGA123",
	"task_id": "TASK-123",
	"issue_id": "PBI-456",
	"pr_id": "789",
	"activity_id": "optional-work-activity-id",
	"limit": 25
}
```

List saved packs:

```text
GET /api/admin/ai-first/evidence-packs?project_id=<project_id_or_name>&limit=25
```

Load one saved pack:

```text
GET /api/admin/ai-first/evidence-packs/<evidence_id>
```

Persisted packs store the sanitized JSON evidence and Markdown snapshot in the auth database under `ai_first_evidence_packs`.

## Policy Profile V0

Policy profiles are project-level configuration stored in `ai_first_policy_profiles`. They are observe/warn metadata today and do not block workflows yet.

List profiles and definitions:

```text
GET /api/admin/ai-first/policy-profiles?project_id=<project_id_or_name>
```

Update a project profile:

```text
PATCH /api/admin/ai-first/policy-profiles
```

Body:

```json
{
	"project_id": "CGA123",
	"profile_name": "team-default",
	"notes": "Lighthouse pilot"
}
```

Built-in profiles:

- `observe-only`: L0 record-only default.
- `local-dev`: L0 high-trust local development.
- `team-default`: L1 warn-on-missing-evidence team workflow.
- `regulated`: L3 approval-gate profile.
- `sovereign`: L4 strict sovereign-boundary profile.
- `offline-isolated`: L4 local-only isolated profile.

Readiness snapshots and evidence packs both include the active project policy profile.

## Policy Gates V0

Readiness snapshots and evidence packs include `policy_gates` derived from the active profile.

- `observe-only` and `local-dev` remain L0 observe mode with no active gates.
- `team-default` enables warning gates for saved evidence packs, CI signals, PR/review signals, benchmark signals, and Work Briefing activity.
- `regulated`, `sovereign`, and `offline-isolated` mark core evidence gates as `required`; v0 reports gate status but does not block actions.
- Failed CI/PR/benchmark signals set the relevant gate to `fail`.

Gate output shape:

```json
{
	"profile_name": "team-default",
	"enforcement_level": "L1",
	"overall_status": "warn",
	"mode": "warning_gates",
	"gates": [
		{
			"key": "ci_signal",
			"label": "CI signal",
			"status": "ok",
			"severity": "warning",
			"summary": "policy-ci: ok at 2026-06-18T02:00:00Z"
		}
	]
}
```

## Signals V0

AI-first signals are project-level PR, CI, and benchmark facts that feed readiness scoring.

Record a signal:

```text
POST /api/admin/ai-first/signals
```

Body:

```json
{
	"project_id": "CGA123",
	"signal_type": "ci",
	"name": "policy-ci",
	"status": "ok",
	"value": "62",
	"unit": "tests",
	"source_url": "https://github.com/nascousa/cga/actions/runs/..."
}
```

List signals:

```text
GET /api/admin/ai-first/signals?project_id=<project_id_or_name>&signal_type=ci&limit=25
```

Import GitHub Actions and PR signals:

```text
POST /api/admin/ai-first/signals/import-github
```

Body:

```json
{
	"project_id": "CGA123",
	"repo_url": "https://github.com/nascousa/cga",
	"limit": 5
}
```

If `repo_url` is omitted, CGA tries the project `upstream_url`, then the repository path's `.git/config` remote URL. Public GitHub repositories can be imported without a token; private repositories may use `GITHUB_TOKEN` or `GH_TOKEN` from the CGA process environment.

Import Azure DevOps builds and PR signals:

```text
POST /api/admin/ai-first/signals/import-azure-devops
```

Body:

```json
{
	"project_id": "CGA123",
	"organization": "contoso",
	"ado_project": "Project",
	"repository": "cga",
	"repo_url": "https://dev.azure.com/contoso/Project/_git/cga",
	"limit": 5
}
```

If `repo_url` is omitted, CGA tries the project `upstream_url`, then the repository path's `.git/config` remote URL. Private Azure DevOps organizations may use `AZURE_DEVOPS_PAT`, `ADO_PAT`, or `AZURE_DEVOPS_EXT_PAT` from the CGA process environment.

## PR Evidence Template

Saved evidence packs expose a PR-ready Markdown block:

```text
GET /api/admin/ai-first/evidence-packs/<evidence_id>/pr-template
```

The Admin AI-First History table also has a `PR` copy action for each saved evidence pack.

Supported `signal_type` values:

- `ci`: CI or local validation result. Feeds Verification Readiness.
- `pr`: PR/review/merge/review-latency result. Feeds Workflow Readiness.
- `benchmark`: token/HPS/quality/cost benchmark result. Feeds ROI & Outcome Readiness.

Status values are normalized into readiness states. `ok`, `pass`, `success`, `merged`, and `approved` count as ready; `fail`, `error`, `blocked`, and `rejected` count as failing; pending/open/review values count as warning.

## First Implementation Boundaries

- No database migration is required.
- No blocking enforcement is active yet; policy gates are reported as observe/warn/fail evidence.
- GitHub and Azure DevOps PR/CI imports are v0 signal ingestion paths; private repositories require process-level credentials.
- Project policy profiles are stored per project in the auth database.

## Suggested Next Steps

1. Add evidence-pack links into Work Briefing activity detail views.
2. Promote warning gates into review gates for lighthouse repos.
3. Add approval-gate workflow support for regulated and sovereign profiles.
4. Add scheduled signal imports for selected projects.