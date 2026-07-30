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
$secondaryInstalledDirectory = Join-Path $testRoot 'installed-secondary'
$candidateDirectory = Join-Path $testRoot 'candidate'
$installedPath = Join-Path $installedDirectory 'cga-relay.exe'
$secondaryInstalledPath = Join-Path $secondaryInstalledDirectory 'cga-relay.exe'
$candidatePath = Join-Path $candidateDirectory 'cga-relay.exe'
$process = $null
$secondaryProcess = $null
$lockProcess = $null
$rollbackProcess = $null

try {
    New-Item `
        -ItemType Directory `
        -Path $installedDirectory, $secondaryInstalledDirectory, $candidateDirectory `
        -Force | Out-Null
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
    Copy-Item -LiteralPath $env:ComSpec -Destination $secondaryInstalledPath -Force
    $originalHash = (Get-FileHash -LiteralPath $installedPath -Algorithm SHA256).Hash
    $secondaryOriginalHash = (Get-FileHash -LiteralPath $secondaryInstalledPath -Algorithm SHA256).Hash
    $process = Start-Process `
        -FilePath $installedPath `
        -ArgumentList '/d /c ping -t 127.0.0.1 > nul' `
        -WindowStyle Hidden `
        -PassThru
    $secondaryProcess = Start-Process `
        -FilePath $secondaryInstalledPath `
        -ArgumentList '/d /c ping -t 127.0.0.1 > nul' `
        -WindowStyle Hidden `
        -PassThru
    $cimProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $($process.Id)"
    $secondaryCimProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $($secondaryProcess.Id)"
    if (-not $cimProcess -or -not $secondaryCimProcess) {
        throw 'The rollback test processes did not start.'
    }
    $expectedTrayArguments = 'tray --config "C:\Relay Config\agent.env" --label "alpha beta"'
    $runningProcesses = @(
        [pscustomobject]@{
            ProcessId = $cimProcess.ProcessId
            ExecutablePath = $cimProcess.ExecutablePath
            CommandLine = "`"$installedPath`" $expectedTrayArguments"
        }
        [pscustomobject]@{
            ProcessId = $secondaryCimProcess.ProcessId
            ExecutablePath = $secondaryCimProcess.ExecutablePath
            CommandLine = "`"$secondaryInstalledPath`" doctor"
        }
    )

    $rollbackError = $null
    try {
        Install-CgaRelayCandidate `
            -CandidatePath $candidatePath `
            -RunningProcesses $runningProcesses `
            -Restart:$true | Out-Null
    } catch {
        $rollbackError = $_.Exception.Message
    }
    if ([string]::IsNullOrWhiteSpace($rollbackError) -or
        -not $rollbackError.Contains('The original binaries were restored and the tray restarted')) {
        throw "The failed candidate restart did not report a successful rollback: $rollbackError"
    }
    if (-not $process.WaitForExit(5000) -or -not $secondaryProcess.WaitForExit(5000)) {
        throw 'The original Relay processes were not both stopped during replacement.'
    }
    if ((Get-FileHash -LiteralPath $installedPath -Algorithm SHA256).Hash -ne $originalHash) {
        throw 'The failed candidate restart did not restore the primary executable.'
    }
    if ((Get-FileHash -LiteralPath $secondaryInstalledPath -Algorithm SHA256).Hash -ne $secondaryOriginalHash) {
        throw 'The failed candidate restart did not restore the secondary executable.'
    }
    $rollbackProcesses = @(
        Get-CimInstance Win32_Process -Filter "Name = 'cga-relay.exe'" |
            Where-Object { $_.ExecutablePath -and
                [System.IO.Path]::GetFullPath($_.ExecutablePath) -eq [System.IO.Path]::GetFullPath($installedPath) }
    )
    if ($rollbackProcesses.Count -ne 1) {
        throw "Expected one restored Relay process, found $($rollbackProcesses.Count)."
    }
    $restoredArguments = Get-CgaRelayArgumentString -CommandLine $rollbackProcesses[0].CommandLine
    if ($restoredArguments -ne $expectedTrayArguments) {
        throw "The restored Relay arguments changed: '$restoredArguments'"
    }
    $secondaryRollbackProcesses = @(
        Get-CimInstance Win32_Process -Filter "Name = 'cga-relay.exe'" |
            Where-Object { $_.ExecutablePath -and
                [System.IO.Path]::GetFullPath($_.ExecutablePath) -eq [System.IO.Path]::GetFullPath($secondaryInstalledPath) }
    )
    if ($secondaryRollbackProcesses.Count -ne 0) {
        throw "The non-tray Relay path was unexpectedly restarted $($secondaryRollbackProcesses.Count) time(s)."
    }
    $rollbackProcess = Get-Process -Id $rollbackProcesses[0].ProcessId
    $rollbackResidue = @(
        Get-ChildItem -LiteralPath $testRoot -File -Recurse |
            Where-Object Name -Like '*.cga-release-*'
    )
    if ($rollbackResidue.Count -ne 0) {
        throw "Rollback residue was not removed: $($rollbackResidue.FullName -join ', ')"
    }

    $failedTargetPath = Join-Path $installedDirectory 'failed-target.exe'
    $failedBackupPath = "$failedTargetPath.cga-release-test.bak"
    $restorableTargetPath = Join-Path $secondaryInstalledDirectory 'restorable-target.exe'
    $restorableBackupPath = "$restorableTargetPath.cga-release-test.bak"
    [System.IO.File]::WriteAllText($failedTargetPath, 'candidate')
    [System.IO.File]::WriteAllText($failedBackupPath, 'original')
    [System.IO.File]::WriteAllText($restorableTargetPath, 'candidate')
    [System.IO.File]::WriteAllText($restorableBackupPath, 'original')
    $faultInjectedTargets = @(
        [pscustomobject]@{
            TargetPath = $failedTargetPath
            BackupPath = $failedBackupPath
            OriginalBackedUp = $true
            Replaced = $true
        }
        [pscustomobject]@{
            TargetPath = $restorableTargetPath
            BackupPath = $restorableBackupPath
            OriginalBackedUp = $true
            Replaced = $true
        }
    )
    $lockedBackup = [System.IO.File]::Open(
        $failedBackupPath,
        [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::ReadWrite,
        [System.IO.FileShare]::None
    )
    try {
        $restoreFailures = @(Restore-CgaRelayTargets -Targets $faultInjectedTargets)
    } finally {
        $lockedBackup.Dispose()
    }
    if ($restoreFailures.Count -ne 1 -or -not $restoreFailures[0].Contains($failedTargetPath)) {
        throw "The fault-injected rollback did not report the locked backup: $($restoreFailures -join '; ')"
    }
    if (-not (Test-Path -LiteralPath $failedBackupPath) -or
        [System.IO.File]::ReadAllText($failedBackupPath) -ne 'original') {
        throw 'The failed rollback did not retain its original backup for manual recovery.'
    }
    if ([System.IO.File]::ReadAllText($restorableTargetPath) -ne 'original' -or
        $faultInjectedTargets[1].OriginalBackedUp -or
        $faultInjectedTargets[1].Replaced) {
        throw 'A failed rollback target prevented a later target from being restored.'
    }

    $injectedDirectory = Join-Path $testRoot 'injected-double-failure'
    $injectedInstalledPath = Join-Path $injectedDirectory 'cga-relay.exe'
    New-Item -ItemType Directory -Path $injectedDirectory -Force | Out-Null
    Copy-Item -LiteralPath $env:ComSpec -Destination $injectedInstalledPath
    $originalMoveFunction = (Get-Item Function:\Move-CgaRelayReleaseFile).ScriptBlock
    function Move-CgaRelayReleaseFile {
        param(
            [string]$SourcePath,
            [string]$DestinationPath
        )

        if ($SourcePath.EndsWith('.new', [StringComparison]::OrdinalIgnoreCase)) {
            throw 'injected promotion failure'
        }
        if ($SourcePath.EndsWith('.bak', [StringComparison]::OrdinalIgnoreCase)) {
            throw 'injected restoration failure'
        }
        Microsoft.PowerShell.Management\Move-Item `
            -LiteralPath $SourcePath `
            -Destination $DestinationPath `
            -Force `
            -ErrorAction Stop
    }
    try {
        $doubleFailureError = $null
        try {
            Install-CgaRelayCandidate `
                -CandidatePath $candidatePath `
                -RunningProcesses @([pscustomobject]@{
                    ProcessId = [int]::MaxValue
                    ExecutablePath = $injectedInstalledPath
                    CommandLine = "`"$injectedInstalledPath`" doctor"
                }) `
                -Restart:$false | Out-Null
        } catch {
            $doubleFailureError = $_.Exception.Message
        }
    } finally {
        Set-Item Function:\Move-CgaRelayReleaseFile -Value $originalMoveFunction
    }
    foreach ($expectedErrorFragment in @(
        'injected promotion failure',
        'Immediate restoration also failed',
        'injected restoration failure',
        'Rollback also failed',
        'Failed-path backups were retained'
    )) {
        if ([string]::IsNullOrWhiteSpace($doubleFailureError) -or
            -not $doubleFailureError.Contains($expectedErrorFragment)) {
            throw "The double-failure report omitted '$expectedErrorFragment': $doubleFailureError"
        }
    }
    $injectedBackups = @(Get-ChildItem -LiteralPath $injectedDirectory -Filter '*.bak' -File)
    $injectedStagedFiles = @(Get-ChildItem -LiteralPath $injectedDirectory -Filter '*.new' -File)
    if ($injectedBackups.Count -ne 1 -or (Test-Path -LiteralPath $injectedInstalledPath)) {
        throw 'The double-failure path did not preserve exactly one backup for manual recovery.'
    }
    if ($injectedStagedFiles.Count -ne 0) {
        throw "The double-failure path left staged residue: $($injectedStagedFiles.FullName -join ', ')"
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
    foreach ($testProcess in @($process, $secondaryProcess, $lockProcess, $rollbackProcess)) {
        if ($testProcess -and -not $testProcess.HasExited) {
            Stop-Process -Id $testProcess.Id -Force -ErrorAction SilentlyContinue
            $testProcess.WaitForExit(5000) | Out-Null
        }
    }
    Remove-Item -LiteralPath $testRoot -Recurse -Force -ErrorAction Stop
}