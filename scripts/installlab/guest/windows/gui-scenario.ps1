# Install-lab guest scenario for the WGS Extract app on Windows (Tauri, NSIS).
#
# baseline snapshot -> download the installer like a browser (Mark of the Web)
# -> Authenticode signature -> install with the wizard's defaults (its Finish page
# opens the app; it is closed) -> the app runs its own setup in Lab
# User's desktop session -> close the window -> real actions through the app's
# bundled runtime -> snapshot -> Settings > Apps > Uninstall with "Delete the
# application data" -> snapshot
#
# Inputs: a JSON file with LAB_RESULTS / LAB_NONCE / LAB_MODE / LAB_SABOTAGE,
#   LAB_GUI_INSTALLER_URL / LAB_GUI_INSTALLER_NAME   the installer under test
#   LAB_CLI_ARCHIVE_URL   optional .tar.gz of a CLI tree for setup to install
#                         (unset: setup installs the CLI release the app pins)
# Nothing here decides pass/fail; the host's oracle does, from the evidence.
param([Parameter(Mandatory = $true)][string]$EnvFile)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
. (Join-Path $PSScriptRoot "lab-common.ps1")

$lab = Get-Content -LiteralPath $EnvFile -Raw | ConvertFrom-Json
$R = $lab.LAB_RESULTS
$Evidence = Join-Path $R "evidence"
New-Item -ItemType Directory -Force -Path $Evidence, (Join-Path $R "workload") | Out-Null
Write-Text (Join-Path $R "nonce") $lab.LAB_NONCE
$Sabotage = "$($lab.LAB_SABOTAGE)"
$Mode = if ($lab.LAB_MODE) { $lab.LAB_MODE } else { "install" }
$Inventory = Join-Path $PSScriptRoot "inventory.ps1"
$Downloads = Join-Path $env:USERPROFILE "Downloads"
$Installer = Join-Path $Downloads $lab.LAB_GUI_INSTALLER_NAME
$ProductName = "WGSExtract"
$UninstallKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$ProductName"
$AppIdentifier = "dev.guiforcli.web.embed.wgsextract"
$AppProcess = "gui-for-cli-webui-tauri"
$Workspace = Join-Path $env:LOCALAPPDATA "$AppIdentifier\BundleWorkspaces\wgs-extract"

function Save-Snapshot([string]$Name) {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Inventory (Join-Path $R "inventory-$Name.txt") $R
}

function Step([string]$Name, [string[]]$Command, [string]$Cwd = $env:USERPROFILE) {
    Invoke-Step -Evidence $Evidence -Name $Name -WorkingDirectory $Cwd -Command $Command | Out-Null
}

# A step written in PowerShell: <name>.log gets its output, <name>.rc is 1 if it threw.
function PsStep([string]$Name, [scriptblock]$Block) {
    $start = Get-Date
    $rc = 0
    # Output collected line by line, so what ran before a failure stays in the log.
    $out = New-Object System.Collections.Generic.List[string]
    try { & $Block 2>&1 | ForEach-Object { $out.Add(($_ | Out-String -Width 300).TrimEnd()) } }
    catch { $rc = 1; $out.Add("ERROR: $($_ | Out-String -Width 300)") }
    Write-Text (Join-Path $Evidence "$Name.log") ($out -join "`n")
    Write-Text (Join-Path $Evidence "$Name.rc") "$rc"
    Write-Text (Join-Path $Evidence "$Name.seconds") ([int]((Get-Date) - $start).TotalSeconds)
    Write-Host "step $Name rc=$rc"
}

function Wait-ForFile([string]$Path, [int]$Seconds) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while (-not (Test-Path -LiteralPath $Path)) {
        if ((Get-Date) -ge $deadline) { throw "timed out after $Seconds s waiting for $Path" }
        Start-Sleep -Seconds 5
    }
}

