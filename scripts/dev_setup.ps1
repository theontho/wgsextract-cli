param(
    [switch]$SkipSmoke,
    [switch]$WebOnly
)

$ErrorActionPreference = "Stop"
$env:PYTHONIOENCODING = "utf-8"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Set-Location (Split-Path $PSScriptRoot -Parent)
New-Item -ItemType Directory -Force "tmp\dev-setup" | Out-Null
$env:TEMP = Join-Path $PWD "tmp\dev-setup"
$env:TMP = $env:TEMP
$Script:ToolsChanged = $false
$userBin = Join-Path ([Environment]::GetFolderPath("UserProfile")) ".local\bin"
$cargoBin = Join-Path ([Environment]::GetFolderPath("UserProfile")) ".cargo\bin"
$nodeRoot = Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "gui-for-cli\node"
$originalPath = $env:Path
$env:Path = "$userBin;$cargoBin;$nodeRoot;$env:Path"

function Invoke-Checked {
    param([string]$Command, [string[]]$Arguments)
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Command failed (exit $LASTEXITCODE)." }
}

function Invoke-Setup {
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "Install Git for Windows: https://git-scm.com/download/win, then rerun setup."
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Invoke-WebRequest -UseBasicParsing https://astral.sh/uv/install.ps1 -OutFile tmp\dev-setup\uv-install.ps1
    $oldInstallDir = $env:UV_INSTALL_DIR
    $env:UV_INSTALL_DIR = $userBin
    try {
        & powershell -NoProfile -ExecutionPolicy Bypass -File tmp\dev-setup\uv-install.ps1
    } finally { $env:UV_INSTALL_DIR = $oldInstallDir }
    if ($LASTEXITCODE -ne 0) { throw "uv installation failed." }
    $Script:ToolsChanged = $true
}
$node = Get-Command node -ErrorAction SilentlyContinue
$nodeMajor = 0
if ($node) { $nodeMajor = [int]((& node --version).TrimStart("v").Split(".")[0]) }
if ($nodeMajor -lt 22) {
    $version = "24.15.0"
    $arch = if ([Runtime.InteropServices.RuntimeInformation]::OSArchitecture -eq "Arm64") { "arm64" } else { "x64" }
    $archive = "node-v$version-win-$arch.zip"
    Invoke-WebRequest -UseBasicParsing "https://nodejs.org/dist/v$version/$archive" -OutFile "tmp\dev-setup\$archive"
    $sums = (Invoke-WebRequest -UseBasicParsing "https://nodejs.org/dist/v$version/SHASUMS256.txt").Content
    $expected = ($sums -split "`n" | Where-Object { $_.Trim().EndsWith("  $archive") }).Trim().Split(" ")[0]
    $sha = [Security.Cryptography.SHA256]::Create()
    $stream = [IO.File]::OpenRead((Join-Path $PWD "tmp\dev-setup\$archive"))
    try { $actual = [BitConverter]::ToString($sha.ComputeHash($stream)).Replace("-", "").ToLowerInvariant() }
    finally { $stream.Dispose(); $sha.Dispose() }
    if ($actual -ne $expected) { throw "Node archive checksum mismatch." }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $unpack = Join-Path $PWD "tmp\dev-setup\node"
    if (Test-Path $unpack) { Remove-Item -Recurse -Force $unpack }
    [IO.Compression.ZipFile]::ExtractToDirectory((Join-Path $PWD "tmp\dev-setup\$archive"), $unpack)
    New-Item -ItemType Directory -Force $nodeRoot | Out-Null
    Copy-Item "$unpack\node-v$version-win-$arch\*" $nodeRoot -Recurse -Force
    $Script:ToolsChanged = $true
}
if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) {
    $arch = if ([Runtime.InteropServices.RuntimeInformation]::OSArchitecture -eq "Arm64") { "aarch64" } else { "x86_64" }
    Invoke-WebRequest -UseBasicParsing "https://static.rust-lang.org/rustup/dist/$arch-pc-windows-msvc/rustup-init.exe" -OutFile tmp\dev-setup\rustup-init.exe
    Invoke-Checked "tmp\dev-setup\rustup-init.exe" @("-y", "--profile", "minimal", "--default-host", "$arch-pc-windows-msvc")
    $Script:ToolsChanged = $true
}
# User scope only; no elevation, winget, or machine PATH changes.
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
$originalUserPath = $userPath
foreach ($directory in @($userBin, $cargoBin, $nodeRoot)) {
    if (($userPath -split ";") -notcontains $directory) { $userPath = "$directory;$userPath" }
}
if ($userPath -ne $originalUserPath) {
    [Environment]::SetEnvironmentVariable("Path", $userPath, "User")
    $Script:ToolsChanged = $true
}
Invoke-Checked "uv" @("sync", "--locked")
$arguments = @("run", "--locked", "python", "scripts/dev_setup.py")
if ($SkipSmoke) { $arguments += "--skip-smoke" }
if ($WebOnly) { $arguments += "--web-only" }
Invoke-Checked "uv" $arguments
}

function Test-InvokingToolPaths {
    $currentPath = $env:Path
    try {
        $env:Path = $originalPath
        foreach ($tool in @("uv", "node", "cargo")) {
            if (-not (Get-Command $tool -CommandType Application -ErrorAction SilentlyContinue)) {
                return $false
            }
        }
        return $true
    } finally { $env:Path = $currentPath }
}

function Show-NodePathWarning {
    if (-not (Test-Path (Join-Path $nodeRoot "node.exe"))) { return }
    $installedVersion = [version]((& (Join-Path $nodeRoot "node.exe") --version).TrimStart("v"))
    $currentPath = $env:Path
    try {
        $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
        $winner = Get-Command node.exe -ErrorAction SilentlyContinue
        if ($winner -and -not $winner.Source.StartsWith("$nodeRoot\", [StringComparison]::OrdinalIgnoreCase)) {
            $winnerVersion = [version]((& $winner.Source --version).TrimStart("v"))
            if ($winnerVersion -lt $installedVersion) {
                Write-Warning "New-terminal PATH conflict: $($winner.Source) (v$winnerVersion) shadows setup's $nodeRoot\node.exe (v$installedVersion). Remove or upgrade the old Node, or reorder PATH so $nodeRoot comes first. For this terminal only: `$env:Path = '$nodeRoot;' + `$env:Path"
            }
        }
    } finally { $env:Path = $currentPath }
}

try {
    Invoke-Setup
} finally {
    if ($Script:ToolsChanged -or -not (Test-InvokingToolPaths)) {
        Write-Host "Open a new terminal before running powershell -ExecutionPolicy Bypass -File .\make.ps1 dev (setup changed tools/PATH, or required tools are missing from the invoking PATH)."
    }
    Show-NodePathWarning
}
exit 0
