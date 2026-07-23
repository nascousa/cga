param(
    [Parameter(Mandatory = $false)]
    [string]$OutputFolder = (Join-Path $PSScriptRoot 'dist\CGA-Docker-Desktop')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot '..\..')
$portableRoot = $OutputFolder

if (Test-Path $portableRoot) {
    Remove-Item -Path $portableRoot -Recurse -Force
}

New-Item -ItemType Directory -Path $portableRoot | Out-Null
New-Item -ItemType Directory -Path (Join-Path $portableRoot 'repos') | Out-Null
New-Item -ItemType Directory -Path (Join-Path $portableRoot 'src') -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $portableRoot 'extensions') -Force | Out-Null

$filesToCopy = @(
    @{ Source = Join-Path $repoRoot 'Dockerfile.dev'; Target = Join-Path $portableRoot 'Dockerfile.dev' },
    @{ Source = Join-Path $repoRoot 'docker-entrypoint.sh'; Target = Join-Path $portableRoot 'docker-entrypoint.sh' },
    @{ Source = Join-Path $repoRoot 'requirements.txt'; Target = Join-Path $portableRoot 'requirements.txt' },
    @{ Source = Join-Path $repoRoot 'LICENSE'; Target = Join-Path $portableRoot 'LICENSE' },
    @{ Source = Join-Path $repoRoot 'NOTICE.md'; Target = Join-Path $portableRoot 'NOTICE.md' },
    @{ Source = Join-Path $repoRoot 'OPEN_SOURCE.md'; Target = Join-Path $portableRoot 'OPEN_SOURCE.md' },
    @{ Source = Join-Path $repoRoot 'THIRD_PARTY_NOTICES.md'; Target = Join-Path $portableRoot 'THIRD_PARTY_NOTICES.md' },
    @{ Source = Join-Path $repoRoot 'DISCLAIMER.md'; Target = Join-Path $portableRoot 'DISCLAIMER.md' },
    @{ Source = Join-Path $repoRoot 'SECURITY.md'; Target = Join-Path $portableRoot 'SECURITY.md' },
    @{ Source = Join-Path $repoRoot 'CONTRIBUTING.md'; Target = Join-Path $portableRoot 'CONTRIBUTING.md' },
    @{ Source = Join-Path $repoRoot 'CODE_OF_CONDUCT.md'; Target = Join-Path $portableRoot 'CODE_OF_CONDUCT.md' },
    @{ Source = Join-Path $PSScriptRoot 'start-desktop.ps1'; Target = Join-Path $portableRoot 'start-desktop.ps1' },
    @{ Source = Join-Path $PSScriptRoot 'start-cga-desktop.cmd'; Target = Join-Path $portableRoot 'start-cga-desktop.cmd' },
    @{ Source = Join-Path $PSScriptRoot 'open-cga-desktop.cmd'; Target = Join-Path $portableRoot 'open-cga-desktop.cmd' },
    @{ Source = Join-Path $PSScriptRoot 'status-cga-desktop.cmd'; Target = Join-Path $portableRoot 'status-cga-desktop.cmd' },
    @{ Source = Join-Path $PSScriptRoot 'logs-cga-desktop.cmd'; Target = Join-Path $portableRoot 'logs-cga-desktop.cmd' },
    @{ Source = Join-Path $PSScriptRoot 'stop-cga-desktop.cmd'; Target = Join-Path $portableRoot 'stop-cga-desktop.cmd' }
)

foreach ($item in $filesToCopy) {
    Copy-Item -Path $item.Source -Destination $item.Target -Force
}

$excludedSourceDirectoryNames = @(
  'target',
  'node_modules',
  'dist',
  '__pycache__',
  '.pytest_cache',
  '.ruff_cache'
)
$excludedSourceFilePatterns = @(
  '*.pyc',
  '*.pyo',
  '.coverage',
  '*.log'
)

