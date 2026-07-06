# CGA Extension Integration Plan for Azure Policy Change Monitor

Date: 2026-07-01

## Decision

Yes. The Azure Policy Change Monitor should be built as a CGA project-level extension and executed through the existing CGA Schedule feature.

The best implementation path is staged:

1. **Phase 1: External HTTP extension**
   - Build the policy monitor as a small service with a `POST /run` endpoint.
   - Configure CGA Schedule with task type `http_request` and the extension endpoint as `target_url`.
   - Store scan reports as CGA evidence/signals or extension output artifacts.

2. **Phase 2: First-class CGA extension page**
   - Add an `Extension` top-level admin page after `Project` and before `Schedule`.
   - Show project-scoped extension status, configuration, last run, and recent findings.
   - Provide a `Run now` action that either calls the extension endpoint directly or creates/runs a CGA schedule task.

3. **Phase 3: Native scheduled task type**
   - Add a first-class schedule `task_type`, for example `policy_monitor`.
   - Build payload defaults and output rendering specifically for policy monitor scans.
   - Keep compatibility with generic `http_request` tasks.

## Current CGA Fit

CGA already has the right primitives:

- Admin topbar tabs include `Project` and `Schedule`.
- Schedule tasks are project-scoped via `project_id`.
- Schedule supports `http_request`, `agent_activation`, and `browseragent_task`.
- Schedule execution posts JSON to a configured `target_url` and records run history.
- The frontend is currently a single admin shell, so adding an `Extension` pane is straightforward.

## Recommended UX

Add `Extension` immediately after `Project` in the admin topbar:

`Dashboard | Report | AI-First | Project | Extension | Schedule | User | Audit | Graph | Settings`

The `Extension` page should be project-scoped and include:

- Project picker or reuse current selected project.
- Extension catalog:
  - `Azure Policy Change Monitor`
- Configuration panel:
  - Repo policy root
  - Target cloud folders: `AllEnvironments`, `USNat`, `USSec`, optional `Bleu`
  - Azure scope: subscription or management group
  - Schedule cadence
  - Notification targets
  - Read-only mode toggle, default on
- Status panel:
  - Last run
  - Last status
  - Critical/high/medium/low findings
  - Drift summary
  - Sovereign parity summary
- Actions:
  - Run now
  - Create schedule
  - View latest report
  - Open schedule task

## Extension Repository Layout

Each CGA extension should own its source and documentation folders. The Azure Policy Monitor extension should live under:

```text
extensions/azure-policy-monitor/
  README.md
  src/
    policy_monitor/
      __init__.py
      config.py
      scanner.py
      runner.py
      reports.py
      cga_adapter.py
  docs/
    Azure_Policies_Beginner_Guide_extracted.md
    Azure_Policy_Change_Monitor_Agent_Recommendation.md
    CGA_Extension_Integration_Plan.md
```

The extension core should stay independent from CGA internals. CGA-specific integration belongs in a thin adapter layer.

## Recommended CGA Host Shape

Add a CGA host extension registry rather than mixing policy-monitor logic directly into Schedule:

```text
src/backend/extensions/
  __init__.py
  models.py
  router.py
  registry.py
```

The CGA host layer should discover extensions, store project-level extension config, expose admin APIs, and call extension adapters. It should not own Azure Policy scanning rules.

Suggested API surface:

- `GET /api/admin/extensions`
- `GET /api/admin/extensions/{extension_id}`
- `GET /api/admin/extensions/{extension_id}/projects/{project_id}/config`
- `PUT /api/admin/extensions/{extension_id}/projects/{project_id}/config`
- `POST /api/admin/extensions/{extension_id}/projects/{project_id}/run`
- `GET /api/admin/extensions/{extension_id}/projects/{project_id}/runs`
- `POST /api/admin/extensions/{extension_id}/projects/{project_id}/schedule`

## Schedule Integration

### Phase 1 Schedule Payload

Use existing `http_request` task type:

```json
{
  "extension_id": "azure_policy_change_monitor",
  "mode": "scheduled_scan",
  "project_external_id": "<CGA project id>",
  "repo_policy_root": "settings/BuiltInPoliciesV2",
  "cloud_folders": ["AllEnvironments", "USNat", "USSec"],
  "checks": [
    "folder_parity",
    "guid_consistency",
    "version_consistency",
    "effect_risk",
    "metadata_required",
    "azure_assignment_drift",
    "compliance_drift"
  ],
  "read_only": true
}
```

The schedule `target_url` can point to the extension service endpoint:

```text
http://localhost:<policy-monitor-port>/run
```

### Phase 3 Native Task Type

After Phase 1 is stable, add `policy_monitor` to the schedule task type union and teach schedule execution how to build the monitor payload. This keeps the UI cleaner and avoids requiring admins to hand-edit JSON.

## Data Model Recommendation

Start small. Reuse schedule run history and persist only extension config plus reports.

Possible tables:

```text
extension_configs
  id
  extension_id
  project_id
  enabled
  config_json
  created_at
  updated_at

extension_runs
  id
  extension_id
  project_id
  schedule_id
  status
  severity
  started_at
  finished_at
  summary_json
  report_path
  created_at
```

For MVP, report files can be stored under a controlled CGA runtime artifacts folder and referenced from `extension_runs.report_path`.

## Why This Is Better Than a Separate Scheduler

- CGA already knows projects and repository paths.
- CGA already has schedule execution, run history, and admin auth.
- Project-level extension state can be displayed beside project metadata.
- One scheduler avoids duplicate timers and inconsistent evidence trails.
- CGA can aggregate policy scan findings into project activity/evidence later.

## Implementation Recommendation

Use this order:

1. Build the policy monitor scanner under `extensions/azure-policy-monitor/src/`.
2. Keep extension docs under `extensions/azure-policy-monitor/docs/`.
3. Wrap the scanner in a small FastAPI extension service with `POST /run`, or expose it through a CGA adapter.
4. Create a CGA Schedule `http_request` task to call it during the first validation phase.
5. Add the CGA `Extension` page for configuration and last-run visibility.
6. Persist extension configs and runs in CGA.
7. Add native schedule task type `policy_monitor` only after the extension contract is stable.

## Security Defaults

- Read-only Azure access by default.
- No secrets in schedule payloads.
- Use Managed Identity or workload identity federation for Azure access.
- Redact tokens, secrets, Authorization headers, and API keys from stored responses.
- Require admin permission to configure extension schedules.
- Require explicit human approval for remediation actions.

## Open Design Questions

- Should extension services run inside the CGA backend process or as sidecar services?
- Should extension reports be stored in CGA PostgreSQL, filesystem artifacts, or Log Analytics?
- Should extension results be recorded as AI-first signals/evidence packs or as a new extension-specific activity type?
- Should `Extension` be admin-only initially, or visible read-only to developers for their own projects?

## Recommended Answer

Build it as a CGA extension, run it through CGA Schedule, and start with the existing `http_request` task type. Add the `Extension` page after `Project` for configuration and visibility. Only add a native `policy_monitor` task type after the scanner and HTTP execution contract are stable.
