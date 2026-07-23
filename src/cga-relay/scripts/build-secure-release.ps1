param(
    [Parameter(Mandatory = $false)]
    [string]$Version,

    [Parameter(Mandatory = $false)]
    [string]$OutputDirectory = (Join-Path $PSScriptRoot '..\dist'),

    [Parameter(Mandatory = $false)]
    [switch]$RequireSignature
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$crateRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$manifestPath = Join-Path $crateRoot 'Cargo.toml'
$manifest = Get-Content -LiteralPath $manifestPath -Raw
$versionMatch = [regex]::Match($manifest, '(?m)^version\s*=\s*"([^"]+)"')
if (-not $versionMatch.Success) {
    throw "Unable to read the relay version from $manifestPath"
}
$manifestVersion = $versionMatch.Groups[1].Value
if (-not $Version) {
    $Version = $manifestVersion
}
if ($Version -ne $manifestVersion) {
    throw "Requested relay version $Version does not match Cargo.toml version $manifestVersion."
}

$signingCertificateBase64 = $env:CGA_RELAY_SIGNING_CERT_BASE64
$signingCertificatePassword = $env:CGA_RELAY_SIGNING_CERT_PASSWORD
Remove-Item Env:CGA_RELAY_SIGNING_CERT_BASE64 -ErrorAction SilentlyContinue
Remove-Item Env:CGA_RELAY_SIGNING_CERT_PASSWORD -ErrorAction SilentlyContinue
$hasCertificate = -not [string]::IsNullOrWhiteSpace($signingCertificateBase64)
$hasPassword = -not [string]::IsNullOrWhiteSpace($signingCertificatePassword)
if ($RequireSignature -and (-not $hasCertificate -or -not $hasPassword)) {
    throw 'Signed release requires CGA_RELAY_SIGNING_CERT_BASE64 and CGA_RELAY_SIGNING_CERT_PASSWORD.'
}
if ($hasCertificate -xor $hasPassword) {
    throw 'Relay signing certificate and password must be configured together.'
}

$outputRoot = [System.IO.Path]::GetFullPath($OutputDirectory)
$targetRoot = Join-Path $crateRoot 'target\secure-release'
$packageRoot = Join-Path $targetRoot "package-$Version"
$builtExe = Join-Path $targetRoot 'x86_64-pc-windows-msvc\release\cga-relay.exe'
$releaseExe = Join-Path $outputRoot 'cga-relay.exe'
$zipName = "cga-relay-$Version-windows-x64.zip"
$zipPath = Join-Path $outputRoot $zipName
$exeChecksumPath = Join-Path $outputRoot 'cga-relay.exe.sha256'
$zipChecksumPath = "$zipPath.sha256"

New-Item -ItemType Directory -Path $outputRoot -Force | Out-Null
Remove-Item -LiteralPath $packageRoot -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $packageRoot -Force | Out-Null
foreach ($path in @($releaseExe, $zipPath, $exeChecksumPath, $zipChecksumPath)) {
    Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
}

Push-Location $crateRoot
try {
    & cargo build --locked --release --target x86_64-pc-windows-msvc --target-dir $targetRoot
    if ($LASTEXITCODE -ne 0) {
        throw "Hardened CGA-Relay build failed with exit code $LASTEXITCODE."
    }
} finally {
    Pop-Location
}
Copy-Item -LiteralPath $builtExe -Destination $releaseExe -Force

if ($hasCertificate) {
    $signTool = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if (-not $signTool) {
        $kitsRoot = Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10\bin'
        $signTool = Get-ChildItem -Path $kitsRoot -Filter signtool.exe -Recurse -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match '\\x64\\signtool\.exe$' } |
            Sort-Object FullName -Descending |
            Select-Object -First 1
    }
    if (-not $signTool) {
        throw 'signtool.exe was not found; install the Windows SDK signing tools.'
    }
    $signToolPath = if ($signTool -is [System.IO.FileInfo]) {
        $signTool.FullName
    } else {
        $signTool.Source
    }

    $certificatePath = Join-Path ([System.IO.Path]::GetTempPath()) "cga-relay-signing-$([guid]::NewGuid().ToString('N')).pfx"
    $existingThumbprints = @{}
    $importedCertificates = @()
    try {
        [System.IO.File]::WriteAllBytes(
            $certificatePath,
            [Convert]::FromBase64String($signingCertificateBase64)
        )
        Get-ChildItem -Path Cert:\CurrentUser\My | ForEach-Object {
            $existingThumbprints[$_.Thumbprint] = $true
        }
        $securePassword = ConvertTo-SecureString $signingCertificatePassword -AsPlainText -Force
        $importedCertificates = @(
            Import-PfxCertificate `
                -FilePath $certificatePath `
                -CertStoreLocation Cert:\CurrentUser\My `
                -Password $securePassword `
                -Exportable:$false
        )
        $signingCertificate = $importedCertificates |
            Where-Object { $_.HasPrivateKey } |
            Select-Object -First 1
        if (-not $signingCertificate) {
            throw 'The relay signing PFX does not contain a certificate with a private key.'
        }
        & $signToolPath sign /fd SHA256 /sha1 $signingCertificate.Thumbprint /s My /tr http://timestamp.digicert.com /td SHA256 $releaseExe
        if ($LASTEXITCODE -ne 0) {
            throw "CGA-Relay Authenticode signing failed with exit code $LASTEXITCODE."
        }
    } finally {
        foreach ($certificate in $importedCertificates) {
            if (-not $existingThumbprints.ContainsKey($certificate.Thumbprint)) {
                Remove-Item -LiteralPath "Cert:\CurrentUser\My\$($certificate.Thumbprint)" -Force -ErrorAction SilentlyContinue
            }
        }
        $securePassword = $null
        $signingCertificateBase64 = $null
        $signingCertificatePassword = $null
        Remove-Item -LiteralPath $certificatePath -Force -ErrorAction SilentlyContinue
    }
}

