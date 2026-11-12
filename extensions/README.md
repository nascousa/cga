# CGA First-Party Plugins

CGA separates optional application capabilities from its core through a
first-party catalog. Plugins run in the same process and use the existing
database and runtime configuration. This is not a third-party code loader.

## Plugin development, approval and SDK

The v1 development kit includes a versioned plugin contract, independent
maintainer/security approval policy, Python SDK, scaffold, static validator,
bounded deterministic packaging, schema and joint-release build tooling.
Start with the [SDK guide](../sdk/cga-plugin-sdk/README.md),
[plugin standard](../docs/plugins/CGA-Plugin-Standard-v1.0.0-en.md) and
[approval policy](../docs/plugins/CGA-Plugin-Approval-v1.0.0-en.md)
([Chinese standard](../docs/plugins/CGA-Plugin-Standard-v1.0.0-zh.md),
[Chinese approval policy](../docs/plugins/CGA-Plugin-Approval-v1.0.0-zh.md)).
This is a release candidate until independent approval and provenance
publication complete. It does not add dynamic installation or retroactively
approve bundled plugins. Actual repository reviewer/protection setup is required;
missing configuration fails the publication gate.

## Core Boundary

ADC governance, system Audit, authentication, projects, graph queries, MCP,
agent memory, the scheduler and backups remain core. Neither `adc` nor `audit`
is a valid plugin ID. Removing Automation management does not remove the
scheduling engine or interrupt jobs owned by other enabled plugins.

## Catalog

| ID | Optional capability | Notes |
|---|---|---|
| `automation` | Task management UI/API and run history | Install adapters separately for the task types you use. |
| `browseragent` | BrowserAgent page tests and artifacts | Uses an existing BrowserAgent endpoint. |
| `workflows` | External agent activation and HTTP tasks | Uses the existing task payload and endpoint contracts. |
| `azure_policy_change_monitor` | Repository/cloud policy monitoring | [Monitor guide](azure-policy-monitor/README.md). |
| `briefing` | Work Dashboard and briefing summary APIs/tools | Activity ingestion/storage and agent memory remain core. |
| `reports` | Weekly report UI, templates and saved reports | Independent of `briefing`; existing localStorage keys are retained. |
| `notifications` | Severity-gated webhook/SMTP delivery | Currently used by the policy monitor; does not determine monitoring outcomes. |
| `evaluation` | Benchmarks, evaluation tools and trace worker | Starting its background worker after initial activation requires an API restart. |

Reports retain their existing placement under Audit, but only their optional
Report entry is removed when disabled. Request logs and system Audit remain
available. Existing styles, including dark-mode styling, are retained.

## Choose Plugins

Administrators use **User menu → Administration → Plugin management**:

1. Select **Installed** for a bundled capability.
2. Select **Enabled** to activate it. Enabling also checks Installed;
   uninstalling clears Enabled.
3. Choose **Save changes**. Admin reloads to apply navigation and assets.

The page uses a **left plugin catalog → right settings panel** layout. Click an
installed plugin card or its **Settings** button to open the panel without
replacing the catalog. The selected card is highlighted. Cards show categories,
activation status, installation/enable controls, **Settings**, and workspace
entrances where applicable. **Save changes** applies the complete installation
selection and reloads Admin; it is separate from saving plugin settings.
Each installed plugin has a detail URL, for example
`/admin/extensions?plugin=browseragent`. Disabled installed plugins remain
configurable; their execution entrances are disabled. Leaving a changed detail
form requests confirmation, including workspace entrances and applying an
installation selection that reloads Admin. **Close settings** returns to the
empty panel without resetting catalog filters or installation drafts. On narrow
screens, opening settings temporarily replaces the list; closing returns to it.
Actions use SVG icons and text.
The settings status has a 12px gap before **Save settings**. With no unsaved
changes, the button is gray and disabled; editing enables the green action,
and reverting all edits disables it again.
Enabled status badges are green in both the overview and detail; disabled
status badges are gray.
Installation/enable controls and action buttons share one compact card footer;
they wrap only when the available width cannot fit both groups.

Settings tabs group related controls: BrowserAgent has **Connection / Capture**,
Workflows **Connections / Payload**, Notifications **Transport / Sender**, and
Reports **General / Template**. The policy monitor reuses its existing controls
under **Sources / Azure Collection / Cloud And Identity / Evidence Summary /
Notifications**, with findings, delivery and history in **Activity**. Each
plugin also has **Diagnostics** for its saved-configuration/runtime check.
Switching tabs keeps the same form controls and unsaved values; saving submits
all sections, including hidden ones. Invalid required fields reveal their tab
before browser validation focuses them. Tabs support Left/Right, Home and End
keyboard navigation.

Cards display a separate **Free** or **Paid** badge beside the activation badge,
from the catalog's `pricing`
metadata (`free` or `paid`), independently of installation/activation status.
All current first-party plugins default to `free`. Older catalog responses
without this field retain that free-plugin behavior; unrecognized values display
**Pricing unavailable**, never an assumed price. These badges do not implement
checkout, subscriptions, license checks or execution restrictions.

