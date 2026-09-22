param(
    [Parameter(Mandatory = $false, Position = 0)]
    [ValidateSet('start', 'stop', 'restart', 'logs', 'status', 'open', 'config')]
    [string]$Command = 'start',

    [Parameter(Mandatory = $false)]
    [bool]$Detached = $true,

    [Parameter(Mandatory = $false)]
    [bool]$OpenBrowser = $false,

    [Parameter(Mandatory = $false)]
    [bool]$WaitForReady = $false,

    [Parameter(Mandatory = $false)]
    [switch]$BuildFromSource
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$bundleRoot = [string](Resolve-Path $PSScriptRoot)
$composeFile = Join-Path $bundleRoot 'docker-compose.yml'
$envExample = Join-Path $bundleRoot '.env.example'
$envFile = Join-Path $bundleRoot '.env'
$runtimeStateFile = Join-Path $bundleRoot 'tmp\cga-desktop-runtime.json'
$prebuiltImageTar = Join-Path $bundleRoot 'cga-desktop-api-image.tar'
$prebuiltImageStateFile = Join-Path $bundleRoot 'tmp\cga-desktop-image.json'
$prebuiltImageName = if ($env:CGA_DESKTOP_IMAGE) { $env:CGA_DESKTOP_IMAGE } else { 'cga-desktop-portable-cga:local' }
$graphSafetyScript = Join-Path $bundleRoot 'src\scripts\desktop-graph-safety.ps1'
if (-not (Test-Path -LiteralPath $graphSafetyScript)) {
    $graphSafetyScript = Join-Path $PSScriptRoot '..\..\src\scripts\desktop-graph-safety.ps1'
}
. $graphSafetyScript
$script:PublishedDockerPorts = $null
$script:OriginalDesktopEnv = @{
    CGA_DESKTOP_API_PORT = $env:CGA_DESKTOP_API_PORT
    CGA_DESKTOP_FALKORDB_PORT = $env:CGA_DESKTOP_FALKORDB_PORT
    CGA_DESKTOP_BROWSER_PORT = $env:CGA_DESKTOP_BROWSER_PORT
}

function Test-Truthy {
    param([string]$Value)
    return $Value -in @('1', 'true', 'yes', 'on')
}

function Assert-DockerAvailable {
    $docker = Get-Command docker -ErrorAction SilentlyContinue
    if (-not $docker) {
        throw 'Docker Desktop is required. Install and start Docker Desktop, then run this launcher again.'
    }

    & docker version --format '{{.Server.Version}}' 1>$null 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw 'Docker Desktop is not running or is not reachable. Start Docker Desktop, wait until it is ready, then run this launcher again.'
    }
}

function Restore-DesktopLauncherEnv {
    foreach ($name in $script:OriginalDesktopEnv.Keys) {
        $value = $script:OriginalDesktopEnv[$name]
        if ($null -eq $value) {
            Remove-Item -Path "Env:$name" -ErrorAction SilentlyContinue
        }
        else {
            Set-Item -Path "Env:$name" -Value $value
        }
    }
}

function Get-DockerPublishedPorts {
    if ($null -ne $script:PublishedDockerPorts) {
        return $script:PublishedDockerPorts
    }

    $publishedPorts = @{}
    try {
        $publishedPortLines = @(docker ps --format '{{.Ports}}' 2>$null)
        foreach ($line in $publishedPortLines) {
            foreach ($match in [regex]::Matches($line, ':(\d+)->')) {
                $publishedPorts[[int]$match.Groups[1].Value] = $true
            }
        }
    }
    catch {
    }

    $script:PublishedDockerPorts = $publishedPorts
    return $script:PublishedDockerPorts
}

