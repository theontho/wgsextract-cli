# Install-lab guest scenario for Windows: what a new user does, from a clean PC.
#
# baseline snapshot -> download install_windows.bat into an empty folder and run
# it -> snapshot -> real workload -> rerun the installer (update) -> quick
# re-check -> uninstall_windows.bat --remove-config --remove-prerequisites,
# delete the folder ->
# snapshot
#
# Inputs: a JSON file with the same LAB_* settings as guest/scenario.sh, with
# LAB_INSTALLER_URL pointing at install_windows.bat and LAB_ARCHIVE_URL at a
# .zip source archive. Nothing here decides pass/fail; the host's oracle does.
param([Parameter(Mandatory = $true)][string]$EnvFile)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
. (Join-Path $PSScriptRoot "lab-common.ps1")

$lab = Get-Content -LiteralPath $EnvFile -Raw | ConvertFrom-Json
$R = $lab.LAB_RESULTS
$Evidence = Join-Path $R "evidence"
New-Item -ItemType Directory -Force -Path $Evidence | Out-Null
Write-Text (Join-Path $R "nonce") $lab.LAB_NONCE
$Sabotage = "$($lab.LAB_SABOTAGE)"
$Mode = if ($lab.LAB_MODE) { $lab.LAB_MODE } else { "install" }
$InstallDir = Join-Path $env:USERPROFILE "wgsextract-cli"
$DataParent = Join-Path $env:USERPROFILE "lab-data"
$Inventory = Join-Path $PSScriptRoot "inventory.ps1"

function Save-Snapshot([string]$Name) {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Inventory (Join-Path $R "inventory-$Name.txt") $R
}

function Step([string]$Name, [string[]]$Command, [string]$Cwd = $env:USERPROFILE) {
    Invoke-Step -Evidence $Evidence -Name $Name -WorkingDirectory $Cwd -Command $Command | Out-Null
}

Save-Snapshot "baseline"
# Tools the installer must provide must not be reachable before it runs.
$survey = foreach ($tool in "pixi", "samtools", "bcftools", "java", "python", "conda", "mamba", "bash", "gcc", "perl") {
    $found = (Get-Command $tool -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1).Source
    # Windows ships App Execution Alias stubs (python.exe opens the Store) and
    # WSL's bash.exe; they aren't usable tools unless they actually run.
    if ($found -and $found -like "*\WindowsApps\*") { $found = "" }
    if ($found -and $tool -eq "bash" -and $found -ieq "$env:SystemRoot\System32\bash.exe") { $found = "" }
    "$tool`t$found"
}
if (Test-Path -LiteralPath "C:\msys64") { $survey += "msys2`tC:\msys64" } else { $survey += "msys2`t" }
# Installers reuse a Pixi in its default place even when PATH doesn't reach it.
$pixiHome = Join-Path $env:USERPROFILE ".pixi\bin\pixi.exe"
$survey += if (Test-Path -LiteralPath $pixiHome) { "pixi-home`t$pixiHome" } else { "pixi-home`t" }
[System.IO.File]::WriteAllLines((Join-Path $R "preexisting-tools.txt"), [string[]]$survey, $Utf8)

if ($Mode -eq "control") {
    Start-Sleep -Seconds 60
    Save-Snapshot "after_install"
    Start-Sleep -Seconds 60
    Save-Snapshot "after_uninstall"
    Write-Host "scenario finished"
    exit 0
}

# The documented path: put install_windows.bat in an empty folder and run it.
$env:WGSEXTRACT_ARCHIVE_URL = $lab.LAB_ARCHIVE_URL
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$installer = Join-Path $InstallDir "install_windows.bat"
if ($Sabotage -eq "noop-installer") {
    Step "install" @("cmd.exe", "/c", "exit 0")
} else {
    Step "installer_download" @("curl.exe", "-fsSL", "-o", $installer, $lab.LAB_INSTALLER_URL) $InstallDir
    Step "install" @($installer) $InstallDir
}
Update-SessionEnvironment
Save-Snapshot "after_install"

$datasetArchive = Join-Path $DataParent "dataset.tar.gz"
New-Item -ItemType Directory -Force -Path $DataParent | Out-Null
Step "dataset" @("curl.exe", "-fsSL", "-o", $datasetArchive, $lab.LAB_DATASET_URL)
$actual = (Get-FileHash -LiteralPath $datasetArchive -Algorithm SHA256 -ErrorAction SilentlyContinue).Hash
if ($actual -ine $lab.LAB_DATASET_SHA256) {
    Write-Text (Join-Path $Evidence "dataset.rc") "1"
    Add-Content -LiteralPath (Join-Path $Evidence "dataset.log") "sha256 mismatch: $actual"
} else {
    Step "dataset_extract" @("tar.exe", "-xzf", $datasetArchive, "-C", $DataParent)
    if ((Get-Content (Join-Path $Evidence "dataset_extract.rc")) -ne "0") {
        Write-Text (Join-Path $Evidence "dataset.rc") "1"
    }
}

switch ($Sabotage) {
    "fake-launcher" {
        # A pixi that claims success without doing anything.
        $pixiBin = Join-Path $env:USERPROFILE ".pixi\bin"
        Remove-Item -LiteralPath (Join-Path $pixiBin "pixi.exe") -Force -ErrorAction SilentlyContinue
        Set-Content -LiteralPath (Join-Path $pixiBin "pixi.cmd") -Value "@exit /b 0" -Encoding ascii
    }
    "break-samtools" {
        Remove-Item -LiteralPath "C:\msys64\ucrt64\bin\samtools.exe" -Force -ErrorAction SilentlyContinue
    }
}

$Data = Join-Path $DataParent "hg19-mini-hg00096"
Step "workload" @("powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
    (Join-Path $PSScriptRoot "workload.ps1"), $InstallDir, $Data, (Join-Path $R "workload"))

# Rerunning the installer must keep user data in the install folder.
New-Item -ItemType Directory -Force -Path (Join-Path $InstallDir "reference") | Out-Null
Write-Text (Join-Path $InstallDir "reference\lab-marker") $lab.LAB_NONCE
if ($Sabotage -eq "noop-installer") {
    Step "update" @("cmd.exe", "/c", "exit 0")
} else {
    Step "update" @($installer) $InstallDir
}
Update-SessionEnvironment
Step "update_kept_data" @("type", (Join-Path $InstallDir "reference\lab-marker"))
Step "post_update_mt" @("pixi", "run", "wgsextract", "lineage", "mt-haplogroup",
    "--input", (Join-Path $Data "HG00096.hg19-mini.bam"), "--ref", (Join-Path $Data "hg19-mini.fa.gz"),
    "--outdir", (Join-Path $R "post_update_mt")) $InstallDir

if ($Sabotage -ne "skip-uninstall") {
    Step "uninstall" @((Join-Path $InstallDir "uninstall_windows.bat"), "--yes", "--remove-config", "--remove-prerequisites") $InstallDir
    # The user downloaded the folder, so deleting it is theirs to do; everything
    # outside it must already be gone.
    Step "remove_install_dir" @("rmdir", "/s", "/q", $InstallDir)
}
if ($Sabotage -eq "leftover") {
    New-Item -ItemType Directory -Force -Path (Join-Path $env:USERPROFILE ".wgsextract-leftover") | Out-Null
}
Remove-Item -LiteralPath $DataParent -Recurse -Force -ErrorAction SilentlyContinue
Update-SessionEnvironment
Save-Snapshot "after_uninstall"
# Crash reports written during the run, for the host to read (the oracle fails
# the run on any; the VM is gone afterwards).
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