function Copy-SourceTreeExcludingGenerated {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Source,

    [Parameter(Mandatory = $true)]
    [string]$Destination
  )

  Get-ChildItem -LiteralPath $Source -Force | ForEach-Object {
    if ($_.PSIsContainer) {
      if ($excludedSourceDirectoryNames -contains $_.Name) {
        return
      }
      $targetDirectory = Join-Path $Destination $_.Name
      New-Item -ItemType Directory -Path $targetDirectory -Force | Out-Null
      Copy-SourceTreeExcludingGenerated -Source $_.FullName -Destination $targetDirectory
      return
    }

    foreach ($pattern in $excludedSourceFilePatterns) {
      if ($_.Name -like $pattern) {
        return
      }
    }

    Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $Destination $_.Name) -Force
  }
}

Copy-SourceTreeExcludingGenerated -Source (Join-Path $repoRoot 'src') -Destination (Join-Path $portableRoot 'src')
Copy-SourceTreeExcludingGenerated -Source (Join-Path $repoRoot 'extensions') -Destination (Join-Path $portableRoot 'extensions')

$generatedSourcePaths = @(
  'src\cga-relay\target',
  'src\viewer\node_modules',
  'src\viewer\dist',
  'src\dist'
)
foreach ($relativePath in $generatedSourcePaths) {
  $generatedPath = Join-Path $portableRoot $relativePath
  if (Test-Path $generatedPath) {
    Remove-Item -Path $generatedPath -Recurse -Force
  }
}

if (-not (Test-Path (Join-Path $portableRoot 'src\scripts\init_auth_db.py'))) {
  throw 'Portable bundle source copy failed: src\scripts\init_auth_db.py is missing.'
}
if (-not (Get-ChildItem -LiteralPath (Join-Path $portableRoot 'extensions') -Directory)) {
  throw 'Portable bundle extension copy failed: no extensions were copied.'
}

