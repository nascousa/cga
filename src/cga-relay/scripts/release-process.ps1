function Get-CgaRelayRunningProcesses {
    if ($env:OS -ne 'Windows_NT') {
        return @()
    }

    @(
        Get-CimInstance Win32_Process -Filter "Name = 'cga-relay.exe'" |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_.ExecutablePath) }
    )
}

function Get-CgaRelayArgumentString {
    param(
        [Parameter(Mandatory = $true)]
        [string]$CommandLine
    )

    $trimmed = $CommandLine.Trim()
    if ($trimmed.StartsWith('"')) {
        $closingQuote = $trimmed.IndexOf('"', 1)
        if ($closingQuote -ge 0) {
            return $trimmed.Substring($closingQuote + 1).TrimStart()
        }
    }

    $firstSpace = $trimmed.IndexOf(' ')
    if ($firstSpace -lt 0) {
        return ''
    }
    $trimmed.Substring($firstSpace + 1).TrimStart()
}

function Remove-CgaRelayReleaseFile {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $false)]
        [ValidateRange(1, 100)]
        [int]$MaxAttempts = 50,

        [Parameter(Mandatory = $false)]
        [ValidateRange(0, 1000)]
        [int]$RetryDelayMilliseconds = 100
    )

    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
        if (-not (Test-Path -LiteralPath $Path)) {
            return
        }

        try {
            Remove-Item -LiteralPath $Path -Force -ErrorAction Stop
            return
        } catch {
            if ($attempt -eq $MaxAttempts) {
                throw "Unable to remove release file '$Path' after $MaxAttempts attempts: $($_.Exception.Message)"
            }
            [System.Threading.Thread]::Sleep($RetryDelayMilliseconds)
        }
    }
}

function Install-CgaRelayCandidate {
    param(
        [Parameter(Mandatory = $true)]
        [string]$CandidatePath,

        [Parameter(Mandatory = $true)]
        [AllowEmptyCollection()]
        [object[]]$RunningProcesses,

        [Parameter(Mandatory = $false)]
        [bool]$Restart = $true
    )

    $candidate = (Resolve-Path -LiteralPath $CandidatePath).Path
    $processRecords = @(
        $RunningProcesses |
            Where-Object {
                $_.ProcessId -and -not [string]::IsNullOrWhiteSpace($_.ExecutablePath)
            } |
            ForEach-Object {
                [pscustomobject]@{
                    ProcessId = [int]$_.ProcessId
                    ExecutablePath = [System.IO.Path]::GetFullPath([string]$_.ExecutablePath)
                    Arguments = if ([string]::IsNullOrWhiteSpace($_.CommandLine)) {
                        ''
                    } else {
                        Get-CgaRelayArgumentString -CommandLine ([string]$_.CommandLine)
                    }
                }
            }
    )
    if ($processRecords.Count -eq 0) {
        return [pscustomobject]@{
            StoppedCount = 0
            ReplacedCount = 0
            RestartedCount = 0
            ReplacedPaths = @()
            RestartedProcessIds = @()
        }
    }

    $replacementId = [guid]::NewGuid().ToString('N')
    $targets = @(
        $processRecords |
            Group-Object { $_.ExecutablePath.ToLowerInvariant() } |
            ForEach-Object {
                $targetPath = $_.Group[0].ExecutablePath
                $stagedPath = "$targetPath.cga-release-$replacementId.new"
                $backupPath = "$targetPath.cga-release-$replacementId.bak"
                Copy-Item -LiteralPath $candidate -Destination $stagedPath -Force
                [pscustomobject]@{
                    TargetPath = $targetPath
                    StagedPath = $stagedPath
                    BackupPath = $backupPath
                    Replaced = $false
                }
            }
    )

    try {
        foreach ($record in $processRecords) {
            if ($record.ProcessId -eq $PID) {
                throw 'The release script cannot replace its own process.'
            }
            $activeProcess = Get-Process -Id $record.ProcessId -ErrorAction SilentlyContinue
            if ($activeProcess) {
                Stop-Process -Id $record.ProcessId -Force -ErrorAction Stop
            }
        }
        foreach ($record in $processRecords) {
            Wait-Process -Id $record.ProcessId -Timeout 10 -ErrorAction SilentlyContinue
            if (Get-Process -Id $record.ProcessId -ErrorAction SilentlyContinue) {
                throw "CGA-Relay process $($record.ProcessId) did not exit within 10 seconds."
            }
        }

        foreach ($target in $targets) {
            Move-Item -LiteralPath $target.TargetPath -Destination $target.BackupPath -Force
            try {
                Move-Item -LiteralPath $target.StagedPath -Destination $target.TargetPath -Force
                $target.Replaced = $true
            } catch {
                Move-Item -LiteralPath $target.BackupPath -Destination $target.TargetPath -Force
                throw
            }
        }
    } catch {
        foreach ($target in $targets) {
            if ($target.Replaced -and (Test-Path -LiteralPath $target.BackupPath)) {
                Remove-Item -LiteralPath $target.TargetPath -Force -ErrorAction SilentlyContinue
                Move-Item -LiteralPath $target.BackupPath -Destination $target.TargetPath -Force
            }
        }
        throw
    } finally {
        foreach ($target in $targets) {
            Remove-Item -LiteralPath $target.StagedPath -Force -ErrorAction SilentlyContinue
        }
    }

    $restartedProcessIds = @()
    if ($Restart) {
        $trayRecord = $processRecords |
            Where-Object { $_.Arguments -match '(^|\s)tray(\s|$)' } |
            Select-Object -First 1
        if ($trayRecord) {
            $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
            $startInfo.FileName = $trayRecord.ExecutablePath
            $startInfo.Arguments = $trayRecord.Arguments
            $startInfo.UseShellExecute = $false
            $startInfo.CreateNoWindow = $true
            $restarted = [System.Diagnostics.Process]::Start($startInfo)
            if (-not $restarted -or $restarted.WaitForExit(1000)) {
                throw 'The replaced CGA-Relay tray process did not remain running.'
            }
            $restartedProcessIds = @($restarted.Id)
        }
    }

    foreach ($target in $targets) {
        Remove-CgaRelayReleaseFile -Path $target.BackupPath
    }

    [pscustomobject]@{
        StoppedCount = $processRecords.Count
        ReplacedCount = $targets.Count
        RestartedCount = $restartedProcessIds.Count
        ReplacedPaths = @($targets | ForEach-Object TargetPath)
        RestartedProcessIds = $restartedProcessIds
    }
}