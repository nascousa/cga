# Publishing CGA (Context Graph Agent)

This guide covers the public GitHub release path for CGA, aka Context Graph Agent: source code, Docker images, and a one-file runtime compose download.

## Release Channels

### Plugin standards and developer SDK

SDK/tooling releases use `plugin-sdk-v<version>`, not the API/Relay `v<version>`
tag path. The [approval policy](plugins/CGA-Plugin-Approval-v1.0.0-en.md) defines
mandatory independent non-author reviews, exact commit/check binding and
operator reviewer/protection setup. Do not invent approvals or reuse API/Relay
signing credentials for an unrelated distribution.

Build a local candidate with:

```powershell
python -m pip install .\sdk\cga-plugin-sdk build "setuptools>=77"
python src\scripts\build_plugin_sdk.py --output .\dist\plugin-sdk-1.0.0
```

The empty-output-only builder pairs both language variants of standards and
approval policy with the SDK wheel/source, schema, developer guide, read-only
example, approval/build tools, full license and SHA-256 manifests.
It records committed versus dirty source explicitly and
never uploads, approves or installs code. Source inputs remain versioned in
`sdk/cga-plugin-sdk` and `docs/plugins`.

After review and merge, run **Plugin SDK release** from **main**, with its
approved PR number and matching SDK version. It verifies live review/check
metadata before building and again before publication, checks out the actual
merge commit, runs tests, attests artifacts and creates a new immutable release.
Configure both reviewer variables and the protected `plugin-sdk-release`
environment first. A local kit or candidate draft is not an attested release.
The existing API/Relay signature/deployment gates remain unchanged.

Publishing the workflow files requires GitHub credentials authorized for
workflow writes. The current OAuth credential rejected both the isolated SDK
branch push and exact-commit draft creation because it lacks `workflow` scope;
no remote SDK branch, approval PR or candidate release was created. Preserve the
local candidate/source patch for review until authorization is granted.
Do not delete gate files, change source targets or bypass required approvals.

### Application runtime channels

- Source download: GitHub automatically provides zip/tarball downloads for every release tag.
- Runtime bundle: the release workflow attaches `cga-<version>.tar.gz`, `docker-compose.release.yml`, and `SHA256SUMS.txt`. The unified checksum manifest covers the source bundle, runtime files, signed Windows Relay executable/ZIP and Unix Relay archives using their flat release asset names.
- Windows relay: the release workflow builds, Authenticode-signs, verifies, and attaches `cga-relay.exe`, `cga-relay-<version>-windows-x64.zip`, and matching SHA-256 files.
- Unix relay: the working-tree workflow is configured to build native Linux x64/ARM64 and macOS Intel/Apple Silicon archives named `cga-relay-<version>-<linux|macos>-<x64|arm64>.tar.gz` (plus macOS ZIPs), with checksum manifests. macOS is CLI-only, without an application bundle or Swift helper. Linux requires detached GPG signatures; macOS requires Developer ID signing and archive notarization. This configuration is not evidence that native validation or a signed public release has succeeded. See the [Desktop Relay guide](../src/cga-relay/README.md).
- One-click Docker Desktop zip: `deploy/docker-desktop/build-release-bundle.ps1` creates `CGA-Docker-Desktop-<version>.zip` with Windows launchers and `cga-desktop-api-image.tar` for fast local startup. Attach the zip and a matching `.zip.sha256` file to the GitHub Release after the tag workflow completes.
- Container image: tag pushes publish the API/runtime image to GitHub Container Registry:
  - `ghcr.io/nascousa/cga-api:<tag>`

## Local report assets

Development and production Dockerfiles build the Report ECharts/Mermaid/PDF
assets in a Node build stage with `npm ci` and `npm run build` from
`src/report-ui`. Keep its lockfile committed; do not rely on an operator's
untracked frontend bundle or a runtime CDN. Source-only runs require the same
commands before using Report. The resulting `src/frontend/report-vendor`
directory is generated, not committed. Runtime/selective Reports distributions
must preserve its chunks, legal-comment files and `licenses/` directory,
including collected upstream LICENSE/NOTICE/COPYING files and package metadata.
Validate an actual PDF download in addition to the chart bundle build.

## Version Iteration

Before building or deploying each completed product-change iteration, increment
the patch version once from the repository root:

```powershell
python src\scripts\bump_version.py
python src\scripts\bump_version.py --check
```

