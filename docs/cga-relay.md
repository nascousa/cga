# CGA-Relay

`cga-relay` is the Rust desktop relay for CGA. It is designed as one installed relay per developer machine. Project repositories only need a small MCP pointer that launches the installed relay over stdio; they do not run their own long-lived MCP server.

## Build And Test

```powershell
Push-Location src/cga-relay
cargo test
cargo build --release
Pop-Location
```

The crate has no third-party Rust dependencies. The release build produces a standalone `cga-relay.exe` at `src/cga-relay/target/release/cga-relay.exe`; install or copy that executable onto the developer machine and launch it directly. Project MCP config must call that installed executable, not `cargo`, Python, PowerShell scripts, or a per-project MCP server.

On Windows MSVC targets, the crate config enables static CRT linking to reduce external runtime requirements for the release executable.

## Safe Config

Use `docs/examples/cga-relay.env.example` as the starting point for a machine-local config file such as `%USERPROFILE%\.cga\relay.env`.

Config files may contain endpoint URLs, stable project identity, local paths, and environment variable names. Do not store token values, API keys, or passwords in config files.

Required identity fields:

- `AGENT_ID`: stable developer-machine relay id.
- `PROJECT_ID`: stable backend project id.
- `PROJECT_ROOT`: local checkout path.
- `API_KEY_ENV`: environment variable name that contains the backend/MCP API key.
- `ACCOUNT_TOKEN_ENV`: environment variable name that contains the developer token for sync.

## CLI

```powershell
cga-relay --help
cga-relay doctor --config %USERPROFILE%\.cga\relay.env --json
cga-relay login --config %USERPROFILE%\.cga\relay.env --email dev@example.com --token-env CGA_DEVELOPER_TOKEN --json
cga-relay projects add --config %USERPROFILE%\.cga\relay.env --namespace dev --project-tag example --root C:\Repos\ExampleProject --json
cga-relay projects list --config %USERPROFILE%\.cga\relay.env --json
cga-relay scan --config %USERPROFILE%\.cga\relay.env --dry-run --json
cga-relay sync --config %USERPROFILE%\.cga\relay.env --all --dry-run --json
cga-relay settings --config %USERPROFILE%\.cga\relay.env --status --json
cga-relay settings --config %USERPROFILE%\.cga\relay.env --render
cga-relay tray --config %USERPROFILE%\.cga\relay.env
cga-relay tray --config %USERPROFILE%\.cga\relay.env --status --json
cga-relay mcp --config %USERPROFILE%\.cga\relay.env
```

`login` stores only profile metadata and the token environment variable name. It never writes the token value to disk.

## Settings Page

When `tray` starts, CGA-Relay also starts a loopback-only dark-mode settings page on `127.0.0.1` and records its URL under `STATE_DIR/settings-url.txt`. The tray `Settings` menu item opens this relay settings page, not the CGA admin settings screen.

The settings page lets the developer sign in with a CGA account and review current user groups. After login, CGA-Relay calls `/api/auth/me` and `/api/auth/me/groups`, caches the account session and current user's group-to-project mappings under `STATE_DIR`, derives local relay project access only from those group mappings, and imports group-authorized projects with valid local `repo_path` values into the local registry under the `account` namespace. Use the Settings page `Refresh access` button after CGA admin group or project membership changes to reload the current account's group-authorized project access without signing out. The cached account session is local machine state and must not be copied into repository files or committed.

After account login, MCP tool calls and `sync` can use the user JWT relay bridge at `/api/auth/cga-relay/mcp-tool` and `/api/auth/cga-relay/sync` when project-token environment variables are not configured. Project-token routes remain supported for deployments that prefer explicit per-project tokens.

## Windows Tray Icon

`tray` runs the same standalone Rust executable as a Windows notification-area relay with a native Shell_NotifyIcon tray icon. The executable icon uses the embedded color `R` resource, while the tray icon uses the embedded gray `R` resource when no CGA account is signed in and switches to the embedded color `R` resource after account login. It does not launch Python, Node, Cargo, PowerShell, or a project-local MCP server.

When `tray` starts successfully, CGA-Relay releases the startup console so the long-running tray process does not leave a blank command window on the desktop. Status and diagnostic commands such as `tray --status --json`, `doctor`, and `settings --render` keep normal terminal output.

Left-clicking the tray icon shows a short running-status message. Right-clicking opens a native menu that first displays `Not signed in` or `Signed in: <username>`, followed by `Settings`, `Logs`, `About`, and `Exit` options. `Settings` opens the CGA-Relay settings page, `Logs` opens the configured log directory, `About` shows the relay version, author, repository, support link, license, relay id, and current account user groups, and `Exit` stops the tray process. Use `tray --status --json` for automation or installers that need to confirm tray support without starting the long-running message loop.

Relay communication logs are written under `LOG_DIR` as hourly UTC timestamped `.log` files named `YYYYMMDD-HH.log`. The relay records MCP stdin/stdout, local settings requests, outbound CGA HTTP requests, and CGA HTTP responses. Authorization headers, bearer values, token fields, password fields, API key fields, secret fields, cookies, and form-style sensitive values are redacted before anything is appended to disk.

## MCP Pointer

Use `docs/examples/cga-relay.mcp.json` as the project-side pointer. It launches the installed `cga-relay` command with `mcp --config ...` over stdio.

The pointer does not reference `/mcp/sse`, does not launch a per-project MCP server, and does not include secret values.

## CRYSTALS/CNSA 2.0 Communication Profile

VSCodeAgent-to-Relay communication uses local stdio IPC. Relay-to-CGA communication uses the CRYSTALS/CNSA 2.0 profile on every HTTP request:

- `X-CGA-Communication-Profile: CRYSTALS-CNSA-2.0`
- `X-CGA-Key-Establishment: ML-KEM-1024`
- `X-CGA-Signature: ML-DSA-87`
- `X-CGA-Transport-Scope: local-ipc` for local loopback development.

The relay allows plaintext HTTP only for loopback hosts such as `127.0.0.1` and `localhost`. Remote CGA deployments must be reached through a PQC-capable TLS endpoint or approved hybrid-PQC local proxy before being used with CGA-Relay.

## Scanner And Sync

`scan` walks the configured root deterministically, applies include/exclude globs, skips oversized and binary files, hashes scanned text files with SHA-256, and reports candidate, excluded, scanned, changed, unchanged, oversized, skipped binary, tombstone, and bytes scanned counts. Built-in excludes cover dependency/build outputs, secret-like files, `.deploy-keys`, and local `.nasco` notes; if a previously synced path later becomes excluded, the next normal scan or sync reports a tombstone for cleanup.

`--dry-run` never updates scan state. Normal scan mode writes local state under `STATE_DIR`. `sync` reads the central relay project registry, fails closed if login or token environment is missing, and submits changed text snapshots to the configured control API when not in dry-run mode.

The project-token backend bridge is exposed at `/api/project/cga-relay/mcp-tool` and `/api/project/cga-relay/sync`. These routes are protected by project tokens through the existing `/api/project` middleware and require the authenticated project identity to match the submitted `project_id`. The account-login bridge is exposed at `/api/auth/cga-relay/mcp-tool` and `/api/auth/cga-relay/sync` and is protected by the normal user JWT flow.
