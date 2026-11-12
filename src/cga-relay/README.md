# CGA Desktop Relay

The dependency-free Rust core provides MCP, incremental Git indexing, account
settings and a loopback gateway. Windows uses the existing native notification
area UI. macOS is CLI-only; Linux adds a GTK3/Ayatana AppIndicator companion.
This source implementation is not itself a published,
signed or native-macOS-verified release.

## Install a Unix package

Extract the archive for your operating system and CPU, then run from that folder:

```sh
# macOS defaults to CLI (no GUI or automatic background process):
sh scripts/install-unix.sh --project-root "$HOME/my-project"
# Linux desktop:
sh scripts/install-unix.sh --mode desktop --project-root "$HOME/my-project"
# Or run the loopback gateway without a desktop:
sh scripts/install-unix.sh --mode headless --project-root "$HOME/my-project"
# Install without enabling a service:
sh scripts/install-unix.sh --mode cli
```

`--no-start` installs the requested startup entry without starting it. Quit an
existing tray before upgrading or switching modes. The installer stops its
previous managed service, replaces only the user installation and retains
configuration, scan state and logs.
No administrator privileges are required for user installation.

- **macOS:** CLI in `~/.local/share/cga-relay/bin/cga-relay`, symlink in
  `~/.local/bin`. No `.app`, menu bar, Swift or Python dependency. Only an
  explicit `--mode headless` installs the optional `com.nascousa.cga-relay`
  LaunchAgent. `--mode desktop` is rejected.
- **Linux:** `~/.local/share/cga-relay/bin/cga-relay`, symlink in
  `~/.local/bin`, XDG application/autostart entry for desktop **or** a systemd
  user service for headless mode. The service runs only within the user's
  session; it does not enable lingering or install a system-wide daemon.
- **Linux desktop dependencies (Debian/Ubuntu):**
  `python3`, `python3-gi`, `gir1.2-gtk-3.0`,
  `gir1.2-ayatanaappindicator3-0.1`, `xdg-utils`. Your desktop must expose an AppIndicator
  host; GNOME may require an AppIndicator shell extension. Wayland support
  depends on that desktop host, not on an X11-only tray implementation.
- **Linux saved account credentials:** install `libsecret-tools` and provide an
  unlocked Secret Service session (for example GNOME Keyring or a compatible
  KWallet Secret Service). There is no plaintext fallback.
- **macOS saved credentials:** the user's login Keychain must be available.
- **Git:** required for incremental Git indexing. The Rust executable does not
  require a source checkout, Node.js or a Python backend runtime.

Configuration is `~/.cga/relay.env`, created with mode `0600`; state and logs
are under `~/.cga/state` and `~/.cga/logs`. Existing configuration is not
overwritten. Review the API URL and project root before signing in. Keep token
values out of the config: credential configuration names environment variables,
not literal secrets.

Set `CGA_RELAY_CREDENTIAL_MODE=session` explicitly to keep new account tokens
only in that process's memory, for example on a headless machine without a
keyring. Restarting requires login again; no token is written to a plaintext
file. Default mode always requires the OS credential store. Legacy Unix
plaintext sessions are rejected rather than silently reused: sign out to remove
the old session, then sign in again using secure storage. Credential deletion
failures retain the reference and report an error so logout can be retried.

If the native credential store is unavailable, login reports an error rather
than saving plaintext. For an explicitly temporary login, set the process
environment variable `CGA_RELAY_CREDENTIAL_MODE=session` before starting Relay;
do not add it to `relay.env`. The token remains only in that process's memory
and does not survive restart. Legacy Unix plaintext sessions are rejected:
sign out in local settings, then sign in again to use protected storage.
Revoke the old token if it may have been exposed.

The transport currently permits only **loopback HTTP**. A remote HTTPS server
requires an independently configured policy-compliant local TLS proxy. Neither
the installer nor the relay silently downgrades a remote HTTPS connection.

## CLI, desktop and headless operation

The Windows/Linux menu offers account/backend status, CGA Web, local account settings, logs,
About, relaunch and exit. Native helpers exchange bounded status/actions with
the Rust parent over pipes, not account tokens. Desktop credential and network
errors remain visible rather than becoming successful login states.

On macOS, invoke CLI commands directly or configure the installed executable as
an MCP stdio server with `mcp --config "$HOME/.cga/relay.env"`. To sign in through
local browser settings or run the gateway manually:

```sh
~/.local/bin/cga-relay serve --config "$HOME/.cga/relay.env"
```

