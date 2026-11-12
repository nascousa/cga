# CGA Runtime Operations

This guide keeps operational details out of the README while preserving the setup and maintenance notes needed by users and maintainers.

## Supported Local Runtimes

### Fixed Local Endpoint

CGA retains its configured host port **18001** and existing bind addresses.
Use `http://127.0.0.1:18001/admin` for the application, previews and UI validation;
do not create alternate preview ports such as 18770/18771 or silently rewrite
port mappings. Changing a port requires explicit user approval. A healthy
existing instance should be reused.

For a conflict, identify the exact listener, owning project and startup source
before stopping anything. Remove only a confirmed obsolete same-project
instance and its obsolete auto-start registration when necessary, preserving
data, configuration and recovery. Never terminate shared Docker host processes
or unrelated services. If ownership or permissions are unclear, report the
blocker instead of changing ports; verify the original URL and health afterward.
See the [ADC 1.2.4 policy](adc-framework.md#fixed-project-ports-and-conflict-cleanup).

### Selectable First-Party Plugins

The runtime supports eight optional plugins managed through
**User menu > Administration > Plugin management**.
The local runtime cutover below is verified.
See the [plugin guide](../extensions/README.md) for the catalog, authenticated
selection API, selective Docker builds and adapter-specific scheduling APIs.

- Unconfigured installations keep all bundled plugins enabled for compatibility.
  Selection is persisted under `plugins` in the existing runtime-config file;
  startup environment defaults do not override a saved selection.
- Disabling or uninstalling retains configuration, scheduled tasks, run history,
  snapshots and browser-local report history. Existing tasks whose adapter is
  unavailable record failed runs rather than making network requests.
- The scheduler, system Audit and ADC governance remain core. Removing Automation
  management does not stop jobs owned by still-enabled adapter plugins.
- Saving a selection reloads Admin. Disabling an actively executing plugin is
  rejected with HTTP 409; wait for its work to finish. Execution counters are
  process-local: this host is intended for the existing single-process API,
  not coordinated multi-worker activation.
- Enabling evaluation after starting with it disabled requires an API restart
  to start background trace recording/evaluation. Direct benchmarks are usable
  immediately; disabling pauses an already-started worker.
- Before replacing an existing distribution with a leaner image, save a selection
  containing only plugins bundled in the new image. A saved selection referencing
  absent plugins is rejected explicitly, rather than silently discarded.

These are in-process first-party plugins, not an arbitrary-code marketplace or
separately installed Python wheels. Build-time selection removes optional
implementations/assets; common Python dependencies remain shared.

#### Verified Local Plugin Cutover (2026-11-10)

- Admin entry: `http://127.0.0.1:18001/admin/extensions`. All eight bundled
  plugins are installed/enabled through the compatibility default; existing
  runtime-config contents were preserved byte-for-byte.
- Persistent Compose API image:
  `cga-local:1.30.131-plugins-20261110-201608`, image
  `sha256:179a4b321c2163038a6881a7e952797b3a9cc30523b7c568a3da1373006740b1`.
  This is a local feature overlay, not a signed/public release.
- Plugin-only patches were applied to the deployed files, retaining unrelated
  live functionality. ADC scripts were not replaced and matched their original
  bytes. Only the API service was recreated; all other **26 containers** retained
  their IDs and start times. Ports and mounts matched before/after.
- Normal configured-administrator login verified the eight-entry plugin catalog,
  Automation, extension listing, briefing/report source, and system Audit APIs.
  Anonymous plugin management returned HTTP 401. Plugin host/assets, deep links
  and health returned HTTP 200; Docker health was `healthy`.
- **27 projects** and ADC/configuration inventory hashes were unchanged. All
  **20 published graphs** retained node/edge counts. Temporary
  `__cga_stage__*` build graphs were excluded consistently from both snapshots;
  their creation/removal is normal indexing activity, not data loss.
- The initial readiness timeout coincided with the existing scheduled database
  dump holding schema locks; the original image was automatically restored.
  After the dump finished, startup and authenticated verification passed.
  Conservative inventory checks also rolled back on temporary graph changes
  before their filtering was corrected. No unrelated process was terminated.
- Rollback image: `cga-local:rollback-plugins-20261110-201608`.
  Original Compose/runtime settings, ADC assets and verification inventories are
  retained under
  `%USERPROFILE%\.nasco\docker\main\cga\backups\pre-plugins-20261110-201608`.
  The pre-cutover PostgreSQL archive is
  `backups\cga-desktop\auth\auth-20261111T041855Z-949e94845d594588ac43afa6cd49e26e.sql.gz`;
  gzip integrity passed, SHA256
  `d6d78bf27de5c6a76ae7fac6cc7c703554ca7376d0239d8c8f35100bc2e60d14`.
  No full restore rehearsal or plugin-driven data migration was performed.
  Preserve later administrator edits and history when rolling back.
- Source validation remains 179 tests and 14 subtests passed. The actual scoped
  image additionally passed isolated activation/API/assets/MCP checks, and its
  staged frontend passed syntax, report generation/history, lazy-loading and
  adapter-default tests. External BrowserAgent, cloud and notification-provider
  delivery were not newly exercised as part of this deployment.

#### Verified Extension Layout Update (2026-11-10)

- Live Extension now opens a responsive **Plugins** card catalog. The separate
  **Policy monitor** workspace contains its scan actions and configuration,
  rather than placing all forms below the catalog. Cards expose categories,
  explicit state, labeled controls and a sticky save bar with unsaved-change
  feedback. Section switching preserves pending selections.
- Only the deployed Admin HTML and plugin host JavaScript were overlaid.
  Backend code, data, runtime settings, ports and mounts were not changed.
  All **27 other running containers** retained IDs and start times.
- Persistent image:
  `cga-local:1.30.131-extension-layout-20261110-203743`, image
  `sha256:3f86aea495813e25c97d7f7595b3a025536f78464cd320d93524838e9834f2ef`.
  Docker health passed; served HTML/JavaScript matched the validated candidate
  byte-for-byte. This remains a local overlay, not a public release.
- Twenty targeted UI tests passed, including existing navigation, policy
  monitor, settings and logout behavior. Browser preview verified three-column
  and single-column layouts without content overflow, checkbox coupling,
  unsaved status and section switching without lost edits.
- Rollback: `cga-local:rollback-extension-layout-20261110-203743`. Original
  Compose and both frontend files are retained under
  `%USERPROFILE%\.nasco\docker\main\cga\backups\pre-extension-layout-20261110-203743`.

The subsequent naming-only update changed the topbar/page heading to **Plugins**
while retaining `/admin/extensions` and internal identifiers for compatibility.
Naming-only image: `cga-local:1.30.131-plugins-name-20261110-203928`;
rollback: `cga-local:rollback-plugins-name-20261110-203928`. Eight targeted
navigation/plugin UI tests passed, live HTML matched the candidate, and Docker
health passed. Original Compose/HTML are retained in
`backups\pre-plugins-name-20261110-203928`.

#### Verified Plugin Details Update (2026-11-10)

- Image for this deployment:
  `cga-local:1.30.131-plugin-details-20261110-213244-r2`, image
  `sha256:a318d8a29f0f15e115b42583b4bbf38692abb9a2b4102e7da830c26e308dd849`.
  This is a scoped local feature overlay, not a signed/public release.
  Unrelated checkout changes were not deployed.
- All eight plugins now have detail/settings pages. The catalog supports search,
  category/status filtering, sorting and Free/Paid metadata badges; all current
  plugins are Free. Enabled indicators are green, and catalog/detail action
  buttons include icons. Pricing labels do not implement billing or licensing.
- Thirteen UI tests passed against the actual staged frontend; sixteen source
  navigation/plugin UI tests also passed. The candidate passed isolated runtime
  checks. Normal administrator login verified all eight settings APIs and core
  endpoints; anonymous plugin management returned HTTP 401. Five served assets
  matched their staged hashes, and Docker health was `healthy`.
- Authenticated live-browser checks opened all eight detail pages, confirmed
  green Enabled indicators and Free badges, and exercised search, category
  filtering, descending sorting and reset without changing plugin settings.
  Shared navigation/plugin asset URLs are versioned to prevent an older cached
  navigation script from breaking the new catalog.
- **27 projects**, **20 published graphs**, **3 ADC tables**, runtime settings
  and ADC asset hashes were preserved. Ports/mounts were unchanged; all **26
  other running containers** retained IDs and start times. System Audit remains
  core. No external provider execution or delivery was exercised.
- An intermediate verification automatically restored the original image because
  Docker Desktop exposed the same bind directory using Windows and Linux-style
  source paths. Canonicalizing those equivalent paths and port-binding ordering
  allowed the final cutover to pass without relaxing mount/port checks. The
  deployment also waits for an existing database dump before restarting.
- Rollback: `cga-local:rollback-plugin-details-20261110-213244`, retaining the
  preceding naming-only image. Original Compose/runtime configuration, live
  source, inventories, payload manifest and final verification are retained in
  `%USERPROFILE%\.nasco\docker\main\cga\backups\pre-plugin-details-20261110-213244`.
  Pre-cutover database archive:
  `backups\cga-desktop\auth\auth-20261111T053716Z-2b73bfddfb854cc39cb291457ec09368.sql.gz`;
  gzip integrity passed, SHA256
  `5b5c4d17b735f0095a00c68049eba38272a2db56c2ff834e9679ac583ea1df2c`.
  No full restore rehearsal was performed. Preserve later settings/history when
  rolling back.

#### Verified Split Plugin Settings Deployment (2026-11-10)

- Image for this deployment:
  `cga-local:1.30.131-plugin-split-20261110-222016`, image
  `sha256:47325fd51b3f00dfd9b76c6e749d21cabec615cbf31828e127213078b9009ced`.
  This remains a local feature overlay, not a signed/public release.
- Only two frontend files were replaced. Admin HTML was generated from the
  previous live file by replacing plugin-specific styles, the plugin pane and
  three asset cache markers; the plugin host JavaScript contains the split
  settings implementation. Backend, ADC, Relay and unrelated checkout changes
  were not included.
- Plugins stay in the left searchable/filterable list while settings open on
  the right. Larger settings use grouped tabs; the monitor includes an Activity
  tab for outputs/findings/history. The redundant **Your plugins** heading and
  introductory paragraph were removed; plugin counts remain.
- Fourteen UI tests passed against the actual staged frontend and inherited
  report asset. Authenticated live-browser checks opened all eight plugin
  settings panels, exercised tabs, and confirmed the selected-card highlight,
  side-by-side layout, green Enabled indicator, removed introduction, button
  icons and no horizontal overflow. Prior isolated browser validation covered
  narrow-screen return, cross-tab drafts and hidden-field validation.
- Normal administrator login verified all eight settings APIs; anonymous plugin
  management returned HTTP 401. Both served frontend hashes matched the payload
  manifest and Docker health was `healthy`.
- **27 projects**, **20 published graphs**, **3 ADC tables**, runtime settings
  and ADC asset hashes were unchanged. Ports/mounts matched; all **26 other
  running containers** retained IDs and start times. No production settings or
  plugin selections were changed by browser verification.
- Rollback: `cga-local:rollback-plugin-split-20261110-222016`. Original Compose,
  runtime configuration, live frontend, inventories, verification probe, payload
  manifest and final verification are retained under
  `%USERPROFILE%\.nasco\docker\main\cga\backups\pre-plugin-split-20261110-222016`.
  This frontend-only cutover performed no database migration or restore; the
  preceding verified database archive remains retained. Roll back the API image
  without restoring the database or overwriting newer runtime settings.

#### Verified Compact Plugin Cards Deployment (2026-11-10)

- Persistent Compose image at that cutover:
  `cga-local:1.30.131-plugin-compact-20261110-223629`, image
  `sha256:1c2824e13fea031cde983ecf7de7b5dd58f3beb8bfd61e44ba13f7011d6d90ea`.
  This is a local frontend overlay, not a signed/public release.
- The overlay changes only five plugin CSS rules, the card footer wrapper and
  three asset cache markers in the existing live HTML/plugin host JavaScript.
  Installed/Enabled controls and Settings/Open actions share a single row when
  space permits. Pricing and activation badges are horizontal, retaining their
  independent meaning and colors. Narrow cards can wrap rather than overflow.
- Fourteen UI tests passed against the actual staged frontend. Both served
  assets matched the payload hashes. Normal administrator login verified all
  eight settings APIs; anonymous plugin management returned HTTP 401.
- Live browser measurements verified all eight cards at approximately **337px
  footer width**: controls/actions on one line, badges on one line, no card or
  page overflow, and icons present on every plugin action button. Asset URLs
  use `1.30.131-plugin-split.2` to avoid stale plugin markup from browser cache.
- Docker health was `healthy`. **27 projects**, **20 published graphs**, **3 ADC
  tables**, runtime settings and ADC asset hashes were unchanged. Ports/mounts
  matched and all **26 other running containers** retained IDs and start times.
  Backend, system Audit, ADC and Relay code were not replaced.
- Rollback: `cga-local:rollback-plugin-compact-20261110-223629`. Original Compose,
  frontend/configuration snapshots, inventories, deployment/staging helpers,
  verification probe, payload manifest and final verification are retained in
  `%USERPROFILE%\.nasco\docker\main\cga\backups\pre-plugin-compact-20261110-223629`.
  No database migration or restore was performed. Roll back only the API image
  and preserve subsequent settings and database activity.

#### Verified Admin Menu And Automation Deployment (2026-11-10)

- Persistent Compose image at that cutover:
  `cga-local:1.30.131-user-menu-20261110-234500`, image
  `sha256:7fde2dd888b3d7c71863ce351741d3ab6232047da35c05ac9c7f89617e775ff7`.
  This is a local frontend overlay, not a signed/public release.
- Only Admin HTML and the navigation JavaScript were replaced. The top bar
  retains Dashboard, Project, Graph, Automation, ADC and Audit. Administrator
  management entries now live under the user menu, separate from Profile and
  Sign out, with their original routes and access controls.
- Automation separates Tasks and Execution history, presents five task columns,
  keeps task details expandable and defaults advanced editor fields to collapsed.
  Project policy/evidence inputs now live under Audit > AI-First Readiness in a
  collapsed section. Plugin settings have a 12px status-to-button gap; Save is
  gray/disabled when clean and green/enabled only while settings differ.
- Twenty-two UI tests passed against the actual staged frontend, and navigation
  plus all inline JavaScript passed Node syntax checks. Served HTML/navigation
  hashes matched the payload. Normal administrator login verified all eight
  plugin settings APIs; anonymous management returned HTTP 401. Original
  Automation, Users, Security and ADC deep links served the same verified shell.
- Authenticated browser checks on **18001** verified the six top-level links,
  management icons/active state, menu dismissal/focus restoration, Automation
  tabs, five task columns, collapsed policy/advanced sections, editor name focus
  and icons on visible Automation actions. Actual settings changed locally from
  clean gray to edited green and back to clean gray without saving. At 380 CSS
  pixels, the menu stayed within the viewport and tables did not overflow the
  page. No alternate preview server or port was started.
- Docker remained healthy on the original **18001** mappings and mounts.
  **27 projects**, **20 published graphs**, **3 ADC tables** (including published
  ADC 1.2.4), runtime configuration and all other frontend asset hashes remained
  unchanged. All **26 other containers** retained IDs and start times.
  Existing authentication behavior, backend, ADC, Relay and plugin host code
  were preserved; unrelated dirty source changes were not deployed.
- Rollback: `cga-local:rollback-user-menu-20261110-234500-r2`. Original Compose,
  live frontend/configuration snapshots, deployed HTML/navigation bytes,
  inventories, helpers, payload hashes and successful verification are retained
  under `%USERPROFILE%\.nasco\docker\main\cga\backups\pre-user-menu-20261110-234500-r2`.
  No database migration or restore was performed.
- An initial verification incorrectly expected a dynamically created form ID in
  static HTML and triggered automatic rollback. The verifier was corrected to
  check static styles and the rendered form was verified separately; the final
  cutover and invariant checks passed.

#### Verified ADC Constitution Workspace Deployment (2026-11-11)

- Current persistent Compose image:
  `cga-local:1.30.132-adc-constitution-20261111-150500-r2`, image
  `sha256:c604c368f75d78adc4b79f66d1cb1c6983f8cd21cdb82a4a6340ac8448840f0d`.
  This is a local overlay, not a signed/public release. Product metadata was
  incremented once to **1.30.132** using the version helper; the installed Relay
  executable was not replaced.
- Only Admin HTML (ADC styles/cache markers), `adc.js`, `adc-versions.js` and the
  backend `APP_VERSION` constant were changed against the deployed baseline.
  Other pending checkout changes were excluded. Constitution uses a searchable
  **240px** document menu beside the editor; project-specific changes are under
  Amendments and the Versions history view is retained.
- Twenty-six exact-payload UI/version tests passed, including a regression for
  historical CRLF documents being incorrectly marked dirty after textarea
  normalization. Comparison normalizes line endings without rewriting published
  documents. JavaScript/Python syntax and version alignment checks passed.
- Served payload hashes matched the manifest. Health reports **1.30.132**.
  Authenticated checks verified master **ADC 1.2.4**, its **45 documents**, eight
  plugin settings APIs and original admin deep links; anonymous management
  still returned HTTP 401.
- Live browser verified all three tab names, the searchable document menu,
  selected document/content, Amendments project choices, and no false draft
  after opening/switching CRLF documents. Desktop measured a 240px menu and
  1122px editor. At 380 CSS pixels the layout stacked without page overflow.
  No release, project amendment or runtime setting was saved.
- Deployment-time before/after inventories matched: **27 projects**, **25
  published graphs**, **3 ADC tables**, runtime configuration, other frontend
  assets, ports and mounts. All **21 other running containers** retained IDs and
  start times. These counts describe this cutover, not the earlier deployment.
  The original `http://127.0.0.1:18001/admin/adc` endpoint remains in use.
- Final cutover evidence, original snapshots and deployed payload are retained
  in `%USERPROFILE%\.nasco\docker\main\cga\backups\pre-adc-constitution-20261111-150500-r2`.
  Its rollback tag is `cga-local:rollback-adc-constitution-20261111-150500-r2`
  (the initial ADC cutover). To return to the last pre-ADC-layout image, use the
  original Compose backup and tag
  `cga-local:rollback-adc-constitution-20261111-150500` retained under
  `backups\pre-adc-constitution-20261111-150500`. Roll back only the API image,
  preserving subsequent database/settings activity. No migration or database
  restore was performed.

### Unix Desktop Relay Candidate (2026-11-10)

The Relay source now includes a headless loopback gateway, macOS CLI and
Linux GTK/AppIndicator companion, user-scoped installers/startup entries,
native credential storage and signed-release jobs. See the
[Relay installation and release guide](../src/cga-relay/README.md).
Existing Windows Relay and CGA services were not replaced by this work.

Local verification:

- Windows: 94 Rust tests passed; hardened release build and existing
  release-process tests passed. The local binary remains unsigned.
- Linux x64: 99 default Rust tests passed; the separately invoked real GNOME
  Keyring integration test passed login, restart persistence and logout.
  Five isolated installer/package-gate tests, three native GTK/AppIndicator
  tests under private D-Bus/Xvfb and installation of the actual final archive
  passed. The candidate was built on Debian 12 (glibc 2.36); release-job Linux
  builds use Ubuntu 24.04 and require a compatible runtime.
- macOS Intel/Apple Silicon: Rust source and tests passed cross-target
  `cargo check`; native CLI/Keychain execution and
  Developer ID/notarization remain unverified without a macOS runner and
  provisioned signing credentials.
- New native CI/release jobs are configured, not claimed executed. Linux ARM64
  has not been run locally. Formal publication still requires all native checks
  and configured Windows, GPG and Apple signing gates.

The macOS scope was subsequently reduced to CLI-only at the user's request:
no `.app`, Swift compiler or menu-bar companion. Installation defaults to CLI;
the optional headless LaunchAgent and Keychain protection remain available.

Early validation caught incomplete credential fixtures, a read-only-source
fixture path, CRLF manifest parsing and a macOS-specific error-conversion issue;
these were corrected and the corresponding checks rerun successfully.

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

### Verified Relay Health Notification Fix (2026-11-10)

- Live Relay logs showed empty health responses during API/container restarts,
  not malformed application payloads. A single failed probe previously raised
  an outage notification, and a single successful probe immediately rearmed it.
- The Windows tray now confirms outages after three consecutive failed probes,
  clears them after two consecutive successful probes, and limits notifications
  across distinct outages to one per five minutes. Confirmed outages still show
  the warning icon during the cooldown. Failed probes and health transitions are
  logged, and reconnection remains automatic.
- Notification wording describes a connection failure rather than claiming the
  container is stopped. Empty responses explicitly report connection closure
  before HTTP headers; nonempty malformed responses remain errors.
- Validation passed: 28 Relay unit tests, four tray-status integration tests,
  three HTTP-response integration tests, and whitespace checks. The release
  candidate passed existing PE hardening, symbol/path disclosure and production
  single-instance verification.
- Installed local candidate SHA256:
  `9d25fd73d37c041a4d7ff514acaba455ba41c9763f6d6cb59ceee5730d330f0d`.
  This remains an unsigned local development candidate, not a public release.
  Only the identified installed Relay process was replaced and restarted with
  its original arguments. Configuration and existing account/cache file hashes
  were preserved. The loopback gateway returned HTTP 200, and repeated live
  health probes returned HTTP 200 with `available=true`, `notification=false`.
- Previous executable, invocation/state hashes and installation result are in
  `%USERPROFILE%\.cga\backups\relay-health-20261110`. Its executable SHA256 is
  `6305e1e8dfbc76f08177815b7b6842aa08e9d6c52e8c1daf5194487483cae1e3`.
  CGA remained healthy with its original container start time; no container was
  recreated or production outage induced for testing.

### Verified Relay And ADC Usability Deployment (2026-11-10)

- The local Windows Relay was replaced with the hardened development candidate;
  its installed SHA256 at that deployment was
  `56d7c12279d10221c60fd9a0367eca06dfa63c2280a706bd3117a6d900d3ac5d`.
  The existing account session and project binding were preserved, and the
  loopback settings gateway remained responsive. The original executable and
  invocation are retained under
  `%USERPROFILE%\.cga\backups\relay-cross-platform-20261110-1927`.
  Stop only the identified installed Relay process before restoring that binary.
  This candidate is unsigned, not a signed public release.
- ADC frontend image `cga-local:1.30.128-adc-usability-20261110.2` overlays only
  the three ADC scripts and their cache markers on the existing navigation-order
  image. Database services, volumes, credentials and unrelated navigation changes
  were preserved. Rollback by changing only the Compose service image to
  `cga-local:1.30.128-nav-order-20261110-191158` and recreating only service `cga`;
  do not restore the database.
- Versions defaults to current revisions, with explicit All history and project
  name/ID search. The table and right-aligned Sankey share the same filters.
  Draft guards cover editor replacement, unstaged project publication, collection,
  restoration and browser reload/close.
- The authenticated live inventory remained exactly 4 releases, 24 project
  revisions and 11 projects. Its pre/post deployment SHA256 was unchanged:
  `316d7aebaf7a19e9e3a387f42a5ee8a3fbd12329735e259f2bd923e3373d4c78`.
  No baseline, binding, collection or revision was modified by these UX checks.
- Browser verification found zero document overflow at widths 320, 390, 768,
  1280 and 1920. Explicit window resize events were dispatched because the
  integrated browser remained hidden and deferred native resize notifications;
  chart widths then matched their containers at every width. All 11 project
  endpoints had identical x coordinates. Theme color synchronization passed.
  An initially stale SVG width was corrected with containment and shared resize
  handling. The viewport and theme were restored afterward.
- The API container is healthy, GET `/admin/adc` returns 200, and the deployed
  versions script hash matches source. HEAD is not supported on that page.

```powershell
.\.venv\Scripts\python.exe -m pytest src\tests\test_adc_ui.py src\tests\test_adc_collection.py src\tests\test_session_expiry_ui.py src\tests\test_navigation_ui.py --no-cov -o addopts= -q
# Passed: 12 tests.
node --check src\frontend\adc-versions.js
# Passed.
git diff --check
# Passed.
```

Native macOS CLI/Keychain execution and formal platform signing/notarization
remain release gates; deploying this local Windows candidate does not satisfy
those gates.

### Version Iteration Follow-up (2026-11-10)

The usability overlay initially retained product version `1.30.128`. That missed
iteration was corrected to **1.30.129**, independently of image/cache suffixes.
Backend, Relay manifest/lockfile, README and ADC script cache versions now agree.
Use the [version iteration procedure](PUBLISHING.md#version-iteration) before
future builds; Policy CI validates alignment without incrementing on retries.

- API image at that cutover: `cga-local:1.30.129`, based on the verified ADC overlay.
  Only backend version metadata and HTML cache markers were changed in this
  follow-up image. `/health` and the browser report `1.30.129`.
- Installed Relay SHA256:
  `0f7ce63d933cf8791ed34e027401b1f48ee9a2c31ff6bd357781f491930a8cc8`.
  The hardened candidate verification passed; the existing installed tray was
  replaced/restarted, retaining its session and project binding.
- The previous API image `cga-local:1.30.128-adc-usability-20261110.2` remains
  available for image-only rollback. Earlier Relay backup instructions still
  apply. No database or immutable ADC version was changed.
- The targeted suite above, plus `test_versioning.py`, `test_health.py` and
  `test_release_workflow.py`, passed **20 tests**. Relay `cargo test --locked`
  passed **94 tests**. Mypy on `src\scripts\bump_version.py`, Ruff, metadata
  alignment and whitespace checks passed. Browser checks confirmed updated
  cache versions and the unchanged ADC inventory digest.

### Inline ADC Information Deployment (2026-11-10)

- Published immutable master **ADC 1.2.3**, changing only
  `.adc/standards/conventions/frontend.md`. Its SHA256 is
  `865614298bd8e465c77ece0dee6b3224ffce11f7f8cac91e4628cd1322c118fe`.
  The new policy requires concise visible copy and right-side, same-line info
  icons for complex explanations, with rich formatting and accessible behavior.
  Earlier release hashes and all 11 projects / 24 revisions were unchanged.
  Existing projects are not automatically upgraded to this new baseline.
- Incremented the product once to **1.30.130**. Final local API image:
  `cga-local:1.30.130-adc-info.2`. The build suffix records the formatting
  refinement within this iteration, not another product-version increment.
  The overlay changes only ADC scripts, HTML cache markers and backend version
  metadata on the verified 1.30.129 runtime.
- The installed hardened Relay reports the same package version; binary SHA256:
  `ea01bf6a70a0a5c3f395c61dfbf01d07b66c6f7ef3c0d2b771a258b36849e0e5`.
  Only the identified installed Relay was replaced/restarted, retaining its
  session and binding. The previous binary and Compose configuration are in
  the session's `adc-info-1.30.130` staging directory. API rollback can use
  `cga-local:1.30.129` without reverting the central policy database.
- The following command passed **20 tests**, including rich DOM formatting,
  escaped/untrusted text, line breaks/code, hover persistence, keyboard/click
  handlers, dismissal and resource cleanup:

```powershell
.\.venv\Scripts\python.exe -m pytest src\tests\test_versioning.py src\tests\test_health.py src\tests\test_release_workflow.py src\tests\test_adc_ui.py src\tests\test_adc_collection.py src\tests\test_session_expiry_ui.py src\tests\test_navigation_ui.py --no-cov -o addopts= -q
```

- JavaScript syntax, Ruff, product-version alignment and whitespace checks
  passed. Browser checks on `/admin/adc` verified same-line/right-side icons
  across all three subtabs at widths 320, 390 and 1280, no page overflow,
  viewport-contained rich tooltips, theme synchronization, field-label
  association and three live project-history help rows.
  The integrated page remained hidden: native focus/hover stability checks did
  not deliver reliably, so explicit DOM focus/pointer/keyboard events exercised
  their registered handlers. Click toggling worked. Redeployment interrupted
  existing index-status polling with transient `ERR_EMPTY_RESPONSE` events;
  final authenticated inventory and page/health checks succeeded.
- The final browser verified semantic code formatting, preserved line breaks,
  the published policy hash and all three cache markers. Removing only the new
  master from the inventory reproduces the previous full inventory digest
  `316d7aebaf7a19e9e3a387f42a5ee8a3fbd12329735e259f2bd923e3373d4c78`.
  No public tag or signed release was created.

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

The Admin briefing dashboard includes copyable PowerShell, Python, and JSON request templates for project-scoped activity publishing. The Audit > Report subtab can connect a Microsoft account with device-code login so generated WSR payloads can enrich stored PBI/PR references with Azure DevOps ticket details.

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

## Audit Workspace

The source implementation groups administrator-only **Audit** into two subtabs:
**Request log** and **Report**, which combines activity and AI-First readiness.
Existing evidence-pack, policy and signal tools remain inside Report's collapsed
advanced panel; weekly drafts, templates and browser-local history remain in a
separate collapsed panel. Server-side administrator authorization is unchanged.
Request-log polling stops when another subtab is selected.

Deep links are `/admin/audit` and `/admin/audit/report`. Legacy
`/admin/audit/ai-first`, `/admin/ai-first`, `/admin/aifirst`, and `/admin/report`
links select Report, preserving query
parameters and fragments. Browser Back/Forward restores the selected subtab.
Report configuration remains under Settings > Report; it is not a second
report-generation screen.

### Unified Report workspace

This redesign is source-only until separately packaged and deployed; the
installed 1.30.132 service has not been replaced by these changes.

- Select zero or more projects (zero means all), a 7/30/90-day or custom UTC
  period, literal title/summary text, event type and recorded status. Select
  activity, current readiness and/or evidence sections, then Generate.
- The inclusive through-date becomes next-day exclusive UTC. Windows must be
  nonempty and at most 366 days. `POST /api/admin/reports/generate` validates and
  applies filters in PostgreSQL before its 10,000-row cap. Results explicitly
  identify truncation; neither missing activity nor a source status proves a
  real-world outcome. Raw metadata and body text are excluded.
- Readiness reuses the current evidence API. Dates/type/status/search do not
  retrospectively filter readiness. Unknown scores are excluded from averages,
  not converted to zero. This is not a productivity or causal ROI report.
- ECharts SVG charts support time zoom and project/status drill-down. Chart
  clicks change draft filters; Generate applies them. Dirty filters disable
  snapshot exports. Evidence paginates at 25 rows; titles expand to summaries
  and stable activity IDs. Light/dark/system themes also update chart styling.
- CSV/JSON export the complete generated snapshot, not unlimited source data.
  CSV escapes quotes and neutralizes spreadsheet formulas. JSON also includes
  filters, coverage, current readiness and the generation timestamp.
- PDF uses Mermaid diagrams and a print-light layout, independent of screen
  theme. Browser rendering is embedded as raster pages, preserving rendered
  Unicode but not searchable PDF text. Evidence includes at most 100 records,
  with shortened titles/summaries and an explicit completeness note; use
  CSV/JSON for the full generated snapshot. Mermaid source is also downloadable.
  Files use `CGA-Report-v<product-version>-en.<ext>`.
- Existing AI-First controls keep their own project scope. Opening advanced
  tools does not alter the report snapshot or migrate/delete saved history.
  Reports remain optional and work without the briefing plugin.

The report workspace does not fetch chart/PDF libraries from a CDN. Both Docker
builds prepare their pinned local assets automatically. For a source checkout:

```powershell
Set-Location src\report-ui
npm ci
npm run build
```

Node/npm are build prerequisites, not API runtime dependencies. Generated
`src/frontend/report-vendor` assets are ignored by Git; include them and their
generated license directory in runtime/distribution packages. A missing bundle
is reported as a generation error, not an empty successful report.

### Audit retention, Change Logs and confirmed updates

The working tree implements these controls; the installed 1.30.132 API still
uses the earlier readiness-only Upgrade page. This is **source support, not a
deployment, signed release or successful real upgrade**. Existing local source
changes must be reviewed and selectively packaged before deployment.

#### Audit retention

Settings > Security shows record count, oldest timestamp, heap/index/total
storage and the last cleanup result. Automatic deletion is **disabled by
default**. The proposed defaults are 7 days for known successful polling,
90 days for other successful reads, and 365 days for protected events.
Protected events include errors, writes and sensitive account/configuration/
upgrade operations; their retention cannot be shorter than other categories.
Unknown/non-UTC timestamp formats are preserved.

Change the policy, run **Preview eligible records**, review/archive any required
evidence, and explicitly confirm **Save policy** to enable it. Preview is
read-only, expires after 15 minutes and is bound to the exact draft. Clean Save
is disabled and gray. Automatic eligibility is checked each minute and cleanup
runs at most hourly; each run permits five bounded batches, defaults to 1,000
rows per batch, and applies a 10-second SQL timeout per batch. A database
advisory lock serializes cleanup and policy changes across workers.
Administrators can explicitly request a bounded run under the saved policy.
Errors are recorded and surfaced rather than reported as success.

Backup **Keep Last N** prunes snapshot files, not `audit_logs`; Docker log
rotation is also separate. Row deletion makes database space reusable after
normal vacuuming; it does not promise immediate OS-level file shrinkage.
Do not automatically run `VACUUM FULL` or delete additional evidence to force
the capacity display down.

#### Change Logs

Settings > Change Logs separates **Unreleased** source changes from frozen
version entries and identifies the current product version. A local overlay
can differ from a formal build with the same version; the page states that
distinction. Historical notes are included only where verified evidence exists.
Release notes are plain text, not executable HTML.

Every product change needs a user-facing JSON fragment; completed version
iterations freeze and archive those fragments. See
[publishing](PUBLISHING.md#change-logs-and-trusted-update-manifests) for the
validation and signing workflow.

#### One-time host updater provisioning

The first implementation supports **one API process and an existing JSON
Docker Compose installation**. It does not update Relay/native installers,
support multi-replica orchestration or perform database/schema migrations.
An older formal release is never offered as an upgrade for a newer local build.
Checks occur every ten minutes; failures are explicit, not treated as
"up to date". Version-pinned installations cannot install automatically.

An operator must provision all of the following before confirmation becomes
available:

1. A trusted publisher RSA public PEM, installed through an independently
   verified channel and mounted read-only in the API. Do not bootstrap trust
   from the same unsigned release being checked. Set `CGA_UPDATE_PUBLIC_KEY`
   to its container path.
2. A dedicated shared queue/status directory, writable by API and executor,
   with `CGA_UPDATE_STATE_DIR` pointing to its API-side path.
3. An actual single-process API deployment, explicitly acknowledged with
   `CGA_UPDATE_SINGLE_PROCESS=1`. Do not set this flag on a multiworker service.
4. A private host config and journal/backup directory outside the shared state
   directory and all API-accessible mounts. Keep the Compose file and trusted
   key outside API-writable storage as well. Protect private files with host
   permissions: recovery includes the original Compose configuration.
5. A tested host-owned full-backup command and the exact paths of its fresh
   `auth.sql.gz` and `falkordb.tgz` outputs. The updater verifies freshness,
   nonempty gzip integrity and private copies; this is not a restore rehearsal.
6. A running independent host executor and a newer compatible formal release
   with a valid PS384-signed, digest-pinned `update-manifest.jwt`.

The following is a configuration shape, **not an installed backup launcher**.
Replace paths and ownership identifiers with the existing installation's values;
the example backup PowerShell script must be supplied and tested by the operator.
All directories must already exist.

```json
{
  "state_dir": "C:\\CGA\\update-state",
  "journal_dir": "C:\\CGA\\update-private\\journals",
  "compose_file": "C:\\CGA\\compose.json",
  "public_key": "C:\\CGA\\trust\\publisher-public.pem",
  "project": "cga-dev",
  "service": "cga",
  "container": "cga-desktop-api",
  "health_url": "http://127.0.0.1:18001/health",
  "backup_command": ["powershell.exe", "-NoProfile", "-File", "C:\\CGA\\operator\\backup-cga.ps1"],
  "backup_files": {
    "auth.sql.gz": "C:\\CGA\\backups\\latest\\auth.sql.gz",
    "falkordb.tgz": "C:\\CGA\\backups\\latest\\falkordb.tgz"
  }
}
```

Run the executor in a separately supervised host process using the same installed
Python dependencies as CGA, not inside the API container:

```powershell
python src\scripts\cga_update_host.py --config C:\CGA\update-private\updater.json
```

It never gives the API a Docker socket or arbitrary backup/host command endpoint.
Review notes, then explicitly confirm installation. The API rechecks the exact
version/digest and the host independently verifies its signature. Queued work
can be cancelled only before the host claims it. New API writes/MCP operations,
scheduled tasks and indexing starts pause while existing work drains.

The executor checks Compose ownership and the original health port, pulls the
signed digest, stops **only the configured API service**, verifies fresh backups
and replaces only its image. Verification checks health/version, image, original
ports, mounts, configured environment values and database reads. Image-provided
defaults may change; operator-configured environment values must not. The private
journal filesystem needs at least 2 GiB free; image storage and actual backup
capacity must also be sufficient. No `compose down`, volume deletion, port
substitution or environment-file rewrite is part of updating.

Failure restores the previous Compose/image, **never an older database over
newer writes**. Interrupted image switching uses the private journal for recovery.
Missing/corrupt journals or failed rollback require operator recovery; preserve
all state and backups instead of retrying blindly. The worker lock is OS-released
on exit. A stale shared queue-directory lock may be removed only after verifying
and stopping its owner; never steal a live lock. Private recovery snapshots and
rejected/cancelled queue receipts require operator-managed archival/retention.

## Responsive Admin Navigation

The admin topbar measures the space available after the logo, version and user
menu. When text labels no longer fit, all visible navigation buttons become a
single row of icons. Labels return automatically when space becomes available,
including after permission-dependent buttons are hidden. At very narrow widths,
only the icon strip scrolls horizontally; the toolbar does not wrap.

Each icon retains its accessible name and existing route. Hover or keyboard
focus displays an unclipped tooltip; Escape dismisses it. Keyboard focus can
scroll an off-screen icon into view without losing its tooltip.

The topbar order is **Dashboard → Project → Graph → Automation → Plugins →
ADC → User → Audit → Settings**: daily project work comes before governance
and system administration. Existing permission-dependent visibility is unchanged.
Automation appears immediately before Extension in the topbar. It is the
existing scheduler, renamed in navigation and its page heading; the
`/admin/schedule` deep link and task behavior remain unchanged.

The 2026-11-10 local navigation overlay passed 12 focused UI tests, JavaScript
syntax and Ruff checks. Browser fixtures verified text/icon/text restoration,
permission changes, hover/focus tooltips and single-row geometry at actual
viewport widths from 300 to 1920 CSS pixels, with zero toolbar or page overflow.
Only the API service was recreated; database, graph and queue services were not
replaced. The previous image and Compose configuration remain rollback sources.

### Verified Navigation Order Deployment (2026-11-10)

The local API at `http://127.0.0.1:18001` now uses
`cga-local:1.30.128-nav-order-20261110-191158`, image
`sha256:e8cd89f3920cc9efbb91aff1462efff9f9e2abd04764211ddd339a9c17aca3c2`.
Only the existing image's topbar button order was changed; the remainder of
the served Admin HTML was preserved. The live HTML matched the scoped overlay
exactly, and `/health` returned HTTP 200 with status `ok`.

Nine navigation/session-expiry regression tests passed. Only the API service
was recreated: the other 26 running containers retained their IDs and start
times. Persistent mounts and port bindings were unchanged. The active Compose
configuration retains the new image, so ordinary restarts preserve the change.

Rollback materials are the image
`cga-local:rollback-nav-order-20261110-191158` and the preceding Compose file
under `%USERPROFILE%\.nasco\docker\main\cga\backups\pre-nav-order-20261110-191158`.
This is a local UI overlay, not a public release or a Relay deployment.

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
