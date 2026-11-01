param(
    [Parameter(Mandatory = $true)][string]$Executable,
    [Parameter(Mandatory = $true)][string]$Output
)
$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force -Path $Output | Out-Null
$sequence = ("ACGT" * 37) + "AC"
$quality = "I" * 150
$sam = "@HD`tVN:1.6`tSO:queryname`n@SQ`tSN:chr1`tLN:1000`n"
foreach ($name in @("pairA", "pairB")) {
    $sam += "$name`t99`tchr1`t101`t60`t150M`t=`t201`t250`t$sequence`t$quality`n"
    $sam += "$name`t147`tchr1`t201`t60`t150M`t=`t101`t-250`t$sequence`t$quality`n"
}
$info = [Diagnostics.ProcessStartInfo]::new()
$info.FileName = $Executable
$info.UseShellExecute = $false
$info.RedirectStandardInput = $true
$info.RedirectStandardOutput = $true
$info.RedirectStandardError = $true
$process = [Diagnostics.Process]::new()
$process.StartInfo = $info
$started = $false
try {
    $started = $process.Start()
    if (-not $started) { throw "samblaster smoke process failed to start" }
    $process.StandardInput.Write($sam)
    $process.StandardInput.Close()
    $body = $process.StandardOutput.ReadToEnd()
    $log = $process.StandardError.ReadToEnd()
    if (-not $process.WaitForExit(10000) -or $process.ExitCode) {
        throw "samblaster duplicate fixture failed: $log"
    }
    $body | Set-Content (Join-Path $Output "duplicates.sam") -Encoding utf8
    $log | Set-Content (Join-Path $Output "duplicates.log") -Encoding utf8
    $records = @($body -split "`n" | Where-Object { $_ -and -not $_.StartsWith("@") })
    if ($records.Count -ne 4) { throw "samblaster changed the record count" }
    foreach ($record in $records) {
        $fields = $record.TrimEnd("`r") -split "`t"
        $duplicate = (([int]$fields[1] -band 1024) -ne 0)
        if ($duplicate -ne ($fields[0] -eq "pairB")) {
            throw "samblaster did not mark the second pair as duplicate"
        }
    }
    "PASS: four SAM records, second pair correctly marked duplicate"
} finally {
    if ($started -and -not $process.HasExited) { $process.Kill() }
    $process.Dispose()
}
