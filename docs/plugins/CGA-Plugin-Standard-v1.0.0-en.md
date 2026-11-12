# CGA Plugin Standard v1.0.0

Status: release candidate; effective only after the joint SDK release is approved.
Contract: `cga.first-party.v1`. SDK: `cga-plugin-sdk` 1.0.0.

## 1. Scope and compatibility

V1 supports trusted, reviewed first-party code integrated into CGA source and
selectively packaged with its image. It does not introduce third-party runtime
loading, ZIP upload/install, a marketplace, billing or process isolation.
Existing first-party plugins remain available under their current contracts;
no historic approval is invented and no production selection is changed.
New integrations and material changes follow this standard.
Host-framework changes remain pending CGA source work; this standalone SDK kit
does not add them to the older public API release. Execution leases are currently
process-local, not distributed multi-worker coordination.

Core authentication, project authorization, Audit, ADC, graph/MCP, memory,
scheduler and backups cannot be replaced or declared optional. A plugin cannot
claim a core ID. Administrative install/enable is not publication approval.

Semantic versioning applies separately to SDK, manifest contract and plugin.
Breaking contract changes require a new major SDK/API and migration plan.
`min_cga_version` states the integrator's prerequisite, not a proven compatibility
matrix. Test each supported host version and retain migration/rollback evidence.

## 2. Required manifest

Each package has `plugin.json`, with unknown fields rejected:

| Field | Requirement |
|---|---|
| `schema_version` | Integer 1 |
| `id`, `name`, `version` | Stable lowercase snake_case ID, readable name, MAJOR.MINOR.PATCH |
| `sdk_api` | `cga.first-party.v1` |
| `execution_model` | `first-party-in-process` |
| `min_cga_version` | Explicit three-part host prerequisite |
| `entrypoint` | One root module's `module:execute`; source must be a resource |
| `license` | Apache-2.0, MIT or BSD-3-Clause; preserve exact upstream notices |
| `permissions` | Explicit least-privilege list, no implicit grants |
| `network_hosts` | Explicit lowercase DNS names, no URLs, credentials or wildcards |
| `secret_env` | Environment variable names only; never values |
| `timeout_seconds` | 1–180 seconds, enforced by the host/provider integration |
| `retention_days` | 1–365; document data classes, cleanup owner and backup exceptions |
| `resources` | Explicit relative files, unique on case-insensitive systems |

Permissions are `project.read`, `activity.write`, `network.outbound`,
`secrets.read`, `filesystem.read`, `filesystem.write`, `scheduler.execute`.
Outbound and secret permissions require matching host/environment declarations.
These are **review declarations**, not a sandbox: in-process Python has the
host's capabilities. Review actual code and enforce controls at every I/O boundary.
Generated JSON Schema describes structure; SDK semantic and filesystem checks
are additionally mandatory.

## 3. Execution and data controls

- Adapters use `execute(task, started_at, started_clock)` and return
  `(state, response_status, payload, error)` through the SDK result contract.
  Exceptions remain failures. Never label provider/network errors as success.
- The host owns authentication, project scope, enable checks, execution leases,
  scheduler concurrency, Audit and persistence. Use the existing host helpers;
  declarations never bypass authorization.
- Keep optional imports lazy. Omitted resources must not be required by core
  imports. Disabled execution returns the existing 409 behavior; omitted routes
  follow existing 404 behavior. Active execution blocks disable/uninstall.
- Validate input, bound evidence/output and retries, propagate cancellation,
  and enforce request deadlines. SDK JSON results are limited to 64 KiB.
- Never return credentials, raw configuration or unredacted provider responses.
  Secrets remain outside source/package/logs. External data flows and service
  endpoints require explicit review; no arbitrary URL execution.
- Retention must be implemented and tested where data is stored. A manifest
  retention number alone does not delete data. Uninstall preserves configuration
  and history unless separately reviewed, explicitly confirmed cleanup is requested.
- Reuse existing project ports. CGA uses 18001; do not auto-increment ports or
  stop unrelated/shared listeners.

## 4. UX and verification

Use the existing plugin list/detail/settings patterns, accessible labels and
keyboard navigation. Retain drafts across tab changes. Clean Save is disabled;
changed Save is enabled. Support dark/light/system charts where reports exist.
Failures must be actionable and distinct from empty successful results.

Required evidence: input/error/authorization tests, enable/disable/lease tests,
core-only packaging/import tests, no-secret output tests, bounded timeout/output,
retention, migration, rollback and supported-platform checks. Test the actual
required outcome, not only a stub or an enabled download button.
Document unsupported cases and skipped tests.

## 5. Packaging and publication

SDK packaging inspects declared files without importing code; paths, symlinks,
key material and size limits are checked. Include all license notices and
reproducible SHA-256 hashes. Checksums prove integrity, not trust or approval.
Existing released bytes/version names must never be overwritten.

Follow [approval policy](CGA-Plugin-Approval-v1.0.0-en.md).
Publish standard, approval policy, SDK wheel/source, schema, examples and guide
as one versioned kit. Formal workflow publication includes GitHub provenance.
An unsigned local candidate or draft release is not an approved runtime update.
The CGA API/Relay signing and deployment gates remain unchanged.
