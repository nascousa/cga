[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$TargetDir
)

$ErrorActionPreference = 'Stop'
$SourceDir = (Resolve-Path (Join-Path $PSScriptRoot '..\..\adc-template')).Path
$Destination = [System.IO.Path]::GetFullPath($TargetDir)

# Generate from the maintained template, never a second embedded rule catalog.
if (Test-Path -LiteralPath $Destination) {
    throw "Target already exists; choose a new directory to preserve existing templates and releases: $Destination"
}
if ($Destination.StartsWith($SourceDir + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Target must not be inside the source template.'
}

New-Item -ItemType Directory -Path $Destination | Out-Null
Get-ChildItem -LiteralPath $SourceDir -Force | Copy-Item -Destination $Destination -Recurse -Force
Write-Host "Success! ADC template copied from $SourceDir to $Destination."
