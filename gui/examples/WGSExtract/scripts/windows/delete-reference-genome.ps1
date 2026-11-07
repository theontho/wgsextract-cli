$ErrorActionPreference = "Stop"

$library = if ($args.Count -gt 0) { $args[0] } else { "" }
if (-not $library) {
    $workspace = if ($env:GUI_FOR_CLI_BUNDLE_WORKSPACE) { $env:GUI_FOR_CLI_BUNDLE_WORKSPACE } else { (Get-Location).Path }
    $library = Join-Path $workspace "reference"
}
$final = if ($args.Count -gt 1) { $args[1] } else { "" }
if (-not $final -or $final.Contains("/") -or $final.Contains("\") -or $final.Contains("..")) {
    Write-Error "Invalid reference genome file name: $final"
    exit 2
}

$targetDir = Join-Path $library "genomes"
if (-not (Test-Path -LiteralPath $targetDir -PathType Container)) {
    Write-Error "Reference genome directory does not exist: $targetDir"
    exit 2
}

$canonicalGenomes = [System.IO.Path]::GetFullPath($targetDir)
$target = Join-Path $targetDir $final
$deleted = $false
$suffixes = @("", ".partial", ".fai", ".gzi", ".dict", ".amb", ".ann", ".bwt", ".pac", ".sa")
foreach ($suffix in $suffixes) {
    $candidate = "$target$suffix"
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
        continue
    }
    $canonical = [System.IO.Path]::GetFullPath($candidate)
    if (-not $canonical.StartsWith($canonicalGenomes + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
        Write-Error "Refusing to delete outside the reference library: $candidate"
        exit 2
    }
    Remove-Item -LiteralPath $canonical -Force
    "Deleted $canonical"
    $deleted = $true
}

$short = Join-Path (Split-Path -Parent $target) ([System.IO.Path]::GetFileNameWithoutExtension($target))
$dict = "$short.dict"
if (Test-Path -LiteralPath $dict -PathType Leaf) {
    $canonicalDict = [System.IO.Path]::GetFullPath($dict)
    if (-not $canonicalDict.StartsWith($canonicalGenomes + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
        Write-Error "Refusing to delete outside the reference library: $dict"
        exit 2
    }
    Remove-Item -LiteralPath $canonicalDict -Force
    "Deleted $canonicalDict"
    $deleted = $true
}

if (-not $deleted) {
    Write-Warning "No files found for $target"
}