The increment synchronizes the backend, Relay manifest/lockfile, README version
and date, and ADC frontend cache versions. It validates all fields before writing
and refuses inconsistent metadata. Do not increment for every individual file
edit or rerun the increment merely to retry a build; use `--check` for retries.
Do not bulk-replace version numbers in historical deployment records, immutable
ADC releases, published files or existing release artifacts.

Policy CI checks version alignment without modifying the checkout. Incrementing
source metadata does not create a tag, publish a release or satisfy signing gates.

## Change Logs and Trusted Update Manifests

The maintenance/update implementation is source-only until separately packaged
and deployed. It does not make the older public release an upgrade for a newer
local build.

For each product change, add or update a JSON fragment under
`src/backend/maintenance/changes/`:

```json
{
  "summary": "Concise user-facing change",
  "changes": {
    "Added": ["Describe the capability and any important limitation."],
    "Fixed": ["Describe a corrected user-visible behavior."]
  }
}
```

Supported categories are Added, Changed, Fixed, Security, Removed, Deprecated
and Breaking. Include nonempty entries only; do not invent older history.
Use the existing version increment once the iteration is complete:

```powershell
python src\scripts\bump_version.py
python src\scripts\check_change_logs.py --release
```

The increment freezes the next version/date in
`src/backend/maintenance/change-logs.json`, archives the original fragments
under `changes/archive/<version>/`, synchronizes metadata/cache markers and
refuses an existing release/archive target. Pending notes appear as Unreleased
until then. Released entries and original archives must not be overwritten.
Policy CI compares product changes with the event's base commit, requires changed
pending notes or a new frozen version and rejects modifications/removal of
previously published history. Local `check_change_logs.py` validates current
history; use `--base <full-commit-hash>` to apply the committed-diff gate locally.
Release mode rejects pending fragments.

Formal API updates additionally require **`CGA_UPDATE_SIGNING_KEY`**, a private
RSA PEM of at least 3072 bits, supplied only through protected Actions secrets.
This is separate from Windows, Linux and Apple Relay signing credentials.
Distribute the matching public PEM through a trusted channel for both API and
host verification; never commit private keys, print secrets or bootstrap trust
from an unverified update download.

The release workflow runs `sign_update_manifest.py` inside the exact built API
image, binding frozen user-facing notes to its official GHCR SHA-256 digest,
version, minimum updater version, installation platform and compatible schemas.
PS384 metadata expires after one year. The initial workflow minimum is 1.30.133;
older installations need an operator-managed bootstrap first. Missing keys,
unfinished notes or inconsistent package/version inputs fail signing; existing
Relay signing gates remain mandatory.

