$ErrorActionPreference = 'Stop'
$config = Join-Path $env:USERPROFILE '.cga\relay.env'
$executable = Join-Path $env:USERPROFILE '.cga\bin\cga-relay.exe'
$envFile = Join-Path $PSScriptRoot '..\..\.env'

foreach ($path in @($config, $executable)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required CGA Relay file is missing: $path"
    }
}
if (Test-Path -LiteralPath $envFile -PathType Leaf) {
    $environmentLines = @(Get-Content -LiteralPath $envFile)
    foreach ($line in Get-Content -LiteralPath $config) {
        if ($line -match '^\s*(API_KEY_ENV|ACCOUNT_TOKEN_ENV)\s*=\s*([A-Za-z_][A-Za-z0-9_]*)\s*$') {
            $name = $Matches[2]
            if (-not [string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name, 'Process'))) {
                continue
            }
            $pattern = '^\s*' + [regex]::Escape($name) + '\s*='
            $valueLine = $environmentLines | Where-Object { $_ -match $pattern } | Select-Object -First 1
            if ($valueLine) {
                $value = ($valueLine -split '=', 2)[1].Trim().Trim('"').Trim("'")
                if (-not [string]::IsNullOrWhiteSpace($value)) {
                    [Environment]::SetEnvironmentVariable($name, $value, 'Process')
                }
            }
        }
    }
}
& $executable tray --config $config
if ($LASTEXITCODE -ne 0) {
    $exitCode = $LASTEXITCODE
    Write-Error "CGA Relay exited with code $exitCode." -ErrorAction Continue
    exit $exitCode
}
