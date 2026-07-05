# CGA-Relay

`cga-relay` is the Rust desktop relay for CGA. It is designed as one installed relay per developer machine. Project repositories only need a small MCP pointer that launches the installed relay over stdio; they do not run their own long-lived MCP server.

## Install Or Build

For normal developer onboarding, install `cga-relay` on the machine where the repository checkout lives. Coding agents should launch that local executable over stdio instead of connecting directly to a remote `/mcp/sse` endpoint.

Preferred install paths:

1. Download the `cga-relay` binary for your platform from the CGA GitHub Release assets, when a release asset is available.
2. If a release asset is not available, build or install from source:

```powershell
Push-Location src/cga-relay
cargo install --path . --force
Pop-Location
cga-relay --help
```

The executable must be on `PATH`, or the project MCP pointer must use the full executable path.

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

For a multi-project workspace, use one machine-local env file per CGA project so `PROJECT_ID`, `PROJECT_ROOT`, and scan state do not collide. Example:

```json
{
	"servers": {
		"cga-relay": {
			"type": "stdio",
			"command": "cga-relay",
			"args": ["mcp", "--config", "%USERPROFILE%\\.cga\\my-project.env"],
			"env": {
				"CGA_COMMUNICATION_PROFILE": "CRYSTALS-CNSA-2.0",
				"CGA_TRANSPORT_SCOPE": "local-ipc"
			}
		}
	}
}
```

## Remote Or Cross-LAN CGA

If CGA runs on another LAN host or in remote Docker, do not point coding agents directly at the remote SSE endpoint. The agent should start local `cga-relay`; relay reads the local git worktree and forwards indexing requests to CGA.

For plaintext HTTP, relay intentionally accepts only loopback CGA URLs such as `http://127.0.0.1:18091`. If your CGA service is reachable at a LAN address such as `http://192.168.1.240:18091`, expose it to relay through an approved local loopback proxy or a deployment-approved PQC/hybrid-PQC TLS endpoint, then set both `API_BASE_URL` and `CONTROL_API_BASE_URL` in the machine-local env file to that local endpoint.

Example machine-local config for a LAN CGA behind a loopback proxy:

```text
AGENT_ID=dev-machine-codex
API_BASE_URL=http://127.0.0.1:18091
CONTROL_API_BASE_URL=http://127.0.0.1:18091
API_KEY_ENV=CONTEXTGRAPH_MCP_TOKEN
ACCOUNT_TOKEN_ENV=CGA_DEVELOPER_TOKEN
PROJECT_ID=replace-with-cga-project-id
PROJECT_ROOT=D:\Repos\ExampleProject
STATE_DIR=%USERPROFILE%\.cga\state\example-project
LOG_DIR=%USERPROFILE%\.cga\logs
```

Store token values only in the named environment variables or the relay account session. Never write token values into the env file or MCP pointer.

When a backend Admin Reindex request cannot run git in the API container, CGA returns `relay_required`. That is expected for remote Docker setups. Use `index_git_incremental` through `cga-relay`; relay computes `git status` on the developer machine and forwards `index_incremental` with the local changed paths.

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