The workflow publishes `update-manifest.jwt`, includes its checksum and uses the
same notes for the GitHub release. Existing assets are not overwritten. Source
changes, ephemeral-key tests and successful local builds do not prove a trusted
formal release or a real update/rollback. Host provisioning and first-phase
limitations are documented in
[runtime operations](runtime-operations.md#one-time-host-updater-provisioning).

## Current Release Readiness (2026-11-10)

| Area | Verified state |
| --- | --- |
| Source version | See the README for the current aligned backend/Relay candidate; this is not a public-release claim. |
| Main integration | Ontology query routing and runtime fixes merged through [PR #57](https://github.com/nascousa/cga/pull/57). The checked main commit is `9a3f714e36e30e7c380046759a389c2ec978b196`. |
| Main validation | [Policy CI](https://github.com/nascousa/cga/actions/runs/42691299869) passed Python quality/security and both Windows/Linux Relay jobs. [CodeQL](https://github.com/nascousa/cga/actions/runs/42691299246) passed all four analysis jobs. These are existing CI results, not a new local full-suite run. |
| Local runtime | The query-policy cutover and its limits are documented in [runtime operations](runtime-operations.md#verified-local-query-policy-cutover-2026-11-09). A read-only check on November 10 returned HTTP 200 from `http://127.0.0.1:18001/health`; no containers or data were changed. |
| Published release | [v1.30.113](https://github.com/nascousa/cga/releases/tag/v1.30.113) is still the latest published release. The v1.30.125 draft is unpublished historical preparation, not the current source candidate. |
| Signing gate | The recorded repository Actions secrets check returned zero repository secrets, so Windows signing was blocked. Unix release jobs require GPG and Apple credentials; API update manifests additionally require `CGA_UPDATE_SIGNING_KEY`. Credential provisioning and successful formal signing are not verified here. Local candidates must not be distributed as signed formal releases. |
| Merge gate | The active main ruleset requires a pull request and currently sets the required approving review count to zero. Historical draft notes about independent approval do not establish today's rule; recheck current rules and any applicable release approvals before publishing. |

### Remaining Maintainer Actions

1. Provision a trusted Windows code-signing PFX and configure
   `CGA_RELAY_SIGNING_CERT_BASE64` and `CGA_RELAY_SIGNING_CERT_PASSWORD` using
   secure GitHub Actions secret entry. Provision the Linux GPG and Apple
   Developer ID/notarization secrets listed in the preflight below as well.
   Provision the separate API publisher key `CGA_UPDATE_SIGNING_KEY` and
   independently distribute its trusted public PEM to updater installations.
   Do not send certificate material, private keys or passwords through issues,
   pull requests, logs, or chat.
2. Select the reviewed main commit for the release, confirm its version metadata
   matches the intended tag, and recheck its CI and current approval requirements.
   The November 9 local overlay is not a substitute for an exact release build.
3. Only after these prerequisites are satisfied, follow the tag flow below.
   Require all signed platform Relay jobs and image publication to succeed,
   then build and verify the matching Docker Desktop bundle.
4. Preserve existing published versions and the historical draft's source/assets.
   Do not relabel or overwrite them to imply they contain the current candidate.

The signing gate is unresolved. No new release tag, public release, or signed
artifact is claimed by this readiness update.

## Maintainer Preflight

Before the first public release:

1. Confirm the Apache License 2.0 text in `LICENSE` is still the intended public license.
2. Review `NOTICE.md` for current acknowledgements and trademark boundaries.
3. Review `DISCLAIMER.md` for current usage risks, sensitive-data handling, AI/automation limits, and third-party service boundaries.
4. Review `THIRD_PARTY_NOTICES.md` against the exact dependency set, browser assets, fonts/icons/images, and container images used by the release.
5. Confirm `.env`, `.deploy-keys/`, `data/`, `tmp/`, logs, local databases, and private keys are not tracked.
6. Update `README.md` version and date.
7. Configure `CGA_RELAY_SIGNING_CERT_BASE64` and `CGA_RELAY_SIGNING_CERT_PASSWORD` as GitHub Actions secrets. The certificate must be a trusted Windows code-signing PFX; never commit it or print either secret. The release script removes both values from the child-process environment before running Cargo, imports the certificate temporarily, signs by thumbprint so the password is not passed to `signtool.exe`, removes the imported certificate, and requires a valid RFC 3161 timestamp.
8. Build and inspect the unsigned local relay candidate. Formal tag builds repeat this check after signing:

```powershell
.\src\cga-relay\scripts\build-secure-release.ps1
```

An unsigned local candidate is validation-only and does not satisfy the formal
release gate. Before pushing a release tag, confirm that the Windows secrets
and the following Unix release secrets are configured:

- Linux: `CGA_RELAY_GPG_PRIVATE_KEY`, `CGA_RELAY_GPG_SIGNING_KEY`, with a
  noninteractive signing key.
- macOS: `CGA_RELAY_APPLE_CERT_BASE64`, `CGA_RELAY_APPLE_CERT_PASSWORD`,
  `CGA_RELAY_APPLE_SIGNING_IDENTITY`, `CGA_RELAY_APPLE_ID`,
  `CGA_RELAY_APPLE_APP_PASSWORD`, `CGA_RELAY_APPLE_TEAM_ID`.
- API update metadata: `CGA_UPDATE_SIGNING_KEY` (RSA PEM, at least 3072 bits).

Build and validate Unix candidates on each target OS/architecture using the
[native packaging commands](../src/cga-relay/README.md#build-test-and-release).
Record native UI, credential-store and signing results separately from installer
tests or Windows/Linux local checks. The selected commit must meet the current
repository and release approval requirements. Do not disable
`-RequireSignature`, GPG verification or Apple notarization to unblock a release.

9. Run local service validation from the repository root:

```bash
docker compose --profile dev config
docker compose --profile dev up --build
```

Then open `http://localhost:8001/admin` and `http://localhost:8001/mcp`.

## Tag Release Flow

Use Semantic Versioning tags only after maintainer preflight succeeds. Read the
selected source version from its manifest rather than copying a stale example.
These commands are not an instruction to publish while signing is blocked:

```bash
git status --short
git checkout main
git pull origin main
version="$(sed -n 's/^version = "\([^"]*\)"$/\1/p' src/cga-relay/Cargo.toml)"
git tag -a "v$version" -m "CGA v$version"
git push origin "v$version"
```

Pushing the tag runs `.github/workflows/release.yml`, which builds and publishes GHCR images and creates a GitHub Release only after the Windows and all Unix Relay jobs succeed. The workflow fails closed before publication if signing secrets are absent, Authenticode validation fails, PE mitigations are missing, debug symbols or build paths remain, Linux GPG signing/verification fails, macOS CLI signing/archive notarization/Gatekeeper checks fail, or artifact generation fails. Standalone CLI executables do not support stapling; Gatekeeper validates their online ticket. CI `-candidate` artifacts are separate development builds and must not be substituted for signed tag-release assets.

After the workflow succeeds, upload the locally built Docker Desktop zip and checksum to the same release:

```powershell
$version = (Select-String -Path src\cga-relay\Cargo.toml -Pattern '^version = "([^"]+)"$').Matches.Groups[1].Value
$zip = "deploy/docker-desktop/dist/releases/CGA-Docker-Desktop-$version.zip"
$checksum = "$zip.sha256"
$hash = (Get-FileHash $zip -Algorithm SHA256).Hash
"$hash  CGA-Docker-Desktop-$version.zip" | Set-Content -Path $checksum -Encoding ASCII
gh release upload "v$version" $zip $checksum --repo nascousa/cga
```

Do not overwrite existing release assets. If an asset already exists, stop and
verify the release commit and checksums before deciding how to proceed.

## User Install From Source

```bash
git clone https://github.com/nascousa/cga.git
cd cga
cp .env.example .env
docker compose --profile dev up --build
```

Open:
- Admin UI: `http://localhost:8001/admin`
- MCP discovery: `http://localhost:8001/mcp`
- FalkorDB Browser: `http://localhost:13000`

## User Install From One-Click Docker Desktop Zip

For non-technical Windows users, publish or send the generated `CGA-Docker-Desktop-<version>.zip` artifact. When downloading from GitHub Releases, verify the matching `CGA-Docker-Desktop-<version>.zip.sha256` file when possible.

1. Install Docker Desktop.
2. Unzip `CGA-Docker-Desktop-<version>.zip`.
3. Double-click `start-cga-desktop.cmd`.
4. Wait for the Admin UI to open.

The launcher creates `.env` when missing, loads `cga-desktop-api-image.tar` when present, starts the Docker Compose stack, waits for `/health`, and opens `http://localhost:18001/admin`. If the image tar is missing, it falls back to building from the packaged source.

Maintainers build that artifact with:

```powershell
Set-Location .\deploy\docker-desktop
.\build-release-bundle.ps1
```

## User Install From Release Images

Download `docker-compose.release.yml` and `.env.example`, then run:

```bash
cp .env.example .env
docker compose -f docker-compose.release.yml up -d
```

Open:
- Admin UI: `http://localhost:8000/admin`
- MCP SSE (`cga-mcp-server`): `http://localhost:8000/mcp/sse`
- FalkorDB Browser: `http://localhost:13000`

Pin a specific release image by setting `CGA_VERSION` in `.env`:

```bash
CGA_VERSION=v1.29.85
```

## GitHub Repository Settings

Recommended settings for a public launch:

- Repository name: `cga` under `nascousa`.
- Repository visibility: Public.
- Actions permissions: allow GitHub Actions to create releases and write packages.
- Packages: after first GHCR publish, make packages public if GitHub does not inherit repository visibility automatically.
- Pages: keep the existing docs workflow enabled if you want README/docs published as a static site.
- About section: set description, topics, and website URL.

Suggested topics: `mcp`, `ai-agents`, `code-indexing`, `graph-database`, `falkordb`, `developer-tools`.

If GitHub returns `Public repositories are not permitted for Enterprise Managed Organizations`, the repository cannot be made public under that organization. Use a public-capable organization/account or ask the enterprise administrator to enable public repositories before tagging the public release.

## Security Notes

- Never commit real `.env` files, tokens, OAuth secrets, private keys, SQLite auth databases, or deployment keys.
- Change `JWT_SECRET_KEY`, `ADMIN_USERNAME`, and `ADMIN_PASSWORD` before exposing the service beyond localhost.
- For public deployments, put the API behind TLS and restrict MCP access with project-scoped tokens.
- For formal public bundles, attach or archive an SBOM/license report for Python packages, npm packages, and container images.