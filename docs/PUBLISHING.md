# Publishing CGA (Context Graph Agent)

This guide covers the public GitHub release path for CGA, aka Context Graph Agent: source code, Docker images, and a one-file runtime compose download.

## Release Channels

- Source download: GitHub automatically provides zip/tarball downloads for every release tag.
- Runtime bundle: the release workflow attaches `cga-<version>.tar.gz`, `docker-compose.release.yml`, and `SHA256SUMS.txt`. The unified checksum manifest covers the source bundle, runtime files, signed Relay executable, and versioned Relay ZIP using their flat release asset names.
- Windows relay: the release workflow builds, Authenticode-signs, verifies, and attaches `cga-relay.exe`, `cga-relay-<version>-windows-x64.zip`, and matching SHA-256 files.
- One-click Docker Desktop zip: `deploy/docker-desktop/build-release-bundle.ps1` creates `CGA-Docker-Desktop-<version>.zip` with Windows launchers and `cga-desktop-api-image.tar` for fast local startup. Attach the zip and a matching `.zip.sha256` file to the GitHub Release after the tag workflow completes.
- Container image: tag pushes publish the API/runtime image to GitHub Container Registry:
  - `ghcr.io/nascousa/cga-api:<tag>`

## Current Release Readiness (2026-11-10)

| Area | Verified state |
| --- | --- |
| Source version | Backend and Relay declare `1.30.128`; this is not a public-release claim. |
| Main integration | Ontology query routing and runtime fixes merged through [PR #57](https://github.com/nascousa/cga/pull/57). The checked main commit is `9a3f714e36e30e7c380046759a389c2ec978b196`. |
| Main validation | [Policy CI](https://github.com/nascousa/cga/actions/runs/42691299869) passed Python quality/security and both Windows/Linux Relay jobs. [CodeQL](https://github.com/nascousa/cga/actions/runs/42691299246) passed all four analysis jobs. These are existing CI results, not a new local full-suite run. |
| Local runtime | The query-policy cutover and its limits are documented in [runtime operations](runtime-operations.md#verified-local-query-policy-cutover-2026-11-09). A read-only check on November 10 returned HTTP 200 from `http://127.0.0.1:18001/health`; no containers or data were changed. |
| Published release | [v1.30.113](https://github.com/nascousa/cga/releases/tag/v1.30.113) is still the latest published release. The v1.30.125 draft is unpublished historical preparation, not the current source candidate. |
| Signing gate | The repository Actions secrets API returned zero repository secrets. Neither required Relay signing secret is configured there. The documented local Relay candidate is unsigned and must not be distributed as a signed formal release. |
| Merge gate | The active main ruleset requires a pull request and currently sets the required approving review count to zero. Historical draft notes about independent approval do not establish today's rule; recheck current rules and any applicable release approvals before publishing. |

### Remaining Maintainer Actions

1. Provision a trusted Windows code-signing PFX and configure
   `CGA_RELAY_SIGNING_CERT_BASE64` and `CGA_RELAY_SIGNING_CERT_PASSWORD` using
   secure GitHub Actions secret entry. Do not send certificate material or its
   password through issues, pull requests, logs, or chat.
2. Select the reviewed main commit for the release, confirm its version metadata
   matches the intended tag, and recheck its CI and current approval requirements.
   The November 9 local overlay is not a substitute for an exact release build.
3. Only after these prerequisites are satisfied, follow the tag flow below.
   Require the signed Relay verification and image publication jobs to succeed,
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
release gate. Before pushing a release tag, confirm that both signing secrets
are configured and that the selected commit meets the current repository and
release approval requirements. Do not disable `-RequireSignature` to unblock a
release.

9. Run local service validation from the repository root:

```bash
docker compose --profile dev config
docker compose --profile dev up --build
```

Then open `http://localhost:8001/admin` and `http://localhost:8001/mcp`.

## Tag Release Flow

Use Semantic Versioning tags only after maintainer preflight succeeds. The
following example uses the current source version, `1.30.128`; it is not an
instruction to publish while signing is blocked:

```bash
git status --short
git checkout main
git pull origin main
git tag -a v1.30.128 -m "CGA v1.30.128"
git push origin v1.30.128
```

Pushing the tag runs `.github/workflows/release.yml`, which builds and publishes GHCR images and creates a GitHub Release. The workflow fails closed before publication if the Relay signing secrets are absent, Authenticode validation fails, PE mitigations are missing, debug symbols or build paths remain, or artifact generation fails.

After the workflow succeeds, upload the locally built Docker Desktop zip and checksum to the same release:

```powershell
$version = "1.30.128"
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