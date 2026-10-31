[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Prefix,
    [int[]]$ProcessIds = @(),
    [string]$Report = "out/windows-arm64/architecture.json"
)

$ErrorActionPreference = "Stop"

function Get-PeMachine {
    param([string]$Path)
    $stream = [System.IO.File]::OpenRead($Path)
    $reader = [System.IO.BinaryReader]::new($stream)
    try {
        if ($stream.Length -lt 64 -or $reader.ReadUInt16() -ne 0x5A4D) {
            throw "${Path}: missing DOS header"
        }
        $stream.Position = 60
        $offset = $reader.ReadUInt32()
        if ($offset -lt 64 -or $offset + 24 -gt $stream.Length) {
            throw "${Path}: invalid PE offset"
        }
        $stream.Position = $offset
        if ($reader.ReadUInt32() -ne 0x4550) { throw "${Path}: missing PE header" }
        return $reader.ReadUInt16()
    } finally {
        $reader.Dispose()
    }
}

if (-not ("WgseArm64Audit" -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class WgseArm64Audit {
    [DllImport("kernel32.dll", SetLastError=true)]
    public static extern bool IsWow64Process2(IntPtr process, out ushort machine, out ushort native);
}
'@
}

$rows = @()
$failures = @()
foreach ($file in Get-ChildItem -LiteralPath $Prefix -Recurse -File |
    Where-Object { $_.Extension -in @(".exe", ".dll", ".pyd") }) {
    $machine = Get-PeMachine $file.FullName
    $rows += [ordered]@{ path = $file.FullName; machine = ("{0:X4}" -f $machine) }
    if ($machine -ne 0xAA64) { $failures += $file.FullName }
}
if ($rows.Count -eq 0) { throw "No PE files found in $Prefix; an empty audit cannot pass" }

$processRows = @()
foreach ($idToAudit in $ProcessIds) {
    $process = Get-Process -Id $idToAudit -ErrorAction Stop
    [ushort]$machine = 0
    [ushort]$native = 0
    if (-not [WgseArm64Audit]::IsWow64Process2($process.Handle, [ref]$machine, [ref]$native)) {
        throw "Cannot query process $idToAudit"
    }
    $pe = Get-PeMachine $process.Path
    $modules = @()
    foreach ($module in $process.Modules) {
        $moduleMachine = Get-PeMachine $module.FileName
        $modules += [ordered]@{
            path = $module.FileName; machine = ("{0:X4}" -f $moduleMachine)
        }
        # Windows itself may ship hybrid system images. Application modules
        # outside the audited prefix still must not introduce x64/ARM64EC.
        if (-not $module.FileName.StartsWith(
            $env:SystemRoot + "\", [System.StringComparison]::OrdinalIgnoreCase
        ) -and $moduleMachine -ne 0xAA64) {
            $failures += $module.FileName
        }
    }
    $processRows += [ordered]@{
        pid = $idToAudit; path = $process.Path
        process_machine = ("{0:X4}" -f $machine)
        native_machine = ("{0:X4}" -f $native)
        pe_machine = ("{0:X4}" -f $pe)
        modules = $modules
    }
    if ($native -ne 0xAA64 -or $machine -ne 0 -or $pe -ne 0xAA64) {
        $failures += "process $idToAudit ($($process.Path))"
    }
}
$parent = Split-Path -Parent $Report
if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
[ordered]@{
    files = $rows; processes = $processRows; failures = $failures
} | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $Report -Encoding utf8
if ($failures.Count) { throw "Non-native ARM64 components: $($failures -join ', ')" }
Write-Host "Native ARM64 audit passed: $($rows.Count) PE files, $($processRows.Count) processes."
