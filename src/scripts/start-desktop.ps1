param(
    [Parameter(Mandatory = $false, Position = 0)]
    [ValidateSet('start', 'stop', 'restart', 'logs', 'status', 'open', 'config')]
    [string]$Command = 'start',

    [Parameter(Mandatory = $false)]
    [bool]$Detached = $true,

    [Parameter(Mandatory = $false)]
    [bool]$OpenBrowser = $false
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$composeFile = Join-Path $repoRoot 'docker-compose.desktop.yml'
$envExample = Join-Path $repoRoot '.env.example'
$envFile = Join-Path $repoRoot '.env'
$runtimeStateFile = Join-Path $repoRoot 'tmp\cga-desktop-runtime.json'
. (Join-Path $PSScriptRoot 'desktop-graph-safety.ps1')

function Test-PortAvailable {
    param([int]$Port)

    try {
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Any, $Port)
        $listener.Start()
        $listener.Stop()
        return $true
    }
    catch {
        return $false
    }
}

function Resolve-FreePort {
    param(
        [int]$PreferredPort,
        [int]$FallbackStart
    )

    if (Test-PortAvailable -Port $PreferredPort) {
        return $PreferredPort
    }

    for ($port = $FallbackStart; $port -lt ($FallbackStart + 100); $port++) {
        if (Test-PortAvailable -Port $port) {
            return $port
        }
    }

    throw "No free port available near $FallbackStart"
}

function Get-DesktopRuntimeState {
    if (-not (Test-Path $runtimeStateFile)) {
        return $null
    }

    try {
        return Get-Content -Path $runtimeStateFile -Raw | ConvertFrom-Json
    }
    catch {
        return $null
    }
}

function Save-DesktopRuntimeState {
    param(
        [int]$ApiPort,
        [int]$GraphPort,
        [int]$BrowserPort
    )

    $runtimeDir = Split-Path -Parent $runtimeStateFile
    if (-not (Test-Path $runtimeDir)) {
        New-Item -ItemType Directory -Path $runtimeDir | Out-Null
    }

    @{
        apiPort = $ApiPort
        graphPort = $GraphPort
        browserPort = $BrowserPort
        updatedAt = (Get-Date).ToString('s')
    } | ConvertTo-Json | Set-Content -Path $runtimeStateFile -Encoding UTF8
}

function Resolve-CommandPort {
    param(
        [string]$EnvValue,
        [object]$SavedValue,
        [int]$DefaultValue
    )

    if ($EnvValue) {
        return [int]$EnvValue
    }

    if ($SavedValue) {
        return [int]$SavedValue
    }

    return $DefaultValue
}

function Invoke-Compose {
    param(
        [string[]]$ComposeArguments
    )

    $composeArgs = @('compose', '-f', $composeFile) + $ComposeArguments
    & docker @composeArgs
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose failed with exit code $LASTEXITCODE"
    }
}

Set-Location $repoRoot

if (-not (Test-Path $envFile) -and (Test-Path $envExample)) {
    Copy-Item $envExample $envFile
    Write-Host 'Created .env from .env.example'
}

$savedState = Get-DesktopRuntimeState
$existingServices = @(docker compose -f $composeFile ps -q 2>$null | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
$stackExists = $existingServices.Count -gt 0

if ($Command -in @('start', 'restart')) {
    Assert-CgaGraphPersistence -ComposeFile $composeFile
    $apiPreferred = if ($env:CGA_DESKTOP_API_PORT) { [int]$env:CGA_DESKTOP_API_PORT } elseif ($savedState -and $savedState.apiPort -and ($stackExists -or (Test-PortAvailable -Port ([int]$savedState.apiPort)))) { [int]$savedState.apiPort } else { Resolve-FreePort -PreferredPort 18001 -FallbackStart 18011 }
    $graphPreferred = if ($env:CGA_DESKTOP_FALKORDB_PORT) { [int]$env:CGA_DESKTOP_FALKORDB_PORT } elseif ($savedState -and $savedState.graphPort -and ($stackExists -or (Test-PortAvailable -Port ([int]$savedState.graphPort)))) { [int]$savedState.graphPort } else { Resolve-FreePort -PreferredPort 16381 -FallbackStart 16391 }
    $browserPreferred = if ($env:CGA_DESKTOP_BROWSER_PORT) { [int]$env:CGA_DESKTOP_BROWSER_PORT } elseif ($savedState -and $savedState.browserPort -and ($stackExists -or (Test-PortAvailable -Port ([int]$savedState.browserPort)))) { [int]$savedState.browserPort } else { Resolve-FreePort -PreferredPort 13001 -FallbackStart 13011 }
}
else {
    $apiPreferred = Resolve-CommandPort -EnvValue $env:CGA_DESKTOP_API_PORT -SavedValue $savedState.apiPort -DefaultValue 18001
    $graphPreferred = Resolve-CommandPort -EnvValue $env:CGA_DESKTOP_FALKORDB_PORT -SavedValue $savedState.graphPort -DefaultValue 16381
    $browserPreferred = Resolve-CommandPort -EnvValue $env:CGA_DESKTOP_BROWSER_PORT -SavedValue $savedState.browserPort -DefaultValue 13001
}

$env:CGA_DESKTOP_API_PORT = "$apiPreferred"
$env:CGA_DESKTOP_FALKORDB_PORT = "$graphPreferred"
$env:CGA_DESKTOP_BROWSER_PORT = "$browserPreferred"
$backupLocation = if ($env:CGA_BACKUP_DIR) { $env:CGA_BACKUP_DIR } else { Join-Path $repoRoot 'data\backups' }

switch ($Command) {
    'start' {
        if ($Detached) {
            Invoke-Compose @('up', '-d', '--build')
        }
        else {
            Invoke-Compose @('up', '--build')
        }
        Save-DesktopRuntimeState -ApiPort $apiPreferred -GraphPort $graphPreferred -BrowserPort $browserPreferred
        Write-Host "Admin UI: http://localhost:$apiPreferred/admin"
        Write-Host "FalkorDB Browser: http://localhost:$browserPreferred"
        Write-Host "Backups: $backupLocation"
        if ($OpenBrowser) {
            Start-Process "http://localhost:$apiPreferred/admin"
        }
    }
    'stop' {
        Invoke-Compose @('stop')
    }
    'restart' {
        Invoke-Compose @('up', '-d', '--build')
        Save-DesktopRuntimeState -ApiPort $apiPreferred -GraphPort $graphPreferred -BrowserPort $browserPreferred
        Write-Host "Admin UI: http://localhost:$apiPreferred/admin"
        Write-Host "Backups: $backupLocation"
        if ($OpenBrowser) {
            Start-Process "http://localhost:$apiPreferred/admin"
        }
    }
    'logs' {
        if ($Detached) {
            Invoke-Compose @('logs', '--tail=200')
        }
        else {
            Invoke-Compose @('logs', '-f', '--tail=200')
        }
    }
    'status' {
        Invoke-Compose @('ps')
        Write-Host "Admin UI: http://localhost:$apiPreferred/admin"
        Write-Host "FalkorDB Browser: http://localhost:$browserPreferred"
        Write-Host "Backups: $backupLocation"
    }
    'open' {
        Start-Process "http://localhost:$apiPreferred/admin"
    }
    'config' {
        Invoke-Compose @('config')
    }
}