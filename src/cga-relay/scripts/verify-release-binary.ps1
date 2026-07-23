param(
    [Parameter(Mandatory = $true)]
    [string]$BinaryPath,

    [Parameter(Mandatory = $false)]
    [switch]$RequireSignature,

    [Parameter(Mandatory = $false)]
    [string[]]$ForbiddenText = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$resolvedPath = (Resolve-Path -LiteralPath $BinaryPath).Path
$bytes = [System.IO.File]::ReadAllBytes($resolvedPath)
if ($bytes.Length -lt 512 -or $bytes[0] -ne 0x4d -or $bytes[1] -ne 0x5a) {
    throw 'Relay artifact is not a valid PE executable.'
}

$peOffset = [BitConverter]::ToInt32($bytes, 0x3c)
if ($peOffset -lt 0 -or $peOffset + 256 -gt $bytes.Length) {
    throw 'Relay artifact contains an invalid PE header offset.'
}
if ($bytes[$peOffset] -ne 0x50 -or $bytes[$peOffset + 1] -ne 0x45 -or
    $bytes[$peOffset + 2] -ne 0 -or $bytes[$peOffset + 3] -ne 0) {
    throw 'Relay artifact contains an invalid PE signature.'
}

$machine = [BitConverter]::ToUInt16($bytes, $peOffset + 4)
if ($machine -ne 0x8664) {
    throw ('Relay artifact must target Windows x64; PE machine is 0x{0:x4}.' -f $machine)
}

$pointerToSymbols = [BitConverter]::ToUInt32($bytes, $peOffset + 12)
$symbolCount = [BitConverter]::ToUInt32($bytes, $peOffset + 16)
if ($pointerToSymbols -ne 0 -or $symbolCount -ne 0) {
    throw 'Relay artifact still contains a COFF symbol table.'
}

$optionalHeader = $peOffset + 24
$optionalMagic = [BitConverter]::ToUInt16($bytes, $optionalHeader)
if ($optionalMagic -ne 0x20b) {
    throw ('Relay artifact must use PE32+; optional header is 0x{0:x4}.' -f $optionalMagic)
}

$sectionCount = [BitConverter]::ToUInt16($bytes, $peOffset + 6)
$optionalHeaderSize = [BitConverter]::ToUInt16($bytes, $peOffset + 20)
$sectionTable = $optionalHeader + $optionalHeaderSize
for ($index = 0; $index -lt $sectionCount; $index++) {
    $section = $sectionTable + ($index * 40)
    if ($section + 40 -gt $bytes.Length) {
        throw 'Relay artifact contains a malformed PE section table.'
    }
    $characteristics = [BitConverter]::ToUInt32($bytes, $section + 36)
    if (($characteristics -band 0x20000000) -ne 0 -and
        ($characteristics -band 0x80000000) -ne 0) {
        throw 'Relay artifact contains a writable and executable PE section.'
    }
}

$dllCharacteristics = [BitConverter]::ToUInt16($bytes, $optionalHeader + 70)
$requiredMitigations = [ordered]@{
    HighEntropyVA = 0x0020
    DynamicBase   = 0x0040
    NxCompat      = 0x0100
    GuardCF       = 0x4000
}
$missingMitigations = @(
    foreach ($mitigation in $requiredMitigations.GetEnumerator()) {
        if (($dllCharacteristics -band $mitigation.Value) -eq 0) {
            $mitigation.Key
        }
    }
)
if ($missingMitigations.Count -gt 0) {
    throw "Relay artifact is missing PE mitigations: $($missingMitigations -join ', ')."
}

$loadConfigDirectoryOffset = $optionalHeader + 112 + (10 * 8)
$loadConfigRva = [BitConverter]::ToUInt32($bytes, $loadConfigDirectoryOffset)
$loadConfigSize = [BitConverter]::ToUInt32($bytes, $loadConfigDirectoryOffset + 4)
$loadConfigFileOffset = $null
for ($index = 0; $index -lt $sectionCount; $index++) {
    $section = $sectionTable + ($index * 40)
    $virtualSize = [BitConverter]::ToUInt32($bytes, $section + 8)
    $virtualAddress = [BitConverter]::ToUInt32($bytes, $section + 12)
    $rawSize = [BitConverter]::ToUInt32($bytes, $section + 16)
    $rawPointer = [BitConverter]::ToUInt32($bytes, $section + 20)
    $mappedSize = [Math]::Max($virtualSize, $rawSize)
    if ($loadConfigRva -ge $virtualAddress -and
        $loadConfigRva -lt ($virtualAddress + $mappedSize)) {
        $loadConfigFileOffset = $rawPointer + ($loadConfigRva - $virtualAddress)
        break
    }
}
if ($loadConfigSize -lt 148 -or $null -eq $loadConfigFileOffset -or
    $loadConfigFileOffset + 148 -gt $bytes.Length) {
    throw 'Relay artifact is missing a valid PE load configuration for CFG.'
}
$declaredLoadConfigSize = [BitConverter]::ToUInt32($bytes, $loadConfigFileOffset)
$guardCheckPointer = [BitConverter]::ToUInt64($bytes, $loadConfigFileOffset + 112)
$guardDispatchPointer = [BitConverter]::ToUInt64($bytes, $loadConfigFileOffset + 120)
$guardFunctionTable = [BitConverter]::ToUInt64($bytes, $loadConfigFileOffset + 128)
$guardFunctionCount = [BitConverter]::ToUInt64($bytes, $loadConfigFileOffset + 136)
$guardFlags = [BitConverter]::ToUInt32($bytes, $loadConfigFileOffset + 144)
if ($declaredLoadConfigSize -lt 148 -or $guardCheckPointer -eq 0 -or
    $guardDispatchPointer -eq 0 -or $guardFunctionTable -eq 0 -or
    $guardFunctionCount -eq 0 -or ($guardFlags -band 0x100) -eq 0 -or
    ($guardFlags -band 0x400) -eq 0) {
    throw 'Relay artifact advertises CFG without complete instrumentation metadata.'
}

$debugDirectoryOffset = $optionalHeader + 112 + (6 * 8)
$debugDirectoryRva = [BitConverter]::ToUInt32($bytes, $debugDirectoryOffset)
$debugDirectorySize = [BitConverter]::ToUInt32($bytes, $debugDirectoryOffset + 4)
$debugTypes = @()
$cetCompat = $false
if ($debugDirectorySize -gt 0) {
    $debugFileOffset = $null
    for ($index = 0; $index -lt $sectionCount; $index++) {
        $section = $sectionTable + ($index * 40)
        $virtualSize = [BitConverter]::ToUInt32($bytes, $section + 8)
        $virtualAddress = [BitConverter]::ToUInt32($bytes, $section + 12)
        $rawSize = [BitConverter]::ToUInt32($bytes, $section + 16)
        $rawPointer = [BitConverter]::ToUInt32($bytes, $section + 20)
        $mappedSize = [Math]::Max($virtualSize, $rawSize)
        if ($debugDirectoryRva -ge $virtualAddress -and
            $debugDirectoryRva -lt ($virtualAddress + $mappedSize)) {
            $debugFileOffset = $rawPointer + ($debugDirectoryRva - $virtualAddress)
            break
        }
    }
    if ($null -eq $debugFileOffset) {
        throw 'Relay artifact debug metadata could not be mapped to a PE section.'
    }
    if (($debugDirectorySize % 28) -ne 0 -or
        $debugFileOffset + $debugDirectorySize -gt $bytes.Length) {
        throw 'Relay artifact contains a malformed PE debug directory.'
    }
    for ($offset = 0; $offset -lt $debugDirectorySize; $offset += 28) {
        $entryOffset = $debugFileOffset + $offset
        $debugType = [BitConverter]::ToUInt32($bytes, $entryOffset + 12)
        $debugTypes += $debugType
        if ($debugType -eq 20) {
            $dataSize = [BitConverter]::ToUInt32($bytes, $entryOffset + 16)
            $dataPointer = [BitConverter]::ToUInt32($bytes, $entryOffset + 24)
            if ($dataSize -lt 4 -or $dataPointer + 4 -gt $bytes.Length) {
                throw 'Relay artifact contains malformed extended DLL characteristics metadata.'
            }
            $extendedDllCharacteristics = [BitConverter]::ToUInt32($bytes, $dataPointer)
            $cetCompat = ($extendedDllCharacteristics -band 0x1) -ne 0
        }
    }
    $symbolDebugTypes = @($debugTypes | Where-Object { $_ -in @(1, 2, 17) })
    if ($symbolDebugTypes.Count -gt 0) {
        throw "Relay artifact contains COFF, CodeView, or embedded PDB debug data: $($symbolDebugTypes -join ', ')."
    }
    $unexpectedDebugTypes = @($debugTypes | Where-Object { $_ -notin @(13, 16, 20) })
    if ($unexpectedDebugTypes.Count -gt 0) {
        throw "Relay artifact contains unexpected PE debug metadata: $($unexpectedDebugTypes -join ', ')."
    }
}
if (16 -notin $debugTypes) {
    throw 'Relay artifact is missing reproducible-build metadata.'
}
if (-not $cetCompat) {
    throw 'Relay artifact is missing CET compatibility metadata.'
}

$ascii = [Text.Encoding]::ASCII.GetString($bytes)
$unicode = [Text.Encoding]::Unicode.GetString($bytes)
$singleInstanceMutexImport = 'CreateMutexW'
$singleInstanceMutexName = 'Global\Nascousa.CGA-Relay.SingleInstance'
$testInstanceScopeName = 'CGA_RELAY_TEST_INSTANCE_SCOPE'
if ($ascii.IndexOf($singleInstanceMutexImport, [StringComparison]::Ordinal) -lt 0) {
    throw "Relay artifact is missing the $singleInstanceMutexImport single-instance mutex import."
}
if ($ascii.IndexOf($singleInstanceMutexName, [StringComparison]::Ordinal) -lt 0 -and
    $unicode.IndexOf($singleInstanceMutexName, [StringComparison]::Ordinal) -lt 0) {
    throw 'Relay artifact is missing the fixed CGA-Relay single-instance mutex name.'
}
$forbiddenPatterns = @(
    '(?i)\.pdb(?:\x00|$)',
    '(?i)[a-z]:\\repos\\',
    '(?i)[a-z]:\\users\\',
    'TEST_SECRET_VALUE_SHOULD_NEVER_LEAK',
    $testInstanceScopeName,
    'UPX!',
    'UPX[0-9]'
)
foreach ($pattern in $forbiddenPatterns) {
    if ($ascii -match $pattern -or $unicode -match $pattern) {
        throw "Relay artifact contains forbidden debug, source-path, test-secret, or packer material: $pattern"
    }
}
foreach ($text in $ForbiddenText) {
    if (-not [string]::IsNullOrWhiteSpace($text) -and
        ($ascii.IndexOf($text, [StringComparison]::OrdinalIgnoreCase) -ge 0 -or
            $unicode.IndexOf($text, [StringComparison]::OrdinalIgnoreCase) -ge 0)) {
        throw 'Relay artifact contains the configured build or source path.'
    }
}

$signature = Get-AuthenticodeSignature -LiteralPath $resolvedPath
if ($RequireSignature -and $signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid) {
    throw "Relay Authenticode signature is not valid: $($signature.StatusMessage)"
}
if ($RequireSignature -and -not $signature.TimeStamperCertificate) {
    throw 'Relay Authenticode signature does not contain a trusted timestamp.'
}

$hash = Get-FileHash -LiteralPath $resolvedPath -Algorithm SHA256
[pscustomobject]@{
    Path                 = $resolvedPath
    Size                 = $bytes.Length
    SHA256               = $hash.Hash.ToLowerInvariant()
    HighEntropyVA        = $true
    DynamicBase          = $true
    NxCompat             = $true
    GuardCF              = $true
    GuardFunctionCount   = $guardFunctionCount
    CetCompat            = $true
    WritableExecutableSectionsAbsent = $true
    SymbolDebugDataAbsent = $true
    PermittedDebugTypes   = @($debugTypes)
    CoffSymbolsAbsent    = $true
    SingleInstanceMutexImport = $singleInstanceMutexImport
    SingleInstanceMutexName = $singleInstanceMutexName
    TestInstanceScopeAbsent = $true
    AuthenticodeStatus   = $signature.Status.ToString()
    SignerSubject        = if ($signature.SignerCertificate) { $signature.SignerCertificate.Subject } else { $null }
    TimestampSubject     = if ($signature.TimeStamperCertificate) { $signature.TimeStamperCertificate.Subject } else { $null }
} | ConvertTo-Json -Depth 3