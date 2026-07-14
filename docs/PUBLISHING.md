# Publishing CGA (Context Graph Agent)

This guide covers the public GitHub release path for CGA, aka Context Graph Agent: source code, Docker images, and a one-file runtime compose download.

## Release Channels

- Source download: GitHub automatically provides zip/tarball downloads for every release tag.
- Runtime bundle: the release workflow attaches `cga-<version>.tar.gz`, `docker-compose.release.yml`, and `SHA256SUMS.txt`.
- One-click Docker Desktop zip: `deploy/docker-desktop/build-release-bundle.ps1` creates `CGA-Docker-Desktop-<version>.zip` with Windows launchers and `cga-desktop-api-image.tar` for fast local startup. Attach the zip and a matching `.zip.sha256` file to the GitHub Release after the tag workflow completes.
- Container image: tag pushes publish the API/runtime image to GitHub Container Registry:
  - `ghcr.io/nascousa/cga-api:<tag>`

## Maintainer Preflight

Before the first public release:

1. Confirm the Apache License 2.0 text in `LICENSE` is still the intended public license.
2. Review `NOTICE.md` for current acknowledgements and trademark boundaries.
3. Review `DISCLAIMER.md` for current usage risks, sensitive-data handling, AI/automation limits, and third-party service boundaries.
4. Review `THIRD_PARTY_NOTICES.md` against the exact dependency set, browser assets, fonts/icons/images, and container images used by the release.
5. Confirm `.env`, `.deploy-keys/`, `data/`, `tmp/`, logs, local databases, and private keys are not tracked.
6. Update `README.md` version and date.
7. Run local validation from the repository root:

```bash
docker compose --profile dev config
docker compose --profile dev up --build
```

Then open `http://localhost:8001/admin` and `http://localhost:8001/mcp`.

## Tag Release Flow

Use Semantic Versioning tags. Example for `1.29.85`:

```bash
git status --short
git checkout main
git pull origin main
git tag -a v1.29.85 -m "CGA v1.29.85"
git push origin v1.29.85
```

Pushing the tag runs `.github/workflows/release.yml`, which builds and publishes GHCR images and creates a GitHub Release.

After the workflow succeeds, upload the locally built Docker Desktop zip and checksum to the same release:

```powershell
$version = "1.30.113"
$zip = "deploy/docker-desktop/dist/releases/CGA-Docker-Desktop-$version.zip"
$checksum = "$zip.sha256"
$hash = (Get-FileHash $zip -Algorithm SHA256).Hash
"$hash  CGA-Docker-Desktop-$version.zip" | Set-Content -Path $checksum -Encoding ASCII
gh release upload "v$version" $zip $checksum --repo nascousa/cga --clobber
```

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