The overview toolbar searches names, IDs, descriptions and categories, combines
category/status filters, and sorts by name (A-Z or Z-A), category or status.
Status order is enabled, installed/disabled, not installed, then not bundled;
names break ties. Counts and an empty-results message reflect the current
filters. Status filters include unsaved selections. Filtering, sorting, opening
details and resetting filters retain installation/enable drafts, including
hidden cards; **Save changes** always submits the complete selection.

| Detail | Configuration and effect |
|---|---|
| Automation | Core scheduler status; entrance to task configuration and run history. Cadence remains task-owned. |
| BrowserAgent | Endpoint, timeout and artifact defaults for **new Admin-created tasks**. |
| Workflows | Agent/HTTP endpoints, timeout and JSON payload defaults for **new Admin-created tasks**. |
| Policy monitor | Existing configuration, snapshots, findings and run history, embedded in its detail rather than a separate top-level tab. |
| Work briefing | Runtime default project, initial batch (1–25) and Dashboard refresh interval; zero disables polling. Applied when opening Dashboard. |
| Reports | Browser-local report template, automatic generation interval and optional HTTPS Loop URL. Existing template/history keys and default Loop link are preserved. |
| Notifications | Existing shared SMTP settings, never a second transport configuration. Password stays in `CGA_SMTP_PASSWORD`; webhook environment references and notification rules stay with the monitor. |
| Evaluation | Runtime trace recording/evaluation preference, worker/operator-gate status and MCP benchmark tool entrances. The preference cannot override `TRACE_ENABLED=false`. |

**Check configuration** reads saved settings and runtime state. It does not
contact providers, send mail/webhooks, execute a scan or run benchmarks.
It must not be interpreted as a successful external connectivity test.

BrowserAgent/workflow URLs are reached from the CGA server/container, not from
the browser. Task API clients continue supplying their own task values; existing
tasks are never rewritten by changing defaults.

`GET /api/admin/plugins/{id}/settings` requires an installed plugin and admin
authentication. `PUT` accepts the fields above for BrowserAgent, Workflows,
briefing, notifications and evaluation. BrowserAgent/Workflow endpoints reject
embedded credentials, query strings and fragments; workflow payload templates
reject inline secret fields and are limited to 16 KiB. Invalid input does not
replace saved configuration or echo submitted secret values. Reports use
browser storage, while Automation and the policy monitor retain their existing
task/configuration editors.

There are three separate states:

- **Available:** its implementation is present in this distribution.
- **Installed:** the administrator selected it for this runtime.
- **Enabled:** its APIs/tools/adapters may execute.

An unbundled plugin cannot be installed from the UI; rebuild/use a distribution
containing it. Runtime uninstall is non-destructive and does not physically
remove files from a shared image.

The admin-only endpoints are `GET /api/admin/plugins` and
`PUT /api/admin/plugins`. PUT accepts exactly:

```json
{
  "installed": ["automation", "browseragent", "reports"],
  "enabled": ["automation", "browseragent", "reports"]
}
```

Enabled IDs must be installed, and installed IDs must be available. Invalid
selections do not replace the saved selection. Disabling/uninstalling an
actively executing plugin returns HTTP 409. Execution leases are process-local;
multi-worker coordination is not implemented.

Disabled execution endpoints return HTTP 409. Omitted Automation routes are
not mounted and return HTTP 404. Optional MCP tools disappear from discovery
and reject direct calls; graph, memory and ADC tools remain present.
The reports script remains readable while installed but disabled so that its
existing template defaults can be edited. Report execution APIs stay gated.

## Automation Workspace

**Open workspace** for Automation opens the **Tasks** view directly. Search
and task-type filters sit above a five-column task list: task, schedule,
next run, last execution and actions. Project/type are grouped with the name;
**Task details** reveals task ID, runner and page URL without adding more
columns. Expanded details survive the existing ten-second refresh.
**Execution history** is a separate tab; switching views preserves filters and
an open editor's values. Tabs support Left/Right, Home and End keys.

**New Task** and Edit open the editor above the list and focus the task name.
Agent ID, timeout and JSON payload remain available under **Advanced**;
collapsing this section does not omit values when saving. Scheduler behavior,
manual execution and existing task settings are unchanged.

Project policy and PR/CI/benchmark evidence inputs are under
**Audit → Report → Evidence packs, policy and signal tools → Project policy
and evidence inputs**, collapsed until needed, using the existing project
picker and APIs.

## Reports Workspace

The source redesign merges activity reports and current AI-First readiness into
**Audit → Report**, with project/UTC-period/type/status/text filters, interactive
ECharts charts and evidence drill-down. Snapshot exports include Mermaid-led
PDF, CSV, JSON and Mermaid source. Advanced evidence tools and legacy weekly
drafts/templates/history are retained in separate collapsed panels.