# The backup sidecar mounts this script directly; make sure it lives at the
# expected path inside the bundle.
Copy-Item -Path (Join-Path $repoRoot 'src\scripts\backup-runtime-data.sh') `
          -Destination (Join-Path $portableRoot 'src\scripts\backup-runtime-data.sh') -Force

# Use a placeholder char (DEL, 0x7F) in the heredoc to bypass PowerShell
# expansion of $-prefixed compose variables; we replace it with '$' below.
$D = [char]0x7f
$portableCompose = @"
name: cga-desktop-portable

services:
  cga:
    build:
      context: .
      dockerfile: Dockerfile.dev
    image: cga-desktop-portable-cga:local
    restart: unless-stopped
    ports:
      - "$D{CGA_DESKTOP_API_PORT:-18001}:8000"
    env_file:
      - path: .env
        required: false
    environment:
      - FALKORDB_HOST=falkordb
      - FALKORDB_PORT=6379
      - FALKORDB_URL=falkor://falkordb:6379
      - FALKORDB_URL_HOST=redis://localhost:$D{CGA_DESKTOP_FALKORDB_PORT:-16381}
      - FALKORDB_BROWSER_URL=http://falkordb:3000
      - FALKORDB_BROWSER_PUBLIC_URL=http://localhost:$D{CGA_DESKTOP_BROWSER_PORT:-13001}
      - QUEUE_REDIS_URL=redis://redis:6379/1
      - CACHE_REDIS_URL=redis://redis:6379/2
      - JWT_SECRET_KEY=$D{JWT_SECRET_KEY:-change-me-at-least-32-chars!!!!!}
      - ADMIN_USERNAME=$D{ADMIN_USERNAME:-admin}
      - ADMIN_PASSWORD=$D{ADMIN_PASSWORD:-changeme}
      - GITHUB_OAUTH_CALLBACK_URL=$D{GITHUB_OAUTH_CALLBACK_URL:-http://localhost:$D{CGA_DESKTOP_API_PORT:-18001}/api/auth/github/callback}
      - PYTHONPATH=/app/src
      - MCP_ACCESS_TOKEN=$D{MCP_ACCESS_TOKEN:-}
      - CGA_POSTGRES_DSN=postgresql://$D{POSTGRES_USER:-app}:$D{POSTGRES_PASSWORD:-app}@postgres:5432/$D{POSTGRES_DB:-appdb}
      - WORKBRIEFING_POSTGRES_DSN=postgresql://$D{POSTGRES_USER:-app}:$D{POSTGRES_PASSWORD:-app}@postgres:5432/$D{POSTGRES_DB:-appdb}
      - BACKUP_DIR=/backups/cga-desktop-portable/auth
    volumes:
      - $D{CGA_BACKUP_DIR:-./data/backups}:/backups
      - "$D{CGA_REPOS_MOUNT:-./repos}:/repos:ro"
    depends_on:
      postgres:
        condition: service_healthy
      falkordb:
        condition: service_healthy
      redis:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 30s

  postgres:
    image: pgvector/pgvector:pg16
    restart: unless-stopped
    environment:
      - POSTGRES_DB=$D{POSTGRES_DB:-appdb}
      - POSTGRES_USER=$D{POSTGRES_USER:-app}
      - POSTGRES_PASSWORD=$D{POSTGRES_PASSWORD:-app}
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U $D{POSTGRES_USER:-app} -d $D{POSTGRES_DB:-appdb}"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 20s

  falkordb:
    image: falkordb/falkordb:latest
    restart: unless-stopped
    ports:
      - "$D{CGA_DESKTOP_FALKORDB_PORT:-16381}:6379"
      - "$D{CGA_DESKTOP_BROWSER_PORT:-13001}:3000"
    volumes:
      - falkordb_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 3s
      retries: 5
      start_period: 20s

  redis:
    image: redis:7-alpine
    restart: unless-stopped
    command: redis-server --appendonly yes
    volumes:
      - redis_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 3s
      retries: 3

  backup:
    image: postgres:16-alpine
    restart: unless-stopped
    environment:
      - BACKUP_STACK_NAME=cga-desktop-portable
      - BACKUP_ROOT=/backups
      - PGHOST=postgres
      - PGPORT=5432
      - PGUSER=$D{POSTGRES_USER:-app}
      - PGDATABASE=$D{POSTGRES_DB:-appdb}
      - PGPASSWORD=$D{POSTGRES_PASSWORD:-app}
      - FALKORDB_DATA_DIR=/falkordb-data
      - BACKUP_INTERVAL_SECONDS=$D{CGA_BACKUP_INTERVAL_SECONDS:-3600}
      - BACKUP_KEEP_COUNT=$D{CGA_BACKUP_KEEP_COUNT:-168}
    volumes:
      - falkordb_data:/falkordb-data:ro
      - $D{CGA_BACKUP_DIR:-./data/backups}:/backups
      - ./src/scripts/backup-runtime-data.sh:/usr/local/bin/backup-runtime-data.sh:ro
    entrypoint: ["/bin/sh", "/usr/local/bin/backup-runtime-data.sh"]
    depends_on:
      postgres:
        condition: service_healthy
      falkordb:
        condition: service_healthy

volumes:
  postgres_data:
  falkordb_data:
  redis_data:
"@

$portableCompose = $portableCompose.Replace([char]0x7f, '$')
Set-Content -Path (Join-Path $portableRoot 'docker-compose.yml') -Value $portableCompose -Encoding UTF8

$portableEnv = @"
# CGA portable Docker Desktop bundle settings
CGA_DESKTOP_API_PORT=18001
CGA_DESKTOP_FALKORDB_PORT=16381
CGA_DESKTOP_BROWSER_PORT=13001

# Drop repositories you want to index into the local ./repos folder,
# or point this at another host folder.
CGA_REPOS_MOUNT=./repos

# Backup snapshots go into this host folder (shared by API and sidecar).
CGA_BACKUP_DIR=./data/backups
CGA_BACKUP_INTERVAL_SECONDS=3600
CGA_BACKUP_KEEP_COUNT=168

# Auth / app
JWT_SECRET_KEY=change-me-at-least-32-chars!!!!!
ADMIN_USERNAME=admin
ADMIN_PASSWORD=changeme

# PostgreSQL (auth + work briefings + pgvector)
POSTGRES_DB=appdb
POSTGRES_USER=app
POSTGRES_PASSWORD=app

MCP_ACCESS_TOKEN=
GITHUB_OAUTH_CLIENT_ID=
GITHUB_OAUTH_CLIENT_SECRET=
GITHUB_OAUTH_CALLBACK_URL=http://localhost:18001/api/auth/github/callback
"@
Set-Content -Path (Join-Path $portableRoot '.env.example') -Value $portableEnv -Encoding UTF8

$portableDockerIgnore = @"
.env
.env.*
!.env.example
repos
tmp
data/backups
src/cga-relay/target
src/viewer/node_modules
src/viewer/dist
src/dist
__pycache__
*.pyc
.pytest_cache
.coverage
coverage
*.log
Thumbs.db
.DS_Store
"@
Set-Content -Path (Join-Path $portableRoot '.dockerignore') -Value $portableDockerIgnore -Encoding UTF8

$portableReadme = @"
# CGA Portable Docker Desktop Package

This folder is a self-contained CGA package for Docker Desktop. CGA runs on
PostgreSQL for auth, projects, audit logs, and work-briefing vectors. Release
packages include a prebuilt CGA API image tar that the launcher loads before
startup; developer packages fall back to building from source when the tar is
not present.

The package does not include Nate Scott's local projects, private repositories,
PostgreSQL data, FalkorDB graph indexes, Redis state, backups, or sample/demo
project data. First run creates a fresh local runtime with an admin account and
empty data stores. Add repositories to ``repos`` or set ``CGA_REPOS_MOUNT``,
then index them from CGA.

## Author And Attribution

CGA (Context Graph Agent) was created and authored by Nate Scott. Preserve this
attribution when sharing, publishing, or redistributing this Docker Desktop
bundle.

## Use

1. Copy repository folders you want to analyze into ``repos``.
2. Double-click ``start-cga-desktop.cmd``.
3. Open ``http://localhost:18001/admin`` if the browser does not open automatically.

For scripted validation without opening a browser, run:

~~~powershell
.\start-desktop.ps1 start -WaitForReady:`$true
~~~

