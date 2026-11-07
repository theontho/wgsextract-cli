$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $PSCommandPath
$runtime = Join-Path $scriptDir "run-wgsextract-env.ps1"
$microarrayRefPath = $null

function Get-IndexInputMessage {
    param([Parameter(Mandatory = $true)][string]$Value)

    $lower = $Value.ToLowerInvariant()
    $craiSuffix = ".crai"
    $baiSuffix = ".bai"
    if ($lower.EndsWith($craiSuffix)) {
        $dataPath = $Value.Substring(0, $Value.Length - $craiSuffix.Length)
        return "Selected CRAM index file: $Value`nChoose the CRAM data file instead: $dataPath"
    }
    if ($lower.EndsWith(".bam$baiSuffix")) {
        $dataPath = $Value.Substring(0, $Value.Length - $baiSuffix.Length)
        return "Selected BAM index file: $Value`nChoose the BAM data file instead: $dataPath"
    }
    if ($lower.EndsWith($baiSuffix)) {
        return "Selected BAM index file: $Value`nChoose the BAM data file, not its .bai index."
    }
    return $null
}

for ($index = 0; $index -lt $args.Count; $index += 1) {
    $argument = [string]$args[$index]
    $inputPath = $null
    $refPath = $null
    if ($argument -eq "--input" -and $index + 1 -lt $args.Count) {
        $inputPath = [string]$args[$index + 1]
    } elseif ($argument.StartsWith("--input=")) {
        $inputPath = $argument.Substring("--input=".Length)
    }
    if ($argument -eq "--ref" -and $index + 1 -lt $args.Count) {
        $refPath = [string]$args[$index + 1]
    } elseif ($argument.StartsWith("--ref=")) {
        $refPath = $argument.Substring("--ref=".Length)
    }

    if ($null -ne $inputPath) {
        $message = Get-IndexInputMessage -Value $inputPath
        if ($message) {
            [Console]::Error.WriteLine($message)
            exit 1
        }
    }
    if ($null -ne $refPath) {
        $microarrayRefPath = $refPath
    }
}

if ($args.Count -gt 0 -and [string]$args[0] -eq "microarray") {
    if (-not $microarrayRefPath) {
        [Console]::Error.WriteLine("Reference genome is required before generating microarray kits.")
        [Console]::Error.WriteLine("Install/download the reference library from the Library page or rerun setup, then choose an existing reference FASTA.")
        exit 1
    }
    if (Test-Path -LiteralPath $microarrayRefPath -PathType Container) {
        [Console]::Error.WriteLine("Reference genome must be a FASTA file, not the reference library directory: $microarrayRefPath")
        [Console]::Error.WriteLine("Choose an installed reference FASTA from the Reference genome dropdown on the Microarray page.")
        exit 1
    }
    if (-not (Test-Path -LiteralPath $microarrayRefPath -PathType Leaf)) {
        [Console]::Error.WriteLine("Reference genome was not found: $microarrayRefPath")
        [Console]::Error.WriteLine("Install/download the reference library from the Library page or rerun setup, then choose an existing reference FASTA.")
        exit 1
    }
}

# The bundle's settings file is this runtime's config.toml, so what the CLI saves
# (ref bootstrap's reference_library, for one) stays in the workspace instead of
# the user's own CLI config, and leaves with the app.
$workspace = if ($env:GUI_FOR_CLI_BUNDLE_WORKSPACE) { $env:GUI_FOR_CLI_BUNDLE_WORKSPACE } else { Split-Path -Parent (Split-Path -Parent $scriptDir) }
$configPath = if ($env:GUI_FOR_CLI_CONFIG_PATH) { $env:GUI_FOR_CLI_CONFIG_PATH } else { Join-Path $workspace "settings\config.toml" }
if ($env:WGSEXTRACT_FORWARD_STDIN -eq "1") {
    $input | & $runtime wgsextract --config $configPath @args
} else {
    & $runtime wgsextract --config $configPath @args
}
exit $LASTEXITCODE
