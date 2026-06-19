# CGA Docker Desktop Bundle

This folder is the recommended local distribution for non-developer Docker Desktop usage.

## What It Does

- Starts CGA, PostgreSQL, FalkorDB, Redis, and the backup sidecar with one command.
- In release zips, loads the bundled prebuilt CGA API image before startup.
- Falls back to building the CGA API image from source when no prebuilt image tar is present.
- Includes the CGA license, notices, disclaimer, security, contribution, and third-party notice files at the package root.
- Starts with a fresh local runtime: no customer projects, private repositories, PostgreSQL data, FalkorDB graph index, Redis state, backups, or sample/demo project data are bundled.
- Stores the last active local ports in `tmp/cga-desktop-runtime.json` so the reopen launcher can find the correct URL.

## Author And Attribution

CGA (Context Graph Agent) was created and authored by Nate Scott. Preserve this attribution when sharing, publishing, or redistributing the Docker Desktop bundle.

## One-Click Windows Launchers

- `start-cga-desktop.cmd`: start the stack and open the Admin UI
- `open-cga-desktop.cmd`: reopen the Admin UI using the saved desktop port
- `status-cga-desktop.cmd`: show container state and the current URLs
- `stop-cga-desktop.cmd`: stop the stack
- `logs-cga-desktop.cmd`: follow the container logs

## Build A Portable Package

- Run `build-portable-bundle.cmd`.
- It creates `dist/CGA-Docker-Desktop` with a self-contained copy of the launcher, Docker build files, and a local `repos` folder.
- This developer package can still build locally if no prebuilt CGA API image tar is present.
- The package root includes `LICENSE`, `NOTICE.md`, `OPEN_SOURCE.md`, `THIRD_PARTY_NOTICES.md`, `DISCLAIMER.md`, `SECURITY.md`, `CONTRIBUTING.md`, and `CODE_OF_CONDUCT.md`.

## Build A Versioned Release Zip

- Run `build-release-bundle.cmd`.
- It reads the current CGA app version and creates both:
	- `dist/releases/CGA-Docker-Desktop-<version>`
	- `dist/releases/CGA-Docker-Desktop-<version>.zip`
- The release folder includes `cga-desktop-api-image.tar`, a prebuilt CGA API image that the launcher loads automatically.
- That zip is the recommended release artifact to publish or send to another user.
- For GitHub Releases, publish the zip with a matching `CGA-Docker-Desktop-<version>.zip.sha256` checksum file.

## License And Notices

Release zips include these files at the package root:

- `LICENSE`: Apache License 2.0 terms for CGA.
- `NOTICE.md`: project notices and acknowledgements.
- `OPEN_SOURCE.md`: open-source release summary and redistributor notes.
- `THIRD_PARTY_NOTICES.md`: direct dependency and container image notices.
- `DISCLAIMER.md`: usage limits, sensitive-data, and third-party service boundaries.
- `SECURITY.md`: vulnerability reporting and security guidance.
- `CONTRIBUTING.md`: contribution terms.
- `CODE_OF_CONDUCT.md`: community participation expectations.

## Default URLs

- Admin UI: `http://localhost:18001/admin`
- MCP SSE (`cga-mcp-server`): `http://localhost:18001/mcp/sse`
- FalkorDB Browser: `http://localhost:13001`

## First Run

1. Install Docker Desktop.
2. Open this folder in File Explorer.
3. Double-click `start-cga-desktop.cmd`.
4. Wait for the browser to open the Admin UI.

If `.env` does not exist yet, the launcher creates it from `.env.example` automatically.
In release zips, the launcher loads the prebuilt CGA API image from `cga-desktop-api-image.tar`, starts the services, waits for `/health`, and then opens the Admin UI. If that image tar is missing, startup uses the source-build fallback.

The first run initializes empty PostgreSQL, FalkorDB, and Redis Docker volumes. It creates the configured admin account and database tables, but it does not import Nate Scott's local projects, ship prebuilt repository indexes, or seed a sample project. Add repositories to `repos` or set `CGA_REPOS_MOUNT`, then index them from CGA.

For scripted validation without opening a browser, run:

```powershell
.\start-desktop.ps1 start -WaitForReady:$true
```

## Configuration

Edit `.env` in this folder when you want fixed credentials, fixed ports, or a different code mount.

Important settings:

- `CGA_REPOS_MOUNT`: host folder mounted into `/repos`
- `CGA_DESKTOP_API_PORT`: Admin and MCP host port
- `CGA_DESKTOP_FALKORDB_PORT`: FalkorDB host port
- `CGA_DESKTOP_BROWSER_PORT`: FalkorDB Browser host port
- `ADMIN_USERNAME` / `ADMIN_PASSWORD`: local login

When this bundle stays inside the CGA repository, the default `CGA_REPOS_MOUNT=../../` points back to the repository root. If you copy this folder somewhere else, change that path to the code folder you want indexed and also update `docker-compose.yml` build context paths.