## License And Notices

The package root includes the CGA open-source and customer-facing notice files:

- ``LICENSE``
- ``NOTICE.md``
- ``OPEN_SOURCE.md``
- ``THIRD_PARTY_NOTICES.md``
- ``DISCLAIMER.md``
- ``SECURITY.md``
- ``CONTRIBUTING.md``
- ``CODE_OF_CONDUCT.md``

Review these files before redistributing or exposing CGA beyond a local desktop
environment.

## Backups

- The bundled ``backup`` sidecar runs ``pg_dump`` of the auth database and a
  tar of FalkorDB data every hour by default into ``./data/backups``.
- The admin UI's **System Settings -> Backup** panel reads and writes the same
  folder, so manual snapshots are visible to the sidecar and vice versa.
- Tune ``CGA_BACKUP_INTERVAL_SECONDS`` / ``CGA_BACKUP_KEEP_COUNT`` in ``.env``.

## Included Launchers

- ``start-cga-desktop.cmd``
- ``open-cga-desktop.cmd``
- ``status-cga-desktop.cmd``
- ``logs-cga-desktop.cmd``
- ``stop-cga-desktop.cmd``

## Notes

- Release zips load ``cga-desktop-api-image.tar`` automatically and start from
  the prebuilt CGA API image.
- If ``cga-desktop-api-image.tar`` is absent, startup uses the source-build
  fallback and builds the local CGA image from the packaged source files.
- Edit ``.env`` if you want different ports, credentials, or a different
  ``CGA_REPOS_MOUNT`` / ``CGA_BACKUP_DIR`` path.
- The packaged ``.dockerignore`` excludes ``repos`` and ``data/backups`` so
  user data does not bloat the Docker build context.
"@
Set-Content -Path (Join-Path $portableRoot 'README.md') -Value $portableReadme -Encoding UTF8

New-Item -ItemType File -Path (Join-Path $portableRoot 'repos\.gitkeep') -Force | Out-Null
Set-Content -Path (Join-Path $portableRoot 'repos\README.txt') -Value "Drop repositories to index in this folder, or edit .env and point CGA_REPOS_MOUNT at another host folder. The release package does not ship Nate Scott's local project repositories or prebuilt index data." -Encoding UTF8

Write-Host "Portable bundle created at: $portableRoot"
