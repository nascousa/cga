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

On Windows MSVC targets, the crate config enables static CRT linking, fat LTO, symbol stripping, panic abort, Control Flow Guard, ASLR, high-entropy ASLR, DEP/NX, CET compatibility, and reproducible linker metadata. Build formal artifacts through the secure release script:

```powershell
.\src\cga-relay\scripts\build-secure-release.ps1
```

The script rejects binaries that retain COFF, CodeView, or embedded PDB symbols; omit required PE mitigations or concrete CFG/CET metadata; contain writable-executable sections; expose common absolute build paths or test secrets; or contain UPX markers. It produces `cga-relay.exe`, a versioned Windows x64 zip, and SHA-256 files. GitHub tag releases additionally require a valid Authenticode signature and RFC 3161 timestamp.

These controls remove routine symbol and path disclosure and raise the cost of static analysis. They do not make native code impossible to disassemble or decompile. CGA-Relay does not use UPX or similar packers because they are reversible and commonly increase endpoint-security false positives.

## Verify Release Artifacts

GitHub Release assets are uploaded with flat file names. Download `SHA256SUMS.txt` and every file named by it into one directory, then run `sha256sum --check SHA256SUMS.txt`. To verify only the Windows Relay assets from PowerShell, compare each asset to its sidecar and require a valid Authenticode signature before execution:

```powershell
$version = '1.30.117'
$assets = @('cga-relay.exe', "cga-relay-$version-windows-x64.zip")
foreach ($asset in $assets) {
	$expected = ((Get-Content ".\$asset.sha256" -Raw) -split '\s+')[0]
	$actual = (Get-FileHash ".\$asset" -Algorithm SHA256).Hash.ToLowerInvariant()
	if ($actual -ne $expected) { throw "SHA-256 mismatch: $asset" }
}
if ((Get-AuthenticodeSignature .\cga-relay.exe).Status -ne 'Valid') {
	throw 'CGA-Relay Authenticode signature is not valid.'
}
```

Never publish or install a formal release artifact when either check fails. Locally built unsigned candidates are for development validation only.

## Safe Config

Use `docs/examples/cga-relay.env.example` as the starting point for a machine-local config file such as `%USERPROFILE%\.cga\relay.env`.

Config files may contain endpoint URLs, stable project identity, local paths, and environment variable names. Do not store token values, API keys, or passwords in config files. The parser uses a strict allowlist of documented keys, rejects duplicate keys and unknown assignments without logging their values, and requires credential environment variable names to match `[A-Za-z_][A-Za-z0-9_]*`. An inline credential such as `CGA_DEVELOPER_TOKEN=<value>` is invalid rather than silently ignored. The same environment-variable-name validation applies to `login --token-env`.

Required identity fields:

- `AGENT_ID`: stable developer-machine relay id.
- `PROJECT_ID`: stable backend project id.
- `PROJECT_ROOT`: local checkout path.
- `API_KEY_ENV`: environment variable name that contains the backend/MCP API key.
- `ACCOUNT_TOKEN_ENV`: environment variable name that contains the developer token for sync.

Scanner and sync limits:

- `MAX_FILE_BYTES`: maximum source file size accepted by the scanner.
- `MAX_BATCH_BYTES`: optional maximum serialized JSON request size. The default is `8388608` bytes (8 MiB). Each request is also limited to 500 snapshots or tombstones.
- Relay HTTP responses are limited to `8388608` bytes (8 MiB), and connected sockets use 30-second read and write timeouts.

## CLI

```powershell
cga-relay --help
cga-relay doctor --config %USERPROFILE%\.cga\relay.env --json
cga-relay login --config %USERPROFILE%\.cga\relay.env --email dev@example.com --token-env CGA_DEVELOPER_TOKEN --json
cga-relay projects add --config %USERPROFILE%\.cga\relay.env --namespace dev --project-tag example --root C:\Repos\ExampleProject --json
cga-relay projects list --config %USERPROFILE%\.cga\relay.env --json
cga-relay scan --config %USERPROFILE%\.cga\relay.env --dry-run --json
cga-relay sync --config %USERPROFILE%\.cga\relay.env --all --dry-run --json
cga-relay index git --config %USERPROFILE%\.cga\relay.env --repo-path C:\Repos\ExampleProject --branch feature/example --parent-ref main
cga-relay index incremental --config %USERPROFILE%\.cga\relay.env --repo-path C:\Repos\ExampleProject --changed-path src\main.py --ref feature/example
cga-relay refs promote --config %USERPROFILE%\.cga\relay.env --repo-path C:\Repos\ExampleProject --ref feature/example --parent-ref main --delete-ref-graph
cga-relay settings --config %USERPROFILE%\.cga\relay.env --status --json
cga-relay settings --config %USERPROFILE%\.cga\relay.env --render
cga-relay tray --config %USERPROFILE%\.cga\relay.env
cga-relay tray --config %USERPROFILE%\.cga\relay.env --status --json
cga-relay mcp --config %USERPROFILE%\.cga\relay.env
```

`login` stores only profile metadata and the token environment variable name. It never writes the token value to disk.

## Settings Page

When `tray` starts, CGA-Relay also starts a loopback-only dark-mode settings page on `127.0.0.1` and records its URL under `STATE_DIR/settings-url.txt`. The tray `Settings` menu item opens this relay settings page, not the CGA admin settings screen.

The local settings listener limits request headers and bodies to 64 KiB each, applies 30-second read and write timeouts, rejects ambiguous content framing, and validates state-changing POST requests against the listener's exact loopback `Host` and browser `Origin`. Responses disable framing and apply a restrictive Content Security Policy. These controls reduce loopback denial-of-service, cross-site request forgery, and DNS rebinding risk; they do not make the settings page remotely accessible.

The settings page lets the developer sign in with a CGA account and review current user groups. After login, CGA-Relay calls `/api/auth/me` and `/api/auth/me/groups`, caches the account session and current user's group-to-project mappings under `STATE_DIR`, derives local relay project access only from those group mappings, and imports group-authorized projects with valid local `repo_path` values into the local registry under the `account` namespace. On Windows, the cached JWT is encrypted with current-user Windows Data Protection API (DPAPI); a copied session cannot be decrypted by another Windows user or machine. Existing plaintext Windows sessions are migrated when first read. Non-Windows source builds restrict the session file to mode `0600`; token environment variables remain the preferred non-Windows credential path. Relay status checks the JWT expiration claim before reporting the account as signed in. An expired cached JWT remains protected local state for diagnosis but cannot authorize sync, MCP, refresh, settings, or tray signed-in status; sign in again to obtain a new JWT. The cached account session is local machine state and must not be copied into repository files or committed. Use the Settings page `Refresh access` button after CGA admin group or project membership changes to reload access without signing out.

After account login, MCP tool calls and `sync` can use the user JWT relay bridge at `/api/auth/cga-relay/mcp-tool` and `/api/auth/cga-relay/sync`. For projects registered under the `account` namespace, `sync` prefers a current account JWT even when a global project-token environment variable is also configured; this prevents a token bound to one project from being sent for another account-authorized project. If the cached account session has expired, account-project sync fails before scanning instead of silently falling back to that global project token. If the server rejects an otherwise unexpired account JWT with HTTP 401, Relay removes the invalid local session and retries through the project endpoint only when an explicit project token is configured. The rejected JWT remains invalidated across all projects and batches in that `sync` invocation. Relay fails closed if the local session cannot be removed, and it never applies fallback to HTTP 403 access denials. Project-token routes remain supported for other namespaces and for deployments that prefer explicit per-project tokens.

## Windows Tray Icon

`tray` runs the same standalone Rust executable as a Windows notification-area relay with a native Shell_NotifyIcon tray icon. The executable icon uses the embedded color `R` resource, while the tray icon uses the embedded gray `R` resource when no CGA account is signed in and switches to the embedded color `R` resource after account login. It does not launch Python, Node, Cargo, PowerShell, or a project-local MCP server.

When `tray` starts successfully, CGA-Relay releases the startup console so the long-running tray process does not leave a blank command window on the desktop. Status and diagnostic commands such as `tray --status --json`, `doctor`, and `settings --render` keep normal terminal output.

Left-clicking the tray icon shows a short running-status message. Right-clicking opens a native menu that first displays `Not signed in` or `Signed in: <username>`, followed by `Settings`, `Logs`, `About`, and `Exit` options. `Settings` opens the CGA-Relay settings page, `Logs` opens the configured log directory, `About` shows the relay version, author, repository, support link, license, relay id, and current account user groups, and `Exit` stops the tray process. Use `tray --status --json` for automation or installers that need to confirm tray support without starting the long-running message loop.

Relay communication logs are written under `LOG_DIR` as hourly UTC timestamped `.log` files named `YYYYMMDD-HH.log`. The relay records MCP stdin/stdout events, local settings requests, outbound CGA HTTP requests, and CGA HTTP responses. MCP payloads, local settings request bodies, and backend HTTP request and response bodies are represented only by byte counts; payload content and payload hashes are not stored. Untrusted response headers, reason phrases, malformed responses, and oversized response prefixes are also represented only by byte counts and status codes where available. Authorization headers, bearer values, token fields, password fields, API key fields, secret fields, cookies, and form-style sensitive values are redacted before anything is appended to disk.

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

`scan` walks the configured root deterministically, applies include/exclude globs, skips oversized and binary files, hashes scanned text files with SHA-256, and reports candidate, excluded, scanned, changed, unchanged, oversized, skipped binary, tombstone, and bytes scanned counts. Built-in excludes cover dependency/build outputs, Python `.venv` and `venv` environments, secret-like files, `.deploy-keys`, and local `.nasco` notes; if a previously synced path later becomes excluded, the next normal scan or sync reports a tombstone for cleanup.

`--dry-run` never updates scan state. Normal scan mode writes local state under `STATE_DIR`. `sync` reads the central relay project registry, fails closed if login or token environment is missing or the only account JWT has expired, and submits changed text snapshots to the configured control API when not in dry-run mode. Scan progress is written to stderr after every 500 processed candidates and at completion; per-batch progress also uses stderr so stdout remains machine-readable JSON.

Sync requests are deterministic and bounded by both 500 items and `MAX_BATCH_BYTES`. The scanner retains snapshot metadata instead of all changed source bodies in memory. Immediately before submission, the relay reads each file again and verifies its size and SHA-256 digest. Every accepted batch updates the local scan-state checkpoint, so a later batch failure resumes from the remaining changes instead of restarting the full first sync.

The project-token backend bridge is exposed at `/api/project/cga-relay/mcp-tool` and `/api/project/cga-relay/sync`. These routes are protected by project tokens through the existing `/api/project` middleware and require the authenticated project identity to match the submitted `project_id`. The account-login bridge is exposed at `/api/auth/cga-relay/mcp-tool` and `/api/auth/cga-relay/sync` and is protected by the normal user JWT flow.

## Branch Graphs

Relay MCP indexing and query tools support isolated temporary ref graphs through `ref_id`, `branch`, or `git_branch`. Parent aliases are `parent_ref`, `base_ref`, and `base_branch`. The `promote_ref` tool reindexes source-ref file paths from the merged target working tree into the parent/default graph and can then delete the source graph.

See [BRANCH-GRAPHS.md](BRANCH-GRAPHS.md) for graph naming, fallback behavior, promotion semantics, examples, and current limitations.
