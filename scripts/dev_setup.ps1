# One-command developer setup for a source checkout on Windows:
#
#   powershell -ExecutionPolicy Bypass -File scripts\dev_setup.ps1 [-SkipTests]
#
# Installs Pixi and `just` for the current user when they're missing, installs
# the default and dev Pixi environments from the lock file, sets up the
# pre-commit hooks, and checks that the CLI, its tools and a quick test suite
# work. Safe to rerun. macOS and Linux: scripts/dev_setup.sh.
[CmdletBinding()]
param([switch]$SkipTests)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Step([string]$Message) { Write-Host "`n==> $Message" }
function Note([string]$Message) { Write-Host "    $Message" }
function Invoke-Checked {
    param([string]$Command, [string[]]$Arguments)
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) { throw "'$Command $($Arguments -join ' ')' failed with exit code $LASTEXITCODE" }
}

Step "Checking prerequisites"
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "git is required: https://git-scm.com/download/win"
}
Note (git --version)

Step "Pixi"
$OrigPath = $env:PATH
& (Join-Path $PSScriptRoot "bootstrap_windows_prereqs.ps1")
$env:PATH = "$env:USERPROFILE\.pixi\bin;$env:PATH"
if (-not (Get-Command pixi -ErrorAction SilentlyContinue)) {
    throw "Pixi isn't on PATH; open a new terminal and rerun."
}
Note (pixi --version)

Step "Installing the default and dev environments (first run downloads a few GB)"
Invoke-Checked pixi @("install", "--locked")
Invoke-Checked pixi @("install", "--locked", "-e", "dev")

Step "just (task runner for the recipes in justfile)"
if (-not (Get-Command just -ErrorAction SilentlyContinue)) {
    Note "Installing just with 'pixi global install just'"
    Invoke-Checked pixi @("global", "install", "just")
}
Note (just --version)

if (Test-Path -LiteralPath (Join-Path $Root ".git")) {
    Step "Installing pre-commit hooks"
    Invoke-Checked pixi @("run", "pre-commit", "install")
}

Step "Checking the CLI and its tools"
Invoke-Checked pixi @("run", "wgsextract", "--version")
Invoke-Checked pixi @("run", "wgsextract", "deps", "check")

if (-not $SkipTests) {
    Step "Running the quick smoke tests"
    Invoke-Checked pixi @("run", "pytest", "tests/test_smoke.py", "-q")
}

Step "Optional tools for contributing"
$missing = $false
if (-not (git config user.email)) {
    Note "Set your git identity: git config --global user.name 'Your Name'; git config --global user.email you@example.com"
    $missing = $true
}
if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    Note "GitHub CLI (pull requests, CI logs): winget install GitHub.cli, then 'gh auth login'"
    $missing = $true
}
else {
    # Windows PowerShell 5.1 turns native stderr into terminating errors under "Stop".
    $ErrorActionPreference = "Continue"
    gh auth status *> $null
    $ghOk = $LASTEXITCODE -eq 0
    $ErrorActionPreference = "Stop"
    if (-not $ghOk) { Note "Log in to GitHub: gh auth login"; $missing = $true }
}
if (-not (Get-Command copilot -ErrorAction SilentlyContinue) -and -not (Get-Command claude -ErrorAction SilentlyContinue)) {
    Note "An AI coding agent, e.g. GitHub Copilot CLI: npm install -g @github/copilot"
    $missing = $true
}
if (-not $missing) { Note "All present." }

$justOnOrigPath = $OrigPath -split ";" | Where-Object { $_ -and (Test-Path -LiteralPath (Join-Path $_ "just.exe")) }
if (-not $justOnOrigPath) {
    Write-Host "`nOpen a new terminal before running the commands below (or add $env:USERPROFILE\.pixi\bin to PATH)."
}

Write-Host @"

Setup complete. Common commands (run 'just' for the full list):
  just run --help    run the CLI from this checkout
  just test          unit and integration tests
  just check         format, lint, typecheck and test
  just ci            the checks CI runs; run before opening a pull request
See docs/developer_setup.md for the change-to-pull-request walkthrough.
"@

exit 0
