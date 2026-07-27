# CGA Runtime Operations

This guide keeps operational details out of the README while preserving the setup and maintenance notes needed by users and maintainers.

## Supported Local Runtimes

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
- Runtime UI configuration is persisted in `data/runtime-config.json` by default, or in `CGA_RUNTIME_CONFIG_PATH` when set.
- The Admin UI's System Settings / Indexing panel stores the default repos folder used when project indexing resolves a project without an explicit Repository Path.
- A backup sidecar dumps PostgreSQL with `pg_dump --format=plain | gzip` and FalkorDB runtime data into `data/backups/<stack>/` every hour by default.
- Override backup destination with `CGA_BACKUP_DIR` and the schedule with `CGA_BACKUP_INTERVAL_SECONDS` / `CGA_BACKUP_KEEP_COUNT`.
- Latest snapshots are written as `auth-latest.sql.gz` and `falkordb-latest.tgz` under the stack-specific backup folder.
- Restoring an auth snapshot uses `psql --single-transaction` and takes a pre-restore safety snapshot first.

The Admin UI's System Settings / Backup panel reads and writes the same folder, so manual Back Up Now, restore, and delete actions are visible to both the UI and sidecar.

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