# Run in-session.ps1 on Lab User's desktop (SSH has none) and wait for it.
function Invoke-InSession([string]$Action, [hashtable]$Request, [int]$Seconds) {
    $Request.results = $R
    $requestFile = Join-Path $R "session-$Action.json"
    Write-Text $requestFile ($Request | ConvertTo-Json -Depth 4)
    $done = Join-Path $R "session-$Action.rc"
    Remove-Item -LiteralPath $done -ErrorAction SilentlyContinue
    $task = "wgse-lab-$Action"
    $script = Join-Path $PSScriptRoot "in-session.ps1"
    $run = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File $script -Action $Action -Request $requestFile"
    # /IT: only in the user's signed-in desktop session, never a background one.
    schtasks.exe /Create /TN $task /TR $run /SC ONCE /ST 23:59 /IT /F | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "could not create the $task task" }
    try {
        schtasks.exe /Run /TN $task | Out-Null
        Wait-ForFile $done $Seconds
    } finally {
        schtasks.exe /Delete /TN $task /F | Out-Null
    }
    Get-Content -LiteralPath (Join-Path $R "session-$Action.log")
    if ((Get-Content -LiteralPath $done) -ne "0") { throw "$Action failed on the desktop" }
}

function Get-AppProcesses {
    # Everything the app started: its window, the Node backend and anything they run.
    $install = (Get-ItemProperty -LiteralPath $UninstallKey -ErrorAction SilentlyContinue).InstallLocation
    Get-Process -ErrorAction SilentlyContinue | Where-Object {
        $_.Path -and (($install -and $_.Path.StartsWith($install.Trim('"'), "OrdinalIgnoreCase")) -or
            $_.Path.StartsWith($Workspace, "OrdinalIgnoreCase"))
    }
}

Save-Snapshot "baseline"
$survey = foreach ($tool in "pixi", "samtools", "bcftools", "java", "python", "conda", "mamba", "bash", "gcc", "perl", "git") {
    $found = (Get-Command $tool -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1).Source
    if ($found -and $found -like "*\WindowsApps\*") { $found = "" }
    if ($found -and $tool -eq "bash" -and $found -ieq "$env:SystemRoot\System32\bash.exe") { $found = "" }
    "$tool`t$found"
}
foreach ($msys in "C:\msys64", (Join-Path $env:LOCALAPPDATA "msys64")) {
    $survey += if (Test-Path -LiteralPath $msys) { "msys2`t$msys" } else { "msys2`t" }
}
# Installers reuse a Pixi in its default place even when PATH doesn't reach it.
$pixiHome = Join-Path $env:USERPROFILE ".pixi\bin\pixi.exe"
$survey += if (Test-Path -LiteralPath $pixiHome) { "pixi-home`t$pixiHome" } else { "pixi-home`t" }
[System.IO.File]::WriteAllLines((Join-Path $R "preexisting-tools.txt"), [string[]]$survey, $Utf8)

if ($Mode -eq "control") {
    # As long as an app run (setup, actions, uninstall take about 30 minutes),
    # with the same signed-in desktop, so the desktop's background activity shows.
    # A different WebView2 app (webview2-window.ps1) is open for 4 minutes, as
    # long as the app's window stays open: what the WebView2 runtime and Windows
    # keep for any WebView2 app.
    Invoke-InSession "launch" @{
        exe = "powershell.exe"; cwd = $env:USERPROFILE; env = @{}
        args = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $PSScriptRoot "webview2-window.ps1"),
            (Join-Path $PSScriptRoot "webview2-sdk"), "240")
        stdout = Join-Path $R "webview2-window-stdout.txt"; stderr = Join-Path $R "webview2-window-stderr.txt"
        wait = $true
    } 420
    Start-Sleep -Seconds 660
    Save-Snapshot "after_install"
    Start-Sleep -Seconds 900
    Save-Snapshot "after_uninstall"
    Write-Host "scenario finished"
    exit 0
}

New-Item -ItemType Directory -Force -Path $Downloads | Out-Null
Step "download_installer" @("curl.exe", "-fsSL", "-o", $Installer, $lab.LAB_GUI_INSTALLER_URL)
# What a browser does to a download; SmartScreen keys off it.
Set-Content -LiteralPath $Installer -Stream Zone.Identifier -Value "[ZoneTransfer]`r`nZoneId=3`r`nHostUrl=$($lab.LAB_GUI_INSTALLER_URL)"

