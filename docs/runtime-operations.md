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
