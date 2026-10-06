$ErrorActionPreference = 'Stop'
$config = Join-Path $env:USERPROFILE '.cga\relay.env'
$executable = Join-Path $env:USERPROFILE '.cga\bin\cga-relay.exe'
$envFile = Join-Path $PSScriptRoot '..\..\.env'

foreach ($path in @($config, $executable, $envFile)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required CGA Relay file is missing: $path"
    }
}
$tokenLine = Get-Content -LiteralPath $envFile |
    Where-Object { $_ -match '^\s*CONTEXTGRAPH_MCP_TOKEN\s*=' } |
    Select-Object -First 1
if (-not $tokenLine) {
    throw 'CONTEXTGRAPH_MCP_TOKEN is not configured in the local .env file.'
}
$env:CONTEXTGRAPH_MCP_TOKEN = ($tokenLine -split '=', 2)[1].Trim().Trim('"').Trim("'")
if ([string]::IsNullOrWhiteSpace($env:CONTEXTGRAPH_MCP_TOKEN)) {
    throw 'CONTEXTGRAPH_MCP_TOKEN is empty.'
}
& $executable tray --config $config
if ($LASTEXITCODE -ne 0) {
    throw "CGA Relay exited with code $LASTEXITCODE."
}
