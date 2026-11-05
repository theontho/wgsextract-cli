# CI entry point runs as a real new PowerShell 5.1 process, without -Command quoting.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
$nodeRoot = Join-Path ([Environment]::GetFolderPath("UserProfile")) ".local\share\gui-for-cli\node"
$node = Get-Command node
if (-not $node.Source.StartsWith("$nodeRoot\", [StringComparison]::OrdinalIgnoreCase)) {
    throw "Hosted Node shadowed setup Node: $($node.Source)"
}
Write-Host "New-shell Node: $($node.Source)"
if (-not [Environment]::GetEnvironmentVariable("UV_PYTHON_INSTALL_DIR", "User")) {
    throw "Setup did not persist UV_PYTHON_INSTALL_DIR."
}
$homeDirectory = [Environment]::GetFolderPath("UserProfile")
$toolDirectories = @{ "uv" = ".local\bin"; "cargo" = ".cargo\bin" }
foreach ($tool in @("uv", "cargo")) {
    $directory = Join-Path $homeDirectory $toolDirectories[$tool]
    $command = Get-Command $tool -CommandType Application
    if (-not $command.Source.StartsWith("$directory\", [StringComparison]::OrdinalIgnoreCase)) {
        throw "Hosted $tool shadowed setup tool: $($command.Source)"
    }
    Write-Host "New-shell ${tool}: $($command.Source)"
}
foreach ($tool in @("uv", "node", "npm.cmd", "cargo")) {
    & $tool --version
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
& powershell -NoProfile -ExecutionPolicy Bypass -File .\make.ps1 precheck
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& uv run --locked python scripts/smoke-dev.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& npm.cmd --prefix app run tauri:prepare-node
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& node app/scripts/run-tauri.mjs build --debug --no-bundle
exit $LASTEXITCODE