Reports remains optional and does not require the briefing plugin. Both Docker
builds prepare its pinned local chart/export assets; source runs must execute
`npm ci` and `npm run build` in `src/report-ui`. Selective distributions include
the generated `src/frontend/report-vendor` chunks and their license notices
when Reports is included. See [report operations](../docs/runtime-operations.md#unified-report-workspace)
for caps, current-versus-historical semantics and PDF limitations. Source support
does not imply the running service has been upgraded.

## Initial Selection and Persistence

With no saved selection, all available plugins are installed/enabled. This
preserves existing installations.

- `CGA_PLUGINS` sets a comma-separated initial selection. An empty value selects
  core only; omit the variable to select all available plugins.
- `CGA_AVAILABLE_PLUGINS` can further restrict availability but cannot restore
  resources omitted from a distribution.
- Saved selection takes precedence over initial-selection environment values.
- Selection is saved in `plugins` alongside existing runtime settings, using
  the existing persistent runtime-config location and volume.

Before switching an existing installation to a lean image, save a selection
compatible with the new image. References to absent plugins are explicit
configuration errors, not an automatic reset of administrator choices.

## Selective Docker Builds

Both Dockerfiles accept the build argument `CGA_PLUGINS`. The build removes
catalog-declared optional implementations/assets and writes `plugin-bundle.json`.
Do not run the packaging script against a working checkout: it trims its target.

```powershell
# Core-only distribution.
docker build -f Dockerfile.prod --build-arg "CGA_PLUGINS=" -t cga:core .

# Browser task management and independent reports.
docker build -f Dockerfile.prod --build-arg "CGA_PLUGINS=automation,browseragent,reports" -t cga:browser-reports .

# Compatibility/default distribution.
docker build -f Dockerfile.dev --build-arg "CGA_PLUGINS=all" -t cga:all .
```

Shared Python dependencies, core compatibility stubs and HTML containers remain.
These builds are not independently published plugin wheels or hot-loaded code
packages. Building an image does not deploy or publish it.

## Scheduling Without Automation Management

The core scheduler can execute installed/enabled adapters without the
`automation` management plugin. Admin-only adapter endpoints are:

- `GET /api/admin/plugins/{plugin_id}/tasks?limit=100`
- `POST /api/admin/plugins/{plugin_id}/tasks`

Supported owners are `browseragent`, `workflows` and
`azure_policy_change_monitor`. POST uses the existing
[scheduled-task model](../src/backend/schedules/models.py); the task type must
belong to the selected owner. Use Automation for graphical task management.

Uninstalling an adapter retains its tasks and run history. Subsequent attempts
record a failed run with status code 409 and do not call the external endpoint.
Reinstalling restores execution without recreating the task. The scheduler
runtime module must still be enabled for scheduled execution.

## Notifications and Reports

Policy monitoring remains usable without `notifications`. If notification
delivery is configured but its plugin is disabled, delivery records an explicit
error independently of the monitor outcome. Secrets remain in their existing
environment-based configuration, not the plugin-selection payload.

Reports use `/api/admin/reports/source` and need not enable Dashboard/briefing.
Browser-local templates and saved report snapshots are preserved on uninstall,
disable, reload and reinstall. Assets load only when the corresponding UI is
opened. Enabling evaluation after an initially disabled startup requires an API
restart for trace recording/evaluation; direct benchmarks activate immediately.

## Validation

Use the existing test environment and isolated PostgreSQL fixture databases;
never point destructive ADC fixtures at an operational database.

```powershell
.\.venv\Scripts\python.exe -m pytest src\tests\test_plugins.py src\tests\test_plugins_ui.py src\tests\test_schedule_service.py src\tests\test_extensions_service.py src\tests\test_work_briefing_api.py src\tests\test_project_work_briefing_api.py src\tests\test_eval.py --no-cov
```

The plugin suite tests each catalog entry's lifecycle, persistence, invalid and
busy transitions, real administrator authorization, adapter isolation, report
independence, MCP discovery and actual application imports from trimmed
core-only and selected-plugin distributions. UI tests execute the existing
report generator and lazy-loading behavior using Node.

Local verification: 179 tests and 14 subtests passed across the plugin, task,
monitor/notification, briefing/report, evaluation, authentication, agent-memory
and ADC regression suites. All eight single-plugin distributions and core-only
distribution imports/health checks passed. The seven plugin implementation
modules passed mypy; changed Python passed Ruff, and plugin/inline JavaScript
syntax checks passed. Browser checks used an isolated fixture: report generation,
uninstall/reinstall with preserved history, briefing lazy loading and adapter-aware
task defaults passed. External cloud and BrowserAgent endpoints were not exercised
live. The local plugin deployment and authenticated API checks are recorded in
[runtime operations](../docs/runtime-operations.md#verified-local-plugin-cutover-2026-11-10);
this does not claim live acceptance of every external provider.