function Test-PortAvailable {
    param([int]$Port)

    $dockerPublishedPorts = Get-DockerPublishedPorts
    if ($dockerPublishedPorts.ContainsKey($Port)) {
        return $false
    }

    $tcpConnectionCommand = Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue
    if ($tcpConnectionCommand) {
        $listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
        if ($listeners.Count -gt 0) {
            return $false
        }
    }

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

function Resolve-StartPort {
    param(
        [string]$EnvValue,
        [object]$SavedValue,
        [int]$DefaultPort,
        [int]$FallbackStart,
        [bool]$StackExists
    )

    if ($EnvValue) {
        $requestedPort = [int]$EnvValue
        if ($StackExists -or (Test-PortAvailable -Port $requestedPort)) {
            return $requestedPort
        }
        Write-Warning "Requested port $requestedPort is unavailable. Selecting a free fallback port near $FallbackStart."
        return Resolve-FreePort -PreferredPort $DefaultPort -FallbackStart $FallbackStart
    }

    if ($SavedValue) {
        $savedPort = [int]$SavedValue
        if ($StackExists -or (Test-PortAvailable -Port $savedPort)) {
            return $savedPort
        }
    }

    return Resolve-FreePort -PreferredPort $DefaultPort -FallbackStart $FallbackStart
}

function Get-JsonFileState {
    param([string]$Path)

    if (-not (Test-Path $Path)) {
        return $null
    }

    try {
        return Get-Content -Path $Path -Raw | ConvertFrom-Json
    }
    catch {
        return $null
    }
}

function Get-StateValue {
    param(
        [object]$State,
        [string]$Name
    )

    if ($null -eq $State) {
        return $null
    }

    $property = $State.PSObject.Properties | Where-Object { $_.Name -eq $Name } | Select-Object -First 1
    if ($null -eq $property) {
        return $null
    }

    return $property.Value
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

function Save-PrebuiltImageState {
    param(
        [string]$ImageName,
        [string]$TarFingerprint
    )

    $runtimeDir = Split-Path -Parent $prebuiltImageStateFile
    if (-not (Test-Path $runtimeDir)) {
        New-Item -ItemType Directory -Path $runtimeDir | Out-Null
    }

    @{
        imageName = $ImageName
        tarFingerprint = $TarFingerprint
        loadedAt = (Get-Date).ToString('s')
    } | ConvertTo-Json | Set-Content -Path $prebuiltImageStateFile -Encoding UTF8
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
    param([string[]]$ComposeArguments)

    $composeArgs = @('compose', '-f', $composeFile) + $ComposeArguments
    & docker @composeArgs
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose failed with exit code $LASTEXITCODE"
    }
}

function Test-DockerImageExists {
    param([string]$ImageName)

    & docker image inspect $ImageName 1>$null 2>$null
    return ($LASTEXITCODE -eq 0)
}

function Import-PrebuiltImage {
    if ((Test-Truthy -Value $env:CGA_BUILD_FROM_SOURCE) -or $BuildFromSource) {
        Write-Host 'Startup mode requested: source build fallback'
        return $false
    }

    if (-not (Test-Path $prebuiltImageTar)) {
        return $false
    }

    $tarInfo = Get-Item -Path $prebuiltImageTar
    $tarFingerprint = "$($tarInfo.Length):$($tarInfo.LastWriteTimeUtc.Ticks)"
    $imageState = Get-JsonFileState -Path $prebuiltImageStateFile
    $loadedImageName = Get-StateValue -State $imageState -Name 'imageName'
    $loadedFingerprint = Get-StateValue -State $imageState -Name 'tarFingerprint'

    if ($loadedFingerprint -eq $tarFingerprint -and $loadedImageName -eq $prebuiltImageName -and (Test-DockerImageExists -ImageName $prebuiltImageName)) {
        Write-Host "Prebuilt CGA API image already loaded: $prebuiltImageName"
        return $true
    }

    Write-Host "Loading prebuilt CGA API image from $prebuiltImageTar"
    & docker image load -i $prebuiltImageTar
    if ($LASTEXITCODE -ne 0) {
        throw "docker image load failed with exit code $LASTEXITCODE"
    }

    Save-PrebuiltImageState -ImageName $prebuiltImageName -TarFingerprint $tarFingerprint
    return $true
}

function Invoke-ComposeUp {
    param(
        [bool]$UseBuild,
        [bool]$DetachedMode
    )

    $arguments = @('up')
    if ($DetachedMode) {
        $arguments += '-d'
    }
    if ($UseBuild) {
        $arguments += '--build'
    }
    Invoke-Compose $arguments
}

function Wait-AdminHealth {
    param(
        [int]$ApiPort,
        [int]$TimeoutSeconds = 120
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $healthUrl = "http://localhost:$ApiPort/health"
    Write-Host "Waiting for CGA health at $healthUrl"
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 4
            if ($response.status -eq 'ok') {
                Write-Host "CGA is ready: $healthUrl"
                return $true
            }
        }
        catch {
        }
        Start-Sleep -Seconds 2
    }

    Write-Warning "CGA did not report healthy within $TimeoutSeconds seconds. Use logs-cga-desktop.cmd to inspect startup logs."
    return $false
}

