# CGA Runtime Operations

This guide keeps operational details out of the README while preserving the setup and maintenance notes needed by users and maintainers.

## Supported Local Runtimes

### Query Routing Policy

Open **Settings > Query Strategy** (`/admin/settings/query`) to choose
lightweight-first (default), automatic, or evidence-first routing, set a canonical
UTF-8 response-byte cap, and select compact/full evidence detail. Both reads and
writes use the existing admin-only runtime configuration API; the policy is
persisted under `query_policy` in `RUNTIME_CONFIG_PATH` (normally
`/app/data/runtime-config.json` in the persistent runtime-data volume).
Updates apply on subsequent `query_graph` calls, without restarting.

The same page exposes admin-only `GET /api/admin/query-routing/recent`.
This is a bounded, process-local list of the last 100 successful routed calls:
selected tool and reason, project, relation/intent, actual cache status,
handling duration, canonical result bytes/budget and truncation. It omits query
identities and source contents, resets on restart, and is not a durable audit log.
Refresh the page's decision table after issuing a query.

Only the recommended `query_graph` tool uses this policy. Explicit legacy tools,
`query_graph_relation` and the separate text/context `strategy_query` remain
compatible and are not globally intercepted. Audit intent always retains full
provenance; a budget too small for required metadata produces an explicit error.
See [the query quickstart](mcp-agent-query-quickstart.md#who-decides-when-to-use-ontology)
for the routing matrix and exact budget boundary.

#### Verified Local Query-Policy Cutover (2026-11-09)

- Live settings: `http://127.0.0.1:18001/admin/settings/query`.
- API overlay: `cga-local:1.30.128-query-policy-20261109`, image
  `sha256:651d3ede2b85cdf72bdfafd0d7f97630fadcdd697c809a175f2e61bbb2a69388`.
  This is a local feature variant, not a new public application release.
- Only policy-related backend/UI hunks were applied to the already-running image.
  Existing worktree session-expiry, learning and audit changes were excluded.
  Only the API container was recreated/restarted; database containers, volumes,
  network settings and credentials were preserved.
- The configured administrator's normal login successfully saved each policy
  through the real admin API. Authenticated project calls verified all three
  modes, required evidence, full audit provenance, an observed cache hit, exact
  canonical UTF-8 byte counts, request rejection, recent-decision visibility and
  explicit-tool compatibility. Unauthenticated admin reads/writes were denied.
- Final saved defaults are `lightweight_first`, `16384` bytes and `compact`
  evidence. The policy survived an API restart; unrelated runtime settings were
  unchanged. The settings deep link serves the new controls and requires login.
- Installed Relay discovery and an authenticated audit query passed through
  the new `query_graph` tool. Its hardened internal candidate SHA256 is
  `bf5d58a631ce4cfc0b1481098d0bfeed7e7ebd0b0b2914a54a9d4ae11ea66c66`.
  Authenticode remains **NotSigned**, because signing credentials are unavailable;
  this is not a signed public release. The single-instance gate was respected
  during stdio checks, then the tray was restored and `/status.json` returned 200.
- Before/after inventory matched exactly: 24 projects, 21 graphs, all node/edge
  counts unchanged. API and graph browser were healthy.
- Rollback image: `cga-local:rollback-query-policy-20261109`.
  Preserved materials under
  `%USERPROFILE%\.nasco\docker\main\cga\backups\pre-query-policy-20261109`
  include the original Compose file and Relay, plus
  `auth-20261109T192230Z-5012-0.sql.gz` and
  `falkordb-20261109T192237Z-5012-0.tgz`. Archive integrity passed `gzip -t`
  and `tar -tzf`; a full restore rehearsal was not performed. There was no
  pre-existing runtime-config file (recorded by `runtime-config.absent`).
  Preserve subsequent administrator edits when rolling back; do not blindly
  remove the now-persistent runtime configuration.

Validation:

```powershell
$env:FALKORDB_PORT = '26381'
.\.venv\Scripts\python.exe -m pytest src\tests\test_query_policy.py src\tests\test_query_policy_ui.py src\tests\test_runtime_config.py src\tests\test_graph_ontology.py src\tests\test_cga_relay_router.py src\tests\test_admin_settings_ui.py src\tests\test_session_expiry_ui.py --no-cov -o addopts= -q
# Passed: 328 tests, including all six relations in both directions on an isolated real graph.

& "$env:USERPROFILE\.cargo\bin\cargo.exe" test --manifest-path src\cga-relay\Cargo.toml -- --test-threads=1
# Passed: 16 unit + 59 CLI + 4 icon tests.
.\src\cga-relay\scripts\tests\release-process.Tests.ps1
# PASS. The subsequently extended policy schema unit assertion also passed.
```

Changed Python files passed Ruff with the already-existing server initialization
`E402` ignored. The new policy module passed mypy; a broader runtime-config check
reported the same 13 pre-existing typing errors reproduced from unchanged HEAD.
`git diff --check` passed. The scoped frontend candidate also passed executable
Node tests for loading, saving, failures, escaping and diagnostics; browser
rendering and save/reload handlers were exercised with isolated mock data.

### Verified Local ADC Centralization (2026-11-10)

- Runtime image: `cga-local:1.30.128-adc-central-20261110.2`, image
  `sha256:500a2b6ee0f54a33484b1d0992494cbe0ada00e34f9ff4dd558beb9602872799`.
  This is a local feature overlay, not a signed public release.
- Only ADC backend modules, the ADC scripts and their Admin-page wiring were
  overlaid on the previously running query-policy image. Unrelated mainline
  frontend changes were not applied. Only the API container was recreated;
  database/graph/queue containers, volumes and credentials were retained.
- The configured administrator's normal login was used for all project updates
  and collection requests. BrowserAgent and Codex-CLI received their missing
  registered checkout paths. The inventoried local DRI, SnapMarker and WorkAssist
  projects were registered through the existing project API.
- All **11 active projects** verified as `managed`: BrowserAgent, Codex-CLI,
  ContextGraphAgent, IcM_Automation, DNSRE-1, orcasql-agctools-echo-ops,
  QuickSearch, IWM, DRI, SnapMarker and WorkAssist. **268 rule documents** were
  collected into 11 immutable project revisions with 11 collection receipts.
  Existing projects retained their baselines. Every local source snapshot was
  re-read and compared with both its receipt digest and the central effective
  document contents. Original `.adc` files were not deleted or rewritten.
- The main-rules editor, project-version panel and Versions tab were exercised
  in the integrated browser using isolated fixture data and the real pinned
  ECharts asset. The SVG Sankey reported `nodeAlign: right`; all three fixture
  project endpoints had identical x coordinates (spread **0 pixels**). Theme
  color synchronization, narrow viewport resizing and subtab navigation passed.
  This browser exercise did not claim an authenticated live UI session.
- Live authenticated version inventory and collection preview passed; anonymous
  inventory/preview calls returned 401. All three deployed ADC script assets
  returned HTTP 200, and the API container was healthy.
- Before migration, the Compose configuration and a PostgreSQL dump were saved
  under `%USERPROFILE%\.nasco\docker\main\cga\backups\pre-adc-central-20261110`.
  The gzip dump round-trip matched the original dump; SHA256 is
  `95c6c477d66c440a70ac58bee7dbe830ca98d2606931c180843b2c10c4dcba88`.
  No full restore rehearsal was performed. Preserve later project revisions
  and administrator edits when planning rollback; do not blindly restore the
  pre-migration database or discard collection history.

Validation used an isolated PostgreSQL container/database, not the live auth
database:

```powershell
# Set ADC_TEST_DSN to the isolated database named adc_test.
.\.venv\Scripts\python.exe -m pytest src\tests\test_adc_collection.py src\tests\test_adc_governance.py src\tests\test_adc_remote.py src\tests\test_adc_ui.py src\tests\test_adc_skills.py src\tests\test_session_expiry_ui.py --no-cov -o addopts= -q
# Passed: 31 tests, 14 subtests, no skips.
$env:MYPYPATH = 'src'
.\.venv\Scripts\python.exe -m mypy -m backend.adc.collection --follow-imports=silent
# Passed.
```

### ADC Definition Update (2026-11-10)

ADC now means **Agent Development Constitution**. The terminology-only local
overlay is `cga-local:1.30.128-adc-definition-20261110`, image
`sha256:36e412964c8f0a5edacdfa098ee889815be4c38f0ae6bf05b3b6401ff00d88cc`.
It changes only the ADC page heading on top of the verified centralization
image; current source documentation, templates and the template generator use
the new expansion.

The normal administrator API published master **ADC 1.2.2**, changing only the
old expansion in 12 documents from 1.2.1. Its document SHA256 is
`46a6473c122a0506f0ed7de9b59df47aba31e51b21648f13bdd421efbede519f`.
The three prior releases' hashes and every existing project revision were
verified unchanged. Pinned project bundles, their local caches and the bundled
historical seed were not silently rewritten or upgraded.

The preceding Compose configuration is retained under
`%USERPROFILE%\.nasco\docker\main\cga\backups\pre-adc-definition-20261110`.
Rolling back the UI image does not remove the newly published master release.

### Docker Desktop Bundle

The Docker Desktop bundle is the recommended local distribution for non-developer Windows usage.

- Entry folder: `deploy/docker-desktop`
- Admin UI: `http://localhost:18001/admin`
- MCP SSE: `http://localhost:18001/mcp/sse`
- FalkorDB Browser: `http://localhost:13001`

One-click launchers:

- `start-cga-desktop.cmd`: starts containers and opens the Admin UI.
- `open-cga-desktop.cmd`: reopens the Admin UI using the last saved desktop port.
- `stop-cga-desktop.cmd`: stops the desktop stack.
- `logs-cga-desktop.cmd`: tails desktop stack logs for support and debugging.

### Repository-Root Desktop Stack

Use this when developing from the repository but wanting the desktop-style port layout and runtime isolation.

```powershell
Copy-Item .env.example .env
./src/scripts/start-desktop.ps1 start
```

Useful commands:

```powershell
./src/scripts/start-desktop.ps1 status
./src/scripts/start-desktop.ps1 logs
./src/scripts/start-desktop.ps1 stop
./src/scripts/start-desktop.ps1 open
```

Set `CGA_DESKTOP_API_PORT`, `CGA_DESKTOP_FALKORDB_PORT`, or `CGA_DESKTOP_BROWSER_PORT` in `.env` or in the shell when fixed custom desktop ports are needed.

### Local Port Binding

All published API, database, Redis and FalkorDB Browser ports default to IPv4
loopback (`127.0.0.1`), including release and generated portable Compose files.
Container-to-container traffic continues to use service DNS names and internal
ports; do not change those addresses to localhost.

- `CGA_API_BIND_ADDRESS` changes only the host-facing API binding.
- `CGA_DB_BIND_ADDRESS` changes database/Redis/Browser host bindings. Keep this
  on loopback even if you deliberately expose the API through a trusted proxy.
- A non-loopback value is an explicit exposure decision, not an authentication
  control. Configure authentication, TLS and firewall rules before changing it.
- Existing containers retain their previous bindings until an operator applies
  the configuration. Do not recreate a data container to change bindings until
  the migration checks below are complete.

### Dev Compose Profile

Use this for source development and container rebuilds.

```powershell
Copy-Item .env.example .env
docker compose --profile dev up --build
```

Default URLs:

- Admin UI: `http://localhost:8001/admin`
- MCP discovery: `http://localhost:8001/mcp`
- FalkorDB Browser: `http://localhost:13000`

## Default Runtime Shape

### Recovery Builds

The `recovery/complete-20261006` working branch combines the
`dev/graph-safety-recovery-20260914` baseline (including transparent CGA tray
icons and Relaunch) with the local ADC REST/MCP/Relay changes. A matching version
number alone does not establish that two builds contain the same changes.
The integrated release is 1.30.127, distinguishing it from earlier local
1.30.126 builds. Windows artifacts remain internal unsigned candidates until
an authorized signing certificate is supplied.
The integration has also been applied to the original workspace on
`dev/graph-safety-recovery-20260914`, preserving its existing launcher changes.
Run the Relay CLI and icon-resource tests before replacing an installed Relay,
and preserve database volumes when replacing the API container.

On this recovered Windows installation, the active deployment is
`%USERPROFILE%\.nasco\docker\main\cga\compose.json`, using the existing `cga-dev`
volumes. The workspace's start/stop/logs/open launchers select this deployment
through the shared `src/scripts/start-desktop.ps1` entry point. Start and restart
check FalkorDB persistence before Compose can recreate a container. The Admin
URL is discovered from the recovered deployment's published API port rather
than assuming port 18001. The local VS Code `Start CGA` task also targets that
deployment.
When that deployment configuration is absent, all workspace launcher actions
retain the standard repository deployment. Neither path falls back after a
startup failure; Docker's failure exit code is preserved.

The workspace launcher also checks `/health` from the Windows host, without
using an HTTP proxy, before reporting a detached start/restart as ready.
`status` performs the same host check once and fails if the published endpoint
is unavailable or is not a healthy CGA service. Container-internal `healthy`
does not prove that Docker Desktop is forwarding the host port correctly.
The explicit `restart` action now actually restarts the API service (`cga`);
an unchanged `compose up` alone does not restart an existing container.
For the standard repository deployment, the selected API, graph, and Browser
ports are saved after Compose succeeds, before the host health check. A failed
health check still fails the command, but subsequent `status` and `open` commands
in a fresh shell use the saved ports rather than defaults or stale mappings.

#### Docker Desktop Stale Port Forwarding

On 2026-11-04, Docker Desktop restored the API's published port to
`172.23.0.4:8000` after engine startup, but the live API had received
`172.23.0.5`. FalkorDB had received `.4`, while its host Browser/Redis forwards
still targeted `.5`. Both containers passed their internal health checks, but
the host endpoints returned empty replies. The Docker backend log recorded
the stale targets; restarting the API replaced its forwarding target with the
live address. This was a forwarding/address mismatch, not an API crash.

The recovered local deployment now pins each service to its existing address:
Redis `.2`, PostgreSQL `.3`, FalkorDB `.4`, API `.5`, and backup `.6`, on the
existing `172.23.0.0/16` network. Its Compose default network is explicitly
external (`cga-dev_default`), preserving the existing subnet and network rather
than replacing it. All existing services on that dedicated network are pinned
to avoid startup-order address swaps. Keep application connections on service
DNS names; do not put these local deployment addresses into shared application
configuration or use this subnet blindly on another machine.

Before applying network changes, preserve a copy of the active deployment
configuration, verify PostgreSQL/FalkorDB persistent mounts and current backups,
and compare project/graph inventories before and after. Recreate only this
Compose project's services without rebuilding/pulling images or removing
volumes; never run `down -v` or restart the shared Docker engine as a repair.
Verify both host `/health` and the FalkorDB Browser, as well as the configured
static addresses, after applying the configuration.

Static addresses prevent the observed startup-order mismatch, but do not
guarantee immunity to unrelated Docker Desktop defects. If it recurs, capture
the backend `vpnkitcontrol` forwarding targets and `docker network inspect`
before restarting anything. Do not mask failures with periodic unconditional
restarts or disable health/security checks.

The local backup sidecar also waits for API health during Compose startup.
Otherwise its initial `pg_dump` can hold relation locks while API initialization
waits to ensure tables exist. This ordering avoids that startup race; Docker
Engine's automatic container restart policy does not itself enforce Compose
dependency ordering.

`start-cga-relay.cmd` launches the verified standalone executable installed at
`%USERPROFILE%\.cga\bin\cga-relay.exe`, with the existing account state and
credential environment-variable configuration. It does not require a checkout
`.env` or an unrelated MCP token: inherited credentials take precedence, and
missing variables named by `API_KEY_ENV` or `ACCOUNT_TOKEN_ENV` can optionally
be loaded from the checkout `.env`. Relay remains responsible for validating
credentials or using its stored account session. The pre-integration Git stash
and `backups\complete-recovery-20261006` database/configuration snapshots are
retained for rollback.

`Dockerfile.dev` accepts a `PIP_INDEX_URL` build argument for environments that
require an alternative HTTPS package index. The default remains PyPI; do not
disable TLS verification to work around download failures.

#### Auto Ontology Local Cutover (2026-11-09)

The recovered local deployment now pins
`cga-local:1.30.128-ontology-20261109` in its existing Compose configuration.
This is a local feature build, not a new public version: `/health` still reports
1.30.128. The image ID is
`sha256:5350ea378a0d49dad6ee342cfe6fbc2e398d5f87493e61b79fa86ec2e1187ad7`.
It layers only the ontology module, graph schema comments, MCP tools and Relay
bridge onto the previously running image, preserving other deployed code rather
than publishing unrelated workspace changes.

The backend and installed Relay expose `get_graph_ontology` and
`query_graph_relation` with contract version 1.0.0. Host health, Admin HTTP,
FalkorDB Browser, installed Relay discovery, authenticated inbound/outbound
queries over existing call evidence and rejection of unsupported relations were
verified. The cutover preserved all 24 registered projects and 21 graph
inventories, including their node/edge counts. Preflight passed 107 targeted
Python tests, all 79 Relay tests, and the release replacement checks.

Rollback material is retained under the local deployment's
`backups\pre-ontology-20261109`: the previous Compose configuration and Relay,
project/graph inventory, and fresh PostgreSQL/FalkorDB archives. Archive integrity
was checked; this cutover did not perform a full restore rehearsal. The previous
API image is retained as `cga-local:rollback-ontology-20261109`. An application
rollback must recreate only `cga` against that retained image; do not restore
databases or remove volumes for this additive, migration-free change.

The installed Relay passed the hardened release binary checks and matches
SHA-256 `7c64a0cf3eb539b8f4b9d48498d4e5d40be4a12cac08b30980c2a76581d2bd64`.
It remains an **internal unsigned candidate**, not a formal signed release.
No signing certificate was available and no signing gate was bypassed.
The existing tray was restored after the exclusive stdio smoke test; the
machine-wide single-instance lock remains enforced. Restart/reconnect an
already-open MCP client to refresh its process and tool list.

For CGA local development, the default supported single-machine runtime is:

- Backend and Admin UI are served together by the single CGA API container.
- FastAPI serves `/admin` and the static frontend.
- FalkorDB stores graph data.
- PostgreSQL stores users, projects, tokens, audit logs, and work activity metadata.
- Redis supports runtime services.
- A backup sidecar snapshots runtime data when enabled by the active compose profile.

Legacy dev-profile helper commands remain available:

```powershell
./src/scripts/start-admin-s1.ps1 start
./src/scripts/start-admin-s1.ps1 status
./src/scripts/start-admin-s1.ps1 logs
./src/scripts/start-admin-s1.ps1 stop
```

## Work Briefing Aggregation

CGA includes a built-in work activity domain adapted from WorkAssist so cross-project progress can roll up into one admin surface.

- Admin UI: `http://localhost:18001/admin/briefing` in desktop mode.
- Admin summary API: `/api/admin/work-briefing`
- Admin activity list API: `/api/admin/work-briefing/activities`
- Project-scoped ingest API: `POST /api/project/work-briefing/activity`
- Project-scoped summary APIs: `GET /api/project/work-briefing`, `GET /api/project/work-briefing/activities`
- MCP tools: `workassist_record_activity`, `workassist_list_recent_activity`, `workassist_get_activity_briefing`
- Agent memory MCP tools: `workassist_get_memory_context` and `workassist_search_memory` provide project-scoped retrieval; `workassist_record_learning` writes bounded, unapproved learning candidates and `workassist_get_learning_context` reads them. Harnesses without MCP support can use authenticated `POST` / `GET /api/project/work-briefing/learning`. See [Agent Memory Layer](agent-memory-design.md) and [Harness Memory Integration](harness-memory-integration.md).
- Cold-start document import: `python src/scripts/memory_cold_start.py --project-id <id> --root .` prints a dry-run plan; add `--apply` to upsert summaries using `WORKBRIEFING_POSTGRES_DSN`.
- Skill validation: `python src/scripts/validate_skills.py .adc` checks every `SKILL.md` for frontmatter, SemVer version and the When to Use / Steps / Validation sections.

The Admin briefing dashboard includes copyable PowerShell, Python, and JSON request templates for project-scoped activity publishing. The Report tab can connect a Microsoft account with device-code login so generated WSR payloads can enrich stored PBI/PR references with Azure DevOps ticket details.

Recorded activity is stored in the local PostgreSQL auth database under the `work_activities` table, keeping project progress local-first alongside project and audit metadata.

## Admin Schedule Automation

CGA includes an admin-only Schedule surface for recurring automation jobs.

- Admin UI: `http://localhost:18001/admin/schedule` in desktop mode.
- Admin schedule API: `/api/admin/schedules`
- Supported task types: BrowserAgent command POSTs, BrowserAgent page-test workflows, agent activation HTTP calls, and generic HTTP POST jobs.
- BrowserAgent page tests can target a page URL, text assertions, console capture, metrics, screenshots, and optional DOM snapshots.
- Each task stores an 8-character task ID, cadence, runner URL, project binding, agent ID, JSON payload, last run status, next run time, and recent execution history.

A lightweight background worker runs due enabled tasks, carries the opened BrowserAgent tab ID through each page-test step, retries text assertions while the page settles, and records each result in `scheduled_task_runs`.

When scheduled tasks execute inside `cga-desktop-api`, `localhost:<port>` points to the container. Host-side BrowserAgent or workflow targets should use `host.docker.internal:<port>` and the target service must be listening on the host.

## Azure Policy Change Monitor Managed Proxy

The Azure Policy Change Monitor can run as a platform extension through a dedicated Azure Container Apps read proxy. This keeps Azure authentication in Azure and gives the local CGA runtime only an HTTPS endpoint plus a shared request key.

The proxy security boundary is fixed:

- One target subscription and four allowlisted read operations: policy definitions, policy set definitions, policy assignments, and Activity Log.
- Subscription-scoped Reader for the dedicated user-assigned managed identity.
- AcrPull only on the reused Azure Container Registry.
- Compliance queries and all Azure write operations are rejected.
- HTTPS-only external ingress, non-root container, immutable image digest, scale 0-1, bounded response size, and bounded Activity Log lookback.
- The shared key is stored only in the Container App secret store and the ignored local `.env` file. Extension configuration stores only `AZURE_POLICY_MONITOR_PROXY_KEY` as the environment variable name.

### Deployment Order

Run deployment commands from the repository root. Set the subscription explicitly before each stage and keep the shared key in a shell variable without printing it.

1. Deploy `deploy/azure-policy-proxy/identity.bicep`. This creates the dedicated identity, subscription Reader assignment, and ACR-scoped AcrPull assignment.
2. Query the identity's live role assignments and wait until both exact scopes are visible. Do not build or deploy the app before this propagation gate passes.
3. Build `extensions/azure-policy-monitor/Dockerfile.proxy` in the existing ACR and resolve the pushed manifest to an `@sha256:` image reference.
4. Generate a 256-bit random key. Store it as `AZURE_POLICY_MONITOR_PROXY_KEY` in the ignored local `.env` file and pass the same value to the secure `proxySharedKey` parameter.
5. Run a resource-group what-if for `deploy/azure-policy-proxy/app.bicep`. Accept only creation of the named Container App; reject updates or deletes to the existing registry, environment, identity, or other apps.
6. Deploy `app.bicep` with the immutable image reference and secure key parameter.

The current reference deployment uses:

| Setting | Value |
|---------|-------|
| Subscription | `40d9a853-9ece-49c7-84eb-3f9896cd2a27` |
| Resource group | `azurepg-icm-automation` |
| Container App | `cga-azure-policy-proxy` |
| Managed identity | `cga-azure-policy-proxy-mi` |
| Endpoint | `https://cga-azure-policy-proxy.kindtree-a8b25993.eastus.azurecontainerapps.io` |
| Image digest | `sha256:0ee00e3238f32df8152c61890f054aedfe72100639293c522a087dea406160f2` |

### Runtime Configuration And Verification

Configure the platform extension `azure_policy_change_monitor` with Azure monitoring enabled, repository scanning disabled, proxy authentication, compliance disabled, Activity Log enabled with a 120-minute lookback, 90 retained snapshots, and `read_only=true`.

After `.env` changes, recreate only the CGA API service that owns the schedule so the process receives the new variable. For the repository-root desktop stack:

```powershell
docker compose -f docker-compose.desktop.yml up -d --build --no-deps cga
```

Verify the deployment in this order:

1. `GET /healthz` returns HTTP 200 with `{"status":"ok"}`.
2. Each of the four authenticated read operations returns HTTP 200.
3. A request without `X-CGA-Proxy-Key` returns HTTP 401.
4. A compliance operation such as `query_policy_states` returns `OperationNotAllowed`.
5. Run the extension twice. The first run creates the baseline; the second should report no drift when Azure state is unchanged.
6. Create one enabled `extension_task` with `payload.extension_id=azure_policy_change_monitor`, `cadence_minutes=30`, and no inline configuration or secret override. Manually execute it once and confirm the linked extension run succeeds.

The active reference schedule is task `ZCOTRCUE`, schedule ID 4. Its scheduler state and recent run history are available from `/api/admin/schedules` and `/api/admin/extensions/azure_policy_change_monitor/runs`.

### Shared-Key Rotation

Rotate the key during a short maintenance window:

1. Generate a new 256-bit random key without printing it or writing it to a tracked file.
2. Redeploy `app.bicep` with the existing immutable image and the new secure `proxySharedKey` value. Wait for the new revision to become ready.
3. Replace only `AZURE_POLICY_MONITOR_PROXY_KEY` in the ignored local `.env` file.
4. Recreate only the CGA API service that owns the schedule.
5. Run all four authenticated operations, the HTTP 401 negative check, and one manual scheduled execution.
6. Clear the key variable from the shell. Never place the value in extension configuration, deployment plans, command output, tickets, or logs.

If the Azure revision succeeds but the local service cannot authenticate, stop scheduled execution until the local `.env` value and the Container App secret are aligned. Do not broaden RBAC or enable compliance as a recovery step.

## Runtime Persistence And Backup

- CGA runtime state lives in PostgreSQL for users, projects, tokens, audit logs, and work activity records.
- FalkorDB stores repository graph data.
- Runtime UI configuration is persisted in `data/runtime-config.json` by default, or in `CGA_RUNTIME_CONFIG_PATH` when set. All Compose distributions, including the generated portable bundle, mount `runtime_data` at `/app/data`.
- The Admin UI's System Settings / Indexing panel stores the default repos folder used when project indexing resolves a project without an explicit Repository Path.
- The dev, desktop, bundle and release sidecars create PostgreSQL logical dumps and FalkorDB RDB archives in `data/backups/<stack>/` every hour by default. The root `prod` profile has only the API's PostgreSQL backup scheduler, not a graph-archive sidecar.
- Override backup destination with `CGA_BACKUP_DIR` and the schedule with `CGA_BACKUP_INTERVAL_SECONDS` / `CGA_BACKUP_KEEP_COUNT`.
- Timestamped names have a uniqueness suffix, so two runs in one second do not overwrite each other. Retention counts immutable snapshots, not the `latest` pointer.
- Latest snapshots are published by atomic rename as `auth-latest.sql.gz` and `falkordb-latest.tgz`. A failed dump, compression, save or archive does not replace the previous good latest or trigger retention pruning for that dataset.

The Admin UI's System Settings / Backup panel reads and writes the same folder, so manual Back Up Now, restore, and delete actions are visible to both the UI and sidecar.

### PostgreSQL Restore Safety

API images and the sidecar use PostgreSQL 16 client tools for the supported
PostgreSQL 16 server. The API checks the dump's client/server major-version
headers before publishing it. A newer `pg_dump` is not automatically safe for
restoration to an older server: PostgreSQL 17 dumps can contain
`SET transaction_timeout`, which PostgreSQL 16 rejects. Source installations
must also install matching clients.

Older API-generated backups may have this version mismatch. Keep them, but do
not assume they are restorable because gzip validation succeeds. Verify them
in an isolated compatible runtime, or select a verified PostgreSQL 16 sidecar
snapshot. Restore errors roll back; the service does not silently remove SQL
statements from a selected backup to make it appear successful.

The API and sidecar serialize auth backup, restore, publication and deletion
using a kernel-managed advisory lock on the same persistent `.auth.lock` file
in the stack's auth backup folder. Linux uses `flock`; native Windows uses a
byte-range file lock. A crashed worker releases its lock without stale-directory
cleanup. Never delete an advisory lock file while workers might be running:
its inode is part of the lock identity. POSIX database child processes inherit
the descriptor, keeping the lock if their parent exits first.

Do not share one backup directory between native Windows services and Linux
containers: cross-kernel lock interoperability is filesystem-dependent.
The default stack-specific folders already keep these runtimes separate.
When forcibly stopping a native Windows restore, terminate its entire process
tree before starting another restore. If an older development build left a
`.auth.lock` **directory**, stop all old workers and verify no restore is active
before removing that legacy directory once; it is never stolen automatically.
Restore streams the selected gzip to a private, disk-backed staging file,
validates gzip EOF/CRC, and flushes/fsyncs it **before** capturing the pre-restore
safety dump. A retained read-only file descriptor supplies `psql` stdin without
loading the full SQL into Python memory. Safety snapshots cannot overwrite the
selected `latest` or prune the selected dated source; retention may temporarily
exceed its limit to preserve that source. A later normal backup can resume
ordinary retention.

Desktop launchers check the existing FalkorDB volume before a start/upgrade.
An old container with its graph outside the expected persistent directory is
left intact and the operation stops for migration. `stop` stops containers
without removing them; `restart` no longer removes the stack first. Release
builders refuse to overwrite an existing release folder or archive, including
any repositories, configuration or backups an installed bundle might contain.

Restore invokes `psql --no-psqlrc --single-transaction --set=ON_ERROR_STOP=1
--file=-`: SQL errors fail explicitly and roll back rather than continuing with
an incomplete restore. Snapshot read/decompression failures are reported
separately from a missing executable. Cancellation of an HTTP request does not
release the filesystem lock while its worker is still running.

The API also streams `pg_dump` stdout to a private file and gzip-compresses it
in 1 MiB chunks. Tool diagnostics are disk-backed and error responses are
limited to 64 KiB; `psql` stdout is discarded. All private staging files are
created under the configured backup directory rather than the default OS
temporary directory (which may be memory-backed), and are removed when their
handles close on success or failure. Use a disk-backed backup directory.
Provision free disk space for the selected decompressed dump, the decompressed
safety dump, and compressed output/latest staging in addition to retained
backups. A staging I/O failure aborts rather than falling back to a full-memory
restore.

The UI restores PostgreSQL **only**, not graphs, Redis state or runtime
configuration. PostgreSQL and graph snapshots are sequential, not a distributed
transaction. For a coordinated recovery, quiesce writers/indexing, choose and
verify the intended pair, restore into an isolated environment first, and then
plan a maintenance window. Restart the API after a successful auth restore so
in-memory components reload state.

### FalkorDB Snapshot Mechanism

The `postgres:16-alpine` sidecar uses its existing BusyBox `nc` support; it does
not install Redis clients or Python at startup. The script:

1. Checks `CONFIG GET dir` and `CONFIG GET dbfilename` against
   `/var/lib/falkordb/data` and `dump.rdb`. The same named volume is mounted
   read-only at `/falkordb-data` in the sidecar.
2. Pins the existing RDB's file descriptor, requests synchronous `SAVE` over
   RESP, and requires the complete success reply (including connection close).
   `SAVE` blocks database commands while serializing, so allow for its latency.
   A background save already in progress, a timeout, an ACL error or a failed
   save fails the attempt; it never falls back to an older file.
3. Requires a **different device/inode identity** for the completed mounted RDB.
   Pinning the old file prevents inode reuse. This catches the old wrong-volume
   mounting failure even if the contacted server acknowledges `SAVE`.
4. Pins and copies the completed RDB before archiving **only `dump.rdb`**.
   Concurrent atomic RDB replacements cannot change the opened file. Live
   AOF files, rewrite fragments and other directory contents are not archived.

Freshness is established by a completed save and file identity replacement,
**not by archive modification time**. Filesystems without usable file identities
fail closed. Docker named volumes provide these identities on the Linux VM.
`CGA_FALKORDB_SNAPSHOT_TIMEOUT_SECONDS` (default 300) bounds the network wait;
increase it for large graphs after assessing the blocking-save latency.

The shell explicitly checks `pg_dump`, `gzip`, archive creation and publication
as separate stages. No `pipefail` extension is required, and database stderr
remains visible. `BACKUP_RUN_ONCE=1` runs one cycle and returns nonzero if either
dataset fails, which is useful for **isolated** validation. The regular scheduler
logs a failed cycle and retries at its next interval.

### Durability Policy And Recovery Windows

All FalkorDB services explicitly set the persisted directory and filename,
`save 60 1`, and `stop-writes-on-bgsave-error yes`. The migration-safe default
requests an RDB save after 60 seconds with at least one change; it does not
silently enable or disable AOF on an existing installation.

- Without AOF, an unclean stop can lose writes since the last completed RDB:
  typically about **60 seconds plus snapshot/scheduling time**, not a guaranteed
  60-second upper bound. A disk failure or repeated save errors can make it
  longer. Monitor persistence errors and available volume space.
- Loss of the database volume requires a verified archive. At the default
  hourly schedule its recovery point can be roughly **one hour plus backup
  duration**, or older if cycles fail. A host-local archive does not protect
  against loss of that host; maintain a separately protected copy.
- FalkorDB's [durability documentation](https://docs.falkordb.com/operations/durability)
  describes AOF, including `appendfsync everysec`. Documentation alone is not
  proof that a mutable `latest` image's exact graph module/digest safely supports
  it. Validate that image's graph writes, AOF rewrite, crash/restart and recovery
  in isolation before adopting AOF.
- AOF can be requested with `CGA_FALKORDB_DURABILITY_ARGS=--appendonly yes
  --appendfsync everysec` **only after** compatibility and migration checks.
  For an existing RDB-only database, first enable AOF on the running server in
  a controlled maintenance procedure, wait for its initial rewrite to complete
  successfully, and preserve a verified RDB before persisting the startup flag.
  Simply restarting with AOF enabled can cause Redis to prefer an absent,
  empty or stale AOF over the intended RDB.
- An RDB archive is restored into a separate clean volume using a compatible
  FalkorDB image and RDB loading mode, not over a live database or alongside an
  unrelated AOF. Prove graph contents before switching the application over.

### Required Migration: Older Desktop/Portable Bundles

**Do not run the new bundle's first startup/recreation until these checks are
complete.** Older bundle and portable Compose files mounted `falkordb_data` at
`/data`, while the image actually writes to `/var/lib/falkordb/data`. Their
named volume may be empty or stale even though the old container still serves
graphs from its writable layer or an anonymous volume. Merely changing the
mount destination can start an apparently empty database.

1. Record the existing Compose project name, actual container IDs, image digest,
   mounted volume names and live `CONFIG GET dir`, `dbfilename`, `appendonly`
   and `save` values. Do not delete, recreate or remove the old container yet.
2. Quiesce application writes. On the **old** graph server, request a completed
   save and secure its actual persisted files outside that container. If AOF is
   active, retain its complete directory/manifest too; do not treat an old
   sidecar tar or `latest` timestamp as evidence of a fresh graph snapshot.
3. Test that snapshot in a separate clean volume/isolated container with the
   same compatible image. Verify expected graph names and node/edge counts.
   Keep the original container and every original volume as rollback sources.
4. During maintenance, stop only the affected writer before seeding the
   **existing named graph volume** with the verified data and correct ownership.
   The corrected mount target is `/var/lib/falkordb/data`. Do not change the
   logical volume key, actual volume name or Compose project name; never use a
   volume-deleting teardown as an upgrade step.
5. Older portable APIs also lacked the `runtime_data:/app/data` mount. Secure
   their `/app/data` (especially `runtime-config.json`) before recreation, then
   seed the new runtime volume and verify configuration survives an isolated
   restart. PostgreSQL and Redis volume names are unchanged.
6. Only after validation apply the new Compose configuration and verify graph
   contents, new loopback bindings and a fresh sidecar snapshot. If any check
   fails, stop and investigate; do not reindex over or delete rollback data.

Repository-root desktop/dev graph mounts were already correct; do **not**
rename their volumes or migrate/recreate them merely because the bundle mount
was corrected. Keep `.env`, repos, backups and runtime configuration when
upgrading. Generate release artifacts in a **new output directory**, not over
an installed runtime. The portable builder now refuses an existing output
folder, and packages this guide as `RUNTIME-OPERATIONS.md`.

### Interrupted Backup Workers

`CGA_BACKUP_LOCK_TIMEOUT_SECONDS` defaults to 300 for both the API and sidecar.
Locks are never stolen based on age: a large dump or restore may legitimately
outlive a timeout. A hard-killed worker can leave `.auth.lock` or `.falkordb.lock`
and a hidden `.in-progress-*` staging directory behind. Stop/verify **all** writers
for that backup folder, confirm no restore is running, and only then remove
that specific stale lock/staging directory. Never remove locks from a live
worker, and do not remove snapshots or named volumes as lock recovery.

## Desktop Bundle Packaging

Recommended non-technical distribution files live under `deploy/docker-desktop`.

Build a zip-ready self-contained package:

```powershell
Set-Location .\deploy\docker-desktop
./build-portable-bundle.ps1
```

Build a versioned release folder and zip archive:

```powershell
Set-Location .\deploy\docker-desktop
./build-release-bundle.ps1
```

The release builder produces `cga-desktop-api-image.tar` inside the release folder. The launcher loads that image automatically, so first startup does not need to build the CGA API image from source. Developers can still force the fallback build path with:

```powershell
./start-desktop.ps1 start -BuildFromSource
```

The Docker Desktop package intentionally uses `18001`, `16381`, and `13001` so it does not collide with the dev profile defaults. The launcher also saves the last active desktop ports under `tmp/cga-desktop-runtime.json` so reopening from a fresh shell still targets the correct local URL.

The release zip intentionally does not include local projects, private repositories, PostgreSQL data, FalkorDB graph indexes, Redis state, backups, or sample/demo project data. First run creates a fresh runtime, creates the configured admin account, and waits for you to add and index repositories.

Default local credentials come from the active launcher's `.env.example`. Change `JWT_SECRET_KEY`, `ADMIN_USERNAME`, and `ADMIN_PASSWORD` before exposing the service beyond localhost.
