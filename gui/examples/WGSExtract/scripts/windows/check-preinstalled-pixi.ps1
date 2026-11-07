# Reports the Pixi and MSYS2 that setup finds. install_windows.bat reuses a Pixi
# on PATH or in ~\.pixi and installs one into ~\.pixi otherwise; $env:PIXI
# overrides the Pixi that commands run with, so an unusable one fails here.
$ErrorActionPreference = "Stop"

function Report-Pixi {
    if ($env:PIXI) {
        if (-not (Test-Path -LiteralPath $env:PIXI -PathType Leaf)) {
            [Console]::Error.WriteLine("PIXI is set but is not a file: $env:PIXI")
            exit 1
        }
        Write-Host "Commands will run with the Pixi set in PIXI: $env:PIXI"
        return
    }
    $command = Get-Command pixi -ErrorAction SilentlyContinue
    if ($command) {
        Write-Host "Setup will reuse the Pixi on PATH: $($command.Source)"
        return
    }
    $homePixi = Join-Path $HOME ".pixi\bin\pixi.exe"
    if (Test-Path -LiteralPath $homePixi -PathType Leaf) {
        Write-Host "Setup will reuse the Pixi in ~\.pixi: $homePixi"
    } else {
        Write-Host "Pixi is not pre-installed; setup will install it into ~\.pixi."
    }
}

function Report-Msys2 {
    $candidates = @()
    . (Join-Path $PSScriptRoot "wgsextract-msys2.ps1")
    $roots = @(Get-WGSExtractMsys2Root)
    foreach ($root in $roots) {
        if ($root -and (Test-Path -LiteralPath (Join-Path $root "usr\bin\pacman.exe") -PathType Leaf)) {
            $candidates += $root
        }
    }
    $pacmanCmd = Get-Command pacman.exe -ErrorAction SilentlyContinue
    if ($pacmanCmd) {
        $root = (Resolve-Path (Join-Path (Split-Path -Parent $pacmanCmd.Source) "..\..")).Path.TrimEnd('\')
        if ($candidates -notcontains $root) { $candidates += $root }
    }
    if ($candidates.Count -gt 0) {
        Write-Host "MSYS2 is pre-installed: $($candidates[0])"
    } else {
        Write-Host "MSYS2 is not pre-installed; setup will install it if needed."
    }
}

Report-Pixi
Report-Msys2
exit 0