try {
Set-Location $bundleRoot
Assert-DockerAvailable

if (-not (Test-Path $envFile) -and (Test-Path $envExample)) {
    Copy-Item $envExample $envFile
    Write-Host 'Created .env from .env.example'
}

$savedState = Get-JsonFileState -Path $runtimeStateFile
$savedApiPort = Get-StateValue -State $savedState -Name 'apiPort'
$savedGraphPort = Get-StateValue -State $savedState -Name 'graphPort'
$savedBrowserPort = Get-StateValue -State $savedState -Name 'browserPort'
$existingServices = @(docker compose -f $composeFile ps -q 2>$null | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
$stackExists = $existingServices.Count -gt 0

if ($Command -in @('start', 'restart')) {
    Assert-CgaGraphPersistence -ComposeFile $composeFile
    $apiPreferred = Resolve-StartPort -EnvValue $env:CGA_DESKTOP_API_PORT -SavedValue $savedApiPort -DefaultPort 18001 -FallbackStart 18011 -StackExists $stackExists
    $graphPreferred = Resolve-StartPort -EnvValue $env:CGA_DESKTOP_FALKORDB_PORT -SavedValue $savedGraphPort -DefaultPort 16381 -FallbackStart 16391 -StackExists $stackExists
    $browserPreferred = Resolve-StartPort -EnvValue $env:CGA_DESKTOP_BROWSER_PORT -SavedValue $savedBrowserPort -DefaultPort 13001 -FallbackStart 13011 -StackExists $stackExists
}
else {
    $apiPreferred = Resolve-CommandPort -EnvValue $env:CGA_DESKTOP_API_PORT -SavedValue $savedApiPort -DefaultValue 18001
    $graphPreferred = Resolve-CommandPort -EnvValue $env:CGA_DESKTOP_FALKORDB_PORT -SavedValue $savedGraphPort -DefaultValue 16381
    $browserPreferred = Resolve-CommandPort -EnvValue $env:CGA_DESKTOP_BROWSER_PORT -SavedValue $savedBrowserPort -DefaultValue 13001
}

$env:CGA_DESKTOP_API_PORT = "$apiPreferred"
$env:CGA_DESKTOP_FALKORDB_PORT = "$graphPreferred"
$env:CGA_DESKTOP_BROWSER_PORT = "$browserPreferred"
$backupLocation = if ($env:CGA_BACKUP_DIR) { $env:CGA_BACKUP_DIR } else { Join-Path $bundleRoot 'data\backups' }

switch ($Command) {
    'start' {
        $hasPrebuiltImage = Import-PrebuiltImage
        Invoke-ComposeUp -UseBuild:(-not $hasPrebuiltImage) -DetachedMode:$Detached
        Save-DesktopRuntimeState -ApiPort $apiPreferred -GraphPort $graphPreferred -BrowserPort $browserPreferred
        Write-Host "Admin UI: http://localhost:$apiPreferred/admin"
        Write-Host "MCP URL: http://localhost:$apiPreferred/mcp"
        Write-Host "FalkorDB Browser: http://localhost:$browserPreferred"
        Write-Host "Backups: $backupLocation"
        if ($hasPrebuiltImage) {
            Write-Host 'Startup mode: prebuilt image'
        }
        else {
            Write-Host 'Startup mode: source build fallback'
        }
        if ($OpenBrowser -or $WaitForReady) {
            Wait-AdminHealth -ApiPort $apiPreferred | Out-Null
        }
        if ($OpenBrowser) {
            Start-Process "http://localhost:$apiPreferred/admin"
        }
    }
    'stop' {
        Invoke-Compose @('stop')
    }
    'restart' {
        $hasPrebuiltImage = Import-PrebuiltImage
        Invoke-ComposeUp -UseBuild:(-not $hasPrebuiltImage) -DetachedMode:$true
        Save-DesktopRuntimeState -ApiPort $apiPreferred -GraphPort $graphPreferred -BrowserPort $browserPreferred
        Write-Host "Admin UI: http://localhost:$apiPreferred/admin"
        Write-Host "Backups: $backupLocation"
        if ($OpenBrowser -or $WaitForReady) {
            Wait-AdminHealth -ApiPort $apiPreferred | Out-Null
        }
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
        Write-Host "Prebuilt image: $(if (Test-Path $prebuiltImageTar) { $prebuiltImageTar } else { 'not bundled' })"
    }
    'open' {
        Start-Process "http://localhost:$apiPreferred/admin"
    }
    'config' {
        Invoke-Compose @('config')
    }
}
}
finally {
    Restore-DesktopLauncherEnv
}
