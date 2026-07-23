param(
    [Parameter(Mandatory = $false)]
    [string]$Version,

    [Parameter(Mandatory = $false)]
    [string]$ReleaseRoot = (Join-Path $PSScriptRoot 'dist\releases'),

    [Parameter(Mandatory = $false)]
    [switch]$SkipImageBuild
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot '..\..')
$mainPy = Join-Path $repoRoot 'src\backend\main.py'
$portableBuilder = Join-Path $PSScriptRoot 'build-portable-bundle.ps1'

if (-not $Version) {
    $mainPyContent = Get-Content -Path $mainPy -Raw
    $match = [regex]::Match($mainPyContent, 'APP_VERSION\s*=\s*"([^"]+)"')
    if (-not $match.Success) {
        throw "Unable to determine APP_VERSION from $mainPy"
    }
    $Version = $match.Groups[1].Value
}

$bundleName = "CGA-Docker-Desktop-$Version"
$versionedFolder = Join-Path $ReleaseRoot $bundleName
$zipPath = Join-Path $ReleaseRoot "$bundleName.zip"
$imageName = 'cga-desktop-portable-cga'
$imageTag = "$imageName`:$Version"
$localImageTag = "$imageName`:local"
$imageTarPath = Join-Path $versionedFolder 'cga-desktop-api-image.tar'

if (-not (Test-Path $ReleaseRoot)) {
    New-Item -ItemType Directory -Path $ReleaseRoot | Out-Null
}

if (Test-Path $versionedFolder) {
    Remove-Item -Path $versionedFolder -Recurse -Force
}

if (Test-Path $zipPath) {
    Remove-Item -Path $zipPath -Force
}

& $portableBuilder -OutputFolder $versionedFolder

if (-not $SkipImageBuild) {
    Write-Host "Building prebuilt CGA API image: $imageTag"
    & docker build --file (Join-Path $versionedFolder 'Dockerfile.dev') --tag $imageTag --tag $localImageTag $versionedFolder
    if ($LASTEXITCODE -ne 0) {
        throw "Docker image build failed with exit code $LASTEXITCODE"
    }

    Write-Host "Saving prebuilt CGA API image to: $imageTarPath"
    & docker image save -o $imageTarPath $localImageTag $imageTag
    if ($LASTEXITCODE -ne 0) {
        throw "Docker image save failed with exit code $LASTEXITCODE"
    }
}

$releaseNotes = @"
CGA Docker Desktop Release Package
Version: $Version
Built: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')

Contents:
- Portable Docker Desktop bundle directory
- Windows one-click launchers
- Local repos drop folder
- Project author and creator attribution: Nate Scott
- Open-source and notice files: LICENSE, NOTICE.md, OPEN_SOURCE.md, THIRD_PARTY_NOTICES.md, DISCLAIMER.md, SECURITY.md, CONTRIBUTING.md, CODE_OF_CONDUCT.md
- Fresh runtime default: no bundled local projects, repository index data, database volumes, backups, or sample/demo project data
- Prebuilt CGA API image tar: $(if ($SkipImageBuild) { 'not included' } else { 'cga-desktop-api-image.tar' })
"@
Set-Content -Path (Join-Path $versionedFolder 'RELEASE.txt') -Value $releaseNotes -Encoding UTF8

Compress-Archive -Path $versionedFolder -DestinationPath $zipPath -Force

Write-Host "Release folder created at: $versionedFolder"
Write-Host "Release zip created at: $zipPath"