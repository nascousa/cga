Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot '..\release-process.ps1')

$buildScriptPath = Join-Path $PSScriptRoot '..\build-secure-release.ps1'
$buildScript = Get-Content -LiteralPath $buildScriptPath -Raw
$verificationOffset = $buildScript.IndexOf("verify-release-binary.ps1")
$replacementOffset = $buildScript.IndexOf('Install-CgaRelayCandidate')
if ($verificationOffset -lt 0 -or $replacementOffset -le $verificationOffset) {
    throw 'The secure release script must verify the candidate before replacing running Relay processes.'
}
foreach ($requiredFragment in @('Get-CgaRelayRunningProcesses', '-RunningProcesses $runningProcesses')) {
    if (-not $buildScript.Contains($requiredFragment)) {
        throw "The secure release script is missing live replacement wiring: $requiredFragment"
    }
}

$testRoot = Join-Path ([System.IO.Path]::GetTempPath()) "cga-relay-release-process-$([guid]::NewGuid().ToString('N'))"
$installedDirectory = Join-Path $testRoot 'installed'
$candidateDirectory = Join-Path $testRoot 'candidate'
$installedPath = Join-Path $installedDirectory 'cga-relay.exe'
$candidatePath = Join-Path $candidateDirectory 'cga-relay.exe'
$process = $null
$lockProcess = $null
$rollbackProcess = $null

try {
    New-Item -ItemType Directory -Path $installedDirectory, $candidateDirectory -Force | Out-Null
    Copy-Item -LiteralPath $env:ComSpec -Destination $installedPath
    Copy-Item -LiteralPath (Join-Path $env:SystemRoot 'System32\whoami.exe') -Destination $candidatePath

    $process = Start-Process `
        -FilePath $installedPath `
        -ArgumentList '/d /c ping -t 127.0.0.1 > nul' `
        -WindowStyle Hidden `
        -PassThru
    $runningProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $($process.Id)"
    if (-not $runningProcess) {
        throw 'The isolated release test process did not start.'
    }

    $result = Install-CgaRelayCandidate `
        -CandidatePath $candidatePath `
        -RunningProcesses @($runningProcess) `
        -Restart:$false

    if (-not $process.WaitForExit(5000)) {
        throw 'The selected running Relay process was not stopped.'
    }
    $installedHash = (Get-FileHash -LiteralPath $installedPath -Algorithm SHA256).Hash
    $candidateHash = (Get-FileHash -LiteralPath $candidatePath -Algorithm SHA256).Hash
    if ($installedHash -ne $candidateHash) {
        throw 'The verified candidate did not replace the running executable.'
    }
    if ($result.StoppedCount -ne 1 -or $result.ReplacedCount -ne 1 -or $result.RestartedCount -ne 0) {
        throw "Unexpected replacement result: $($result | ConvertTo-Json -Compress)"
    }
    $replacementResidue = @(
        Get-ChildItem -LiteralPath $installedDirectory -File |
            Where-Object Name -Like '*.cga-release-*'
    )
    if ($replacementResidue.Count -ne 0) {
        throw "Replacement residue was not removed: $($replacementResidue.FullName -join ', ')"
    }

    Copy-Item -LiteralPath $env:ComSpec -Destination $installedPath -Force
    $originalHash = (Get-FileHash -LiteralPath $installedPath -Algorithm SHA256).Hash
    $process = Start-Process `
        -FilePath $installedPath `
        -ArgumentList '/d /c ping -t 127.0.0.1 > nul' `
        -WindowStyle Hidden `
        -PassThru
    $cimProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $($process.Id)"
    if (-not $cimProcess) {
        throw 'The rollback test process did not start.'
    }
    $runningProcess = [pscustomobject]@{
        ProcessId = $cimProcess.ProcessId
        ExecutablePath = $cimProcess.ExecutablePath
        CommandLine = "`"$installedPath`" tray"
    }

    $rollbackError = $null
    try {
        Install-CgaRelayCandidate `
            -CandidatePath $candidatePath `
            -RunningProcesses @($runningProcess) `
            -Restart:$true | Out-Null
    } catch {
        $rollbackError = $_.Exception.Message
    }
    if ([string]::IsNullOrWhiteSpace($rollbackError) -or
        -not $rollbackError.Contains('The original binary was restored and restarted')) {
        throw "The failed candidate restart did not report a successful rollback: $rollbackError"
    }
    if ((Get-FileHash -LiteralPath $installedPath -Algorithm SHA256).Hash -ne $originalHash) {
        throw 'The failed candidate restart did not restore the original executable.'
    }
    $rollbackProcesses = @(
        Get-CimInstance Win32_Process -Filter "Name = 'cga-relay.exe'" |
            Where-Object { $_.ExecutablePath -and
                [System.IO.Path]::GetFullPath($_.ExecutablePath) -eq [System.IO.Path]::GetFullPath($installedPath) }
    )
    if ($rollbackProcesses.Count -ne 1) {
        throw "Expected one restored Relay process, found $($rollbackProcesses.Count)."
    }
    $rollbackProcess = Get-Process -Id $rollbackProcesses[0].ProcessId
    $rollbackResidue = @(
        Get-ChildItem -LiteralPath $installedDirectory -File |
            Where-Object Name -Like '*.cga-release-*'
    )
    if ($rollbackResidue.Count -ne 0) {
        throw "Rollback residue was not removed: $($rollbackResidue.FullName -join ', ')"
    }

    $lockPath = Join-Path $testRoot 'transient-lock.bak'
    $lockReadyPath = Join-Path $testRoot 'transient-lock.ready'
    [System.IO.File]::WriteAllText($lockPath, 'locked')
    $escapedLockPath = $lockPath.Replace("'", "''")
    $escapedReadyPath = $lockReadyPath.Replace("'", "''")
    $lockScript = @"
`$stream = [System.IO.File]::Open('$escapedLockPath', 'Open', 'ReadWrite', 'None')
try {
    [System.IO.File]::WriteAllText('$escapedReadyPath', 'ready')
    [System.Threading.Thread]::Sleep(500)
} finally {
    `$stream.Dispose()
}
"@
    $encodedLockScript = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($lockScript))
    $lockProcess = Start-Process `
        -FilePath (Get-Process -Id $PID).Path `
        -ArgumentList '-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand', $encodedLockScript `
        -WindowStyle Hidden `
        -PassThru
    for ($attempt = 1; $attempt -le 250 -and -not (Test-Path -LiteralPath $lockReadyPath); $attempt++) {
        [System.Threading.Thread]::Sleep(20)
    }
    if (-not (Test-Path -LiteralPath $lockReadyPath) -or $lockProcess.HasExited) {
        throw 'The transient lock helper did not acquire its exclusive file lock.'
    }

    Remove-CgaRelayReleaseFile -Path $lockPath
    if (Test-Path -LiteralPath $lockPath) {
        throw 'The release cleanup retry did not remove the transiently locked file.'
    }
    if (-not $lockProcess.WaitForExit(5000)) {
        throw 'The transient lock helper did not exit.'
    }

    Write-Output 'release-process.Tests.ps1: PASS'
} finally {
    if ($process -and -not $process.HasExited) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    }
    if ($lockProcess -and -not $lockProcess.HasExited) {
        Stop-Process -Id $lockProcess.Id -Force -ErrorAction SilentlyContinue
    }
    if ($rollbackProcess -and -not $rollbackProcess.HasExited) {
        Stop-Process -Id $rollbackProcess.Id -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $testRoot -Recurse -Force -ErrorAction SilentlyContinue
}