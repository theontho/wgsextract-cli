param(
    [Parameter(Position = 0)]
    [string]$Action = "help",
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$RunnerArgs = @()
)

$ErrorActionPreference = "Stop"
$env:PYTHONIOENCODING = "utf-8"
Set-Location $PSScriptRoot

function Show-Help {
    @"
Usage: powershell -ExecutionPolicy Bypass -File .\make.ps1 <target> [NAME=value ...]

  setup              Bootstrap user tools, locked dependencies/hooks, and smoke test.
                     Optional: -SkipSmoke -WebOnly
  precheck           Verify development tools and repository setup.
  worktree-setup     Create a developer worktree (BRANCH=name).
  worktree-teardown  Remove a registered worktree (BRANCH=name).
  run                Build and serve the Web UI (BUNDLE=path PORT=port).
  dev                Serve the Web UI with watch and reload.
  tauri              Run the Tauri desktop app.
  build              Compile the Web UI.
  build-tauri        Build the Tauri desktop app.
  lint               Typecheck, validate bundles/locales, check duplication.
  test               Run TypeScript and Python tests.
  ci                 Run lint and tests.
  package            Build the Tauri NSIS installer.
  clean              Remove build outputs.
"@
}

function Invoke-PythonTool {
    param([string[]]$Arguments)
    $homeDirectory = [Environment]::GetFolderPath("UserProfile")
    $nodeRoot = Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "gui-for-cli\node"
    $env:Path = "$homeDirectory\.local\bin;$homeDirectory\.cargo\bin;$nodeRoot;$env:Path"
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        throw "Run powershell -ExecutionPolicy Bypass -File .\make.ps1 setup first (uv is missing)."
    }
    foreach ($item in $RunnerArgs) {
        if ($item -match "^(?<name>[A-Za-z_][A-Za-z0-9_]*)=(?<value>.*)$") {
            Set-Item -Path "Env:\$($Matches.name)" -Value $Matches.value
        } else { $Arguments += $item }
    }
    & uv run --locked python @Arguments
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

$targets = @{
    "run" = @("run", "web")
    "dev" = @("run", "dev")
    "tauri" = @("run", "tauri")
    "build" = @("build", "web")
    "build-tauri" = @("build", "tauri")
    "lint" = @("lint")
    "test" = @("test")
    "ci" = @("ci")
    "package" = @("package", "tauri")
    "clean" = @("clean")
}
switch ($Action) {
    "help" { Show-Help }
    "setup" {
        # Explicit child process preserves native exit status under Windows PowerShell 5.1.
        & powershell -NoProfile -ExecutionPolicy Bypass -File scripts\dev_setup.ps1 @RunnerArgs
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    "precheck" { Invoke-PythonTool @("tools/precheck.py") }
    "worktree-setup" { Invoke-PythonTool @("tools/worktree.py", "setup") }
    "worktree-teardown" { Invoke-PythonTool @("tools/worktree.py", "teardown") }
    default {
        if (-not $targets.ContainsKey($Action)) {
            Write-Host "Unknown target: $Action"
            Show-Help
            exit 2
        }
        Invoke-PythonTool (@("tools/tasks.py") + $targets[$Action])
    }
}
exit 0