PsStep "signature" {
    $zone = Get-Content -LiteralPath $Installer -Stream Zone.Identifier -ErrorAction SilentlyContinue
    "Mark of the Web: $($zone -join ' ')"
    $sig = Get-AuthenticodeSignature -LiteralPath $Installer
    "Status: $($sig.Status)"
    "Signer: $($sig.SignerCertificate.Subject)"
    if ($sig.Status -ne "Valid") { throw "not validly signed: $($sig.Status)" }
}
# Clicking "More info > Run anyway" in SmartScreen's warning; the lab can't click it.
Unblock-File -LiteralPath $Installer

PsStep "install_app" {
    # Double-clicking the download and clicking through the wizard's defaults.
    Invoke-InSession "install" @{
        installer = $Installer; windowPattern = "$ProductName*Setup*"; titlePrefix = $ProductName
    } 3700
    $key = Get-ItemProperty -LiteralPath $UninstallKey
    "Apps entry: $($key.DisplayName) $($key.DisplayVersion) at $($key.InstallLocation)"
    "UninstallString: $($key.UninstallString)"
    $exe = Join-Path $key.InstallLocation.Trim('"') "$AppProcess.exe"
    if (-not (Test-Path -LiteralPath $exe)) { throw "no $exe" }
    $shortcut = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\$($key.DisplayName).lnk"
    if (-not (Test-Path -LiteralPath $shortcut)) { throw "no Start menu shortcut $shortcut" }
    "Start menu: $shortcut"
    # The Finish page's "Run" box is ticked by default, so the app opens. The
    # user closes it here; setup below opens it again with the lab's settings.
    $deadline = (Get-Date).AddSeconds(120)
    while (-not (Get-Process -Name $AppProcess -ErrorAction SilentlyContinue)) {
        if ((Get-Date) -ge $deadline) { throw "the app did not open from the Finish page" }
        Start-Sleep -Seconds 2
    }
    "the app opened from the Finish page"
    Start-Sleep -Seconds 20
    Invoke-InSession "close" @{ processName = $AppProcess } 120
    Start-Sleep -Seconds 5
    $left = @(Get-AppProcesses)
    if ($left.Count -gt 0) { throw "still running after the window closed: $($left.Path -join ', ')" }
}
$app = Get-ItemProperty -LiteralPath $UninstallKey -ErrorAction SilentlyContinue
$InstallDir = if ($app) { $app.InstallLocation.Trim('"') } else { Join-Path $env:LOCALAPPDATA "WGSExtract" }

PsStep "setup" {
    $events = Join-Path $R "setup-events.ndjson"
    $appEnv = @{
        GUI_FOR_CLI_AUTO_RUN_SETUP = "1"
        GUI_FOR_CLI_SETUP_EVENTS_LOG = $events
    }
    if ($lab.LAB_CLI_ARCHIVE_URL) { $appEnv.WGSEXTRACT_ARCHIVE_URL = $lab.LAB_CLI_ARCHIVE_URL }
    # Opening the app from the Start menu; it runs its own setup.
    Invoke-InSession "launch" @{
        exe = Join-Path $InstallDir "$AppProcess.exe"; cwd = $InstallDir; env = $appEnv
        stdout = Join-Path $R "app-stdout.txt"; stderr = Join-Path $R "app-stderr.txt"
    } 120
    $started = Get-Date
    $deadline = $started.AddHours(3)
    $complete = $null
    while (-not $complete) {
        if ((Get-Date) -ge $deadline) { throw "setup did not finish within 3 hours" }
        $running = @(Get-AppProcesses)
        if (-not ($running | Where-Object { $_.ProcessName -eq $AppProcess })) { throw "the app exited during setup" }
        # The window starts setup as soon as it has loaded; without its backend it never will.
        if (-not (Test-Path -LiteralPath $events)) {
            if (-not ($running | Where-Object { $_.ProcessName -eq "node" })) { throw "the app's backend (node) is not running" }
            if ((Get-Date) -ge $started.AddMinutes(20)) { throw "setup did not start within 20 minutes" }
        }
        Start-Sleep -Seconds 15
        if (Test-Path -LiteralPath $events) {
            $complete = Get-Content -LiteralPath $events -Encoding UTF8 | ForEach-Object { $_ | ConvertFrom-Json } |
                Where-Object { $_.type -eq "complete" } | Select-Object -Last 1
        }
    }
    Write-Text (Join-Path $R "setup-result.json") ($complete.result | ConvertTo-Json -Depth 8)
    "setup: $($complete.result.status)"
    Invoke-InSession "close" @{ processName = $AppProcess } 120
    Start-Sleep -Seconds 5
    $left = @(Get-AppProcesses)
    if ($left.Count -gt 0) { throw "still running after the window closed: $($left.Path -join ', ')" }
}