Open the loopback URL in `~/.cga/state/settings-url.txt`. The gateway binds only
to `127.0.0.1` and retains existing same-origin/intent protections. It serves
account settings, status and incremental-index requests; **it does not
automatically watch or synchronize every project**. Schedule explicit commands
when periodic synchronization is needed.

For Linux service management:

```sh
systemctl --user status cga-relay
journalctl --user -u cga-relay
systemctl --user restart cga-relay
```

The existing single-instance policy remains: stop the tray/service before
launching another CLI or stdio-MCP process. Unix instances are scoped to the
effective user with private lock directories; separate users do not block
one another.

To disable startup, run `sh scripts/uninstall-unix.sh` from the extracted
package. User config, state, logs and installed application files are retained;
quit the tray and remove the named application/binary directory manually.
**Sign out before uninstalling** to remove saved account credentials. Neither
uninstall nor upgrade deletes project sources, scan history or keychain entries.

## Build, test and release

Build packages **on the target OS/architecture**:

```sh
cargo test --locked
python3 scripts/tests/test_unix_install.py
sh scripts/build-unix-package.sh /absolute/path/to/new-output-directory
```

On macOS, use `bash scripts/tests/run-native-tests.sh` instead of direct
`cargo test`: it runs the same native tests against a temporary unlocked
Keychain and restores the original default/search list afterward. Synthetic
fixtures authorize the compiled test Relay identity without changing production
credential storage or requiring GUI consent. Policy and release CI share this
wrapper. Installer path assertions compare resolved paths because macOS
temporary directories may be reached through `/var` aliases.

macOS source builds require a Rust toolchain and the system C linker; packaging
uses `codesign` and `ditto`, with `xcrun notarytool` for formal releases. There is
no Swift/Cocoa helper to build. Linux desktop helper syntax can be checked with
`/usr/bin/python3 -m py_compile desktop/linux/tray.py`.
Native GTK smoke tests run under a private D-Bus/Xvfb session:
`dbus-run-session -- xvfb-run -a /usr/bin/python3 desktop/linux/test_tray.py`
(requires `xvfb`, `xauth`, `dbus-x11`). The ignored
`native_secret_service` integration test must also pass inside an isolated,
unlocked Secret Service session; CI starts a temporary GNOME Keyring for it.
After packaging, run `python3 scripts/tests/smoke_unix_package.py PACKAGE_DIR`
to verify an actual isolated installation and headless gateway.

Default packages contain `-candidate` in their names. macOS candidates use
ad-hoc signatures, **not Developer ID or notarization**. Do not instruct users
to bypass Gatekeeper. Output directories must not already exist, preventing
accidental replacement of an earlier package. SHA-256 files use flat archive
names suitable for validation after downloading.

Formal packaging uses `--release` and fails closed if signing is unavailable:

- Linux: an available noninteractive GPG signer selected by
  `CGA_RELAY_GPG_SIGNING_KEY`; detached archive signature is verified before
  upload. Verify both the trusted signing-key fingerprint and archive signature.
- macOS: `CGA_RELAY_APPLE_SIGNING_IDENTITY` (Developer ID Application),
  `CGA_RELAY_NOTARY_PROFILE`, optionally `CGA_RELAY_NOTARY_KEYCHAIN`.
  CLI executable signing, strict validation, accepted archive notarization and
  Gatekeeper assessment must all pass. Standalone executables cannot carry a
  stapled ticket; Gatekeeper uses Apple's online notarization ticket.
- Windows: unchanged Authenticode certificate and hardened-PE verification gate.

CI includes native Linux x64/ARM64, macOS Intel/Apple Silicon and Windows.
Installer tests use isolated home directories and mocked service managers;
they never modify the runner's actual login service. Candidate artifacts are
separate from tagged releases. A tagged release depends on **all** signed
platform jobs before publishing; missing Apple, GPG or Windows signing
credentials block publication rather than publishing an unsigned substitute.

Release secrets:

- Existing Windows signing certificate/password.
- `CGA_RELAY_GPG_PRIVATE_KEY`, `CGA_RELAY_GPG_SIGNING_KEY`.
- `CGA_RELAY_APPLE_CERT_BASE64`, `CGA_RELAY_APPLE_CERT_PASSWORD`,
  `CGA_RELAY_APPLE_SIGNING_IDENTITY`, `CGA_RELAY_APPLE_ID`,
  `CGA_RELAY_APPLE_APP_PASSWORD`, `CGA_RELAY_APPLE_TEAM_ID`.

Local Windows/Linux validation is not evidence that macOS CLI execution,
Keychain prompts, Intel/ARM hardware, desktop-specific Linux indicator hosts
or Apple notarization have passed. Record those native results before calling
the corresponding platform release production-ready.
