param(
    [Parameter(Mandatory = $true)][string]$QueueDirectory
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

function Write-BrokerFailure {
    param(
        [object]$Request,
        [string]$Message
    )

    $stderrPath = if ($Request -and $Request.PSObject.Properties["stderrPath"]) { [string]$Request.stderrPath } else { "" }
    if ($stderrPath) {
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::AppendAllText($stderrPath, $Message + [Environment]::NewLine, $utf8NoBom)
    }
    $exitCodePath = if ($Request -and $Request.PSObject.Properties["exitCodePath"]) { [string]$Request.exitCodePath } else { "" }
    if ($exitCodePath -and -not (Test-Path -LiteralPath $exitCodePath)) {
        Set-Content -LiteralPath $exitCodePath -Value 1 -Encoding ascii
    }
}

function Invoke-AdminRequest {
    param([string]$RequestPath)

    $request = $null
    try {
        $request = Get-Content -LiteralPath $RequestPath -Raw | ConvertFrom-Json
        if (-not $request.PSObject.Properties["launcherPath"] -or -not $request.launcherPath) {
            throw "Admin broker request is missing launcherPath."
        }
        $pushedLocation = $false
        if ($request.PSObject.Properties["workingDirectory"] -and $request.workingDirectory) {
            Push-Location -LiteralPath ([string]$request.workingDirectory)
            $pushedLocation = $true
        }
        try {
            & "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" `
                -NoProfile `
                -NonInteractive `
                -ExecutionPolicy Bypass `
                -File ([string]$request.launcherPath)
            $exitCode = if ($LASTEXITCODE -is [int]) { $LASTEXITCODE } else { 0 }
            if ($request.PSObject.Properties["exitCodePath"] -and $request.exitCodePath -and -not (Test-Path -LiteralPath ([string]$request.exitCodePath))) {
                Set-Content -LiteralPath ([string]$request.exitCodePath) -Value $exitCode -Encoding ascii
            }
        }
        finally {
            if ($pushedLocation) {
                Pop-Location
            }
        }
    }
    catch {
        Write-BrokerFailure -Request $request -Message $_.Exception.Message
    }
}

New-Item -ItemType Directory -Force -Path $QueueDirectory | Out-Null
$pendingRequests = @(
    Get-ChildItem -LiteralPath $QueueDirectory -Filter "*.pending.json" -File -ErrorAction SilentlyContinue |
        Sort-Object CreationTimeUtc, Name
)

foreach ($pendingRequest in $pendingRequests) {
    $runningPath = [System.IO.Path]::ChangeExtension($pendingRequest.FullName, ".running.json")
    try {
        Move-Item -LiteralPath $pendingRequest.FullName -Destination $runningPath -ErrorAction Stop
    }
    catch {
        continue
    }
    try {
        Invoke-AdminRequest -RequestPath $runningPath
    }
    finally {
        Remove-Item -LiteralPath $runningPath -Force -ErrorAction SilentlyContinue
    }
}