Copy-Item -LiteralPath (Join-Path $env:LOCALAPPDATA "$AppIdentifier\logs\backend.log") `
    -Destination (Join-Path $R "app-backend.log") -ErrorAction SilentlyContinue

# The real actions through the app's own runtime: its bundled Node and its
# bundle-test runner, on its bundle and workspace. Clicking isn't simulated;
# everything a click would start is.
$env:GUI_FOR_CLI_APP_SUPPORT_NAME = $AppIdentifier
Step "actions" @((Join-Path $InstallDir "node\node.exe"),
    (Join-Path $InstallDir "platform\typescript\dist\web\src\server\bundle-test-cli.js"),
    "--plan", (Join-Path (Split-Path $PSScriptRoot) "gui-plan.json"), "--workspace", $Workspace,
    "--report", (Join-Path $R "bundle-report.json"), "--log", (Join-Path $R "bundle-test.log"),
    (Join-Path $InstallDir "examples\EmbeddedBundle")) $InstallDir
Remove-Item Env:GUI_FOR_CLI_APP_SUPPORT_NAME
# The oracle reads outputs where the CLI lab keeps them.
if (Test-Path -LiteralPath (Join-Path $Workspace "lab-output")) {
    Copy-Item -Path (Join-Path $Workspace "lab-output\*") -Destination (Join-Path $R "workload") -Recurse -Force
}
New-Item -ItemType Directory -Force -Path (Join-Path $R "workload\info") | Out-Null
foreach ($dir in "output", "genomes") {
    Get-ChildItem -LiteralPath (Join-Path $Workspace $dir) -Recurse -Filter "*.wgse_info.json" -ErrorAction SilentlyContinue |
        Copy-Item -Destination (Join-Path $R "workload\info")
}
Save-Snapshot "after_install"

if ($Sabotage -ne "skip-uninstall") {
    PsStep "uninstall" {
        $key = Get-ItemProperty -LiteralPath $UninstallKey
        Invoke-InSession "uninstall" @{
            uninstallString = $key.UninstallString; displayName = $key.DisplayName
            deleteAppDataLabel = "Delete the application data"
            env = @{ GUI_FOR_CLI_UNINSTALL_RESULT_FILE = (Join-Path $R "uninstall-result.json") }
        } 7200
        if (Test-Path -LiteralPath $UninstallKey) { throw "the Apps entry is still registered" }
        $left = @(Get-AppProcesses)
        if ($left.Count -gt 0) { throw "still running after uninstall: $($left.Path -join ', ')" }
    }
}
# Remove what the test itself created, so any remaining change is the product's.
Remove-Item -LiteralPath $Installer -Force -ErrorAction SilentlyContinue
Update-SessionEnvironment
Save-Snapshot "after_uninstall"
$runStart = (Get-Item -LiteralPath (Join-Path $R "nonce")).LastWriteTime
foreach ($dir in "$env:LOCALAPPDATA\Microsoft\Windows\WER", "$env:ProgramData\Microsoft\Windows\WER", "$env:LOCALAPPDATA\CrashDumps") {
    Get-ChildItem -LiteralPath $dir -Recurse -File -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -gt $runStart } |
        ForEach-Object {
            New-Item -ItemType Directory -Force -Path (Join-Path $R "crash-reports") | Out-Null
            Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $R "crash-reports") -ErrorAction SilentlyContinue
        }
}
Write-Host "scenario finished"