$verifyArgs = @{
    BinaryPath   = $releaseExe
    ForbiddenText = @($crateRoot)
}
if ($RequireSignature) {
    $verifyArgs.RequireSignature = $true
}
& (Join-Path $PSScriptRoot 'verify-release-binary.ps1') @verifyArgs

Copy-Item -LiteralPath $releaseExe -Destination (Join-Path $packageRoot 'cga-relay.exe') -Force
$packageReadme = @"
CGA-Relay $Version for Windows x64

This release is built with static CRT linking, fat LTO, stripped symbols, panic abort,
Control Flow Guard, ASLR, high-entropy ASLR, DEP/NX, and CET compatibility.
Windows account sessions are protected for the current user with DPAPI.

Verify SHA-256 before execution. Code hardening raises reverse-engineering cost but
cannot make a native executable impossible to analyze.
"@
Set-Content -LiteralPath (Join-Path $packageRoot 'README.txt') -Value $packageReadme -Encoding ASCII

$exeHash = (Get-FileHash -LiteralPath $releaseExe -Algorithm SHA256).Hash.ToLowerInvariant()
"$exeHash  cga-relay.exe" | Set-Content -LiteralPath $exeChecksumPath -Encoding ASCII
Copy-Item -LiteralPath $exeChecksumPath -Destination (Join-Path $packageRoot 'cga-relay.exe.sha256') -Force
Compress-Archive -Path (Join-Path $packageRoot '*') -DestinationPath $zipPath -CompressionLevel Optimal -Force
$zipHash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
"$zipHash  $zipName" | Set-Content -LiteralPath $zipChecksumPath -Encoding ASCII

[pscustomobject]@{
    Version = $Version
    Executable = $releaseExe
    Archive = $zipPath
    ExecutableSHA256 = $exeHash
    ArchiveSHA256 = $zipHash
    SignatureRequired = [bool]$RequireSignature
} | ConvertTo-Json -Depth 3