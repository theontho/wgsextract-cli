param(
    [Parameter(Mandatory = $true)][string]$Executable,
    [Parameter(Mandatory = $true)][string[]]$Arguments,
    [Parameter(Mandatory = $true)][string]$Prefix,
    [Parameter(Mandatory = $true)][string]$Report,
    [string]$InputPrefix = ""
)
$ErrorActionPreference = "Stop"
$info = [Diagnostics.ProcessStartInfo]::new()
$info.FileName = $Executable
$info.UseShellExecute = $false
$info.RedirectStandardInput = $true
$info.RedirectStandardOutput = $true
$info.RedirectStandardError = $true
foreach ($argument in $Arguments) { $info.ArgumentList.Add($argument) }
$process = [Diagnostics.Process]::new()
$process.StartInfo = $info
$started = $false
try {
    $started = $process.Start()
    if (-not $started) { throw "Native tool process could not be started" }
    $stdout = $process.StandardOutput.ReadToEndAsync()
    $stderr = $process.StandardError.ReadToEndAsync()
    if ($InputPrefix) {
        $process.StandardInput.Write($InputPrefix)
        $process.StandardInput.Flush()
    }
    # Keep the real aligner waiting on its input pipe while inspecting modules.
    Start-Sleep -Seconds 1
    if ($process.HasExited) {
        throw "Tool exited before its native process could be audited: $($stderr.GetAwaiter().GetResult())"
    }
    & "$PSScriptRoot\audit_windows_arm64.ps1" -Prefix $Prefix -ProcessIds @($process.Id) -Report $Report
    $process.StandardInput.Close()
    if (-not $process.WaitForExit(10000)) { throw "Tool did not finish after native process audit" }
    $output = $stdout.GetAwaiter().GetResult()
    $errors = $stderr.GetAwaiter().GetResult()
    [IO.File]::WriteAllText("$Report.stdout.log", $output)
    [IO.File]::WriteAllText("$Report.stderr.log", $errors)
    if ($process.ExitCode) { throw "Native tool failed: $errors" }
} finally {
    if ($started -and -not $process.HasExited) { $process.Kill() }
    $process.Dispose()
}
