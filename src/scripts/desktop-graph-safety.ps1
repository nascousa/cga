function Assert-CgaGraphPersistence {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ComposeFile
    )

    $expectedDirectory = '/var/lib/falkordb/data'
    $containers = @(& docker compose -f $ComposeFile ps --all --quiet falkordb)
    if ($LASTEXITCODE -ne 0) {
        throw 'Cannot inspect existing FalkorDB containers; refusing to recreate the stack.'
    }
    foreach ($containerId in $containers) {
        if ([string]::IsNullOrWhiteSpace($containerId)) {
            continue
        }
        $mountJson = & docker inspect --format '{{json .Mounts}}' $containerId
        if ($LASTEXITCODE -ne 0) {
            throw 'Cannot inspect FalkorDB persistence; existing containers were left untouched.'
        }
        $mounts = @($mountJson | ConvertFrom-Json)
        $persistent = @($mounts | Where-Object {
            $_.Type -in @('volume', 'bind') -and $_.RW -and
            $_.Destination.TrimEnd('/') -ceq $expectedDirectory
        })
        if ($persistent.Count -ne 1) {
            throw "Unsafe FalkorDB volume mapping in $containerId. Do not recreate or remove this container. Save and migrate its RDB into the persistent $expectedDirectory volume first; see runtime-operations.md."
        }
        $running = & docker inspect --format '{{.State.Running}}' $containerId
        if ($LASTEXITCODE -ne 0) {
            throw 'Cannot inspect FalkorDB state; refusing to recreate the stack.'
        }
        if ($running -eq 'true') {
            $directory = @(& docker exec $containerId redis-cli --raw CONFIG GET dir)
            if ($LASTEXITCODE -ne 0 -or $directory.Count -ne 2 -or
                $directory[0] -cne 'dir' -or $directory[1] -cne $expectedDirectory) {
                throw 'Cannot confirm the running FalkorDB data directory matches its volume. Preserve the existing container and verify its configuration and backup before upgrading.'
            }
        }
    }
}
