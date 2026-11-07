$ErrorActionPreference = "Stop"

$workspace = if ($env:GUI_FOR_CLI_BUNDLE_WORKSPACE) { $env:GUI_FOR_CLI_BUNDLE_WORKSPACE } else { (Get-Location).Path }
$configPath = if ($env:GUI_FOR_CLI_CONFIG_PATH) { $env:GUI_FOR_CLI_CONFIG_PATH } else { Join-Path $workspace "settings\config.toml" }
$outputPath = Join-Path $workspace "output"
$referencePath = Join-Path $workspace "reference"
$genomeLibraryPath = Join-Path $workspace "genomes"
$tempPath = Join-Path $workspace "temp"
$logPath = Join-Path $workspace "logs"
# The CLI requires its temporary directory to exist already.
New-Item -ItemType Directory -Force -Path $tempPath, $logPath | Out-Null

function ConvertTo-TomlBasicString {
    param([string]$Value)
    return $Value.Replace("\", "\\").Replace('"', '\"')
}

$tomlOutputPath = ConvertTo-TomlBasicString $outputPath
$tomlReferencePath = ConvertTo-TomlBasicString $referencePath
$tomlGenomeLibraryPath = ConvertTo-TomlBasicString $genomeLibraryPath
$tomlTempPath = ConvertTo-TomlBasicString $tempPath
$tomlLogPath = ConvertTo-TomlBasicString $logPath

[ordered]@{
    path = $configPath
    contents = "output_directory = `"$tomlOutputPath`"`nreference_library = `"$tomlReferencePath`"`ngenome_library = `"$tomlGenomeLibraryPath`"`ntemp_directory = `"$tomlTempPath`"`nlog_dir = `"$tomlLogPath`"`nreference_fasta = `"`"`ndefault_input_vcf = `"`"`nmother_vcf_path = `"`"`nfather_vcf_path = `"`"`nyleaf_executable = `"`"`nhaplogrep_executable = `"`"`n"
} | ConvertTo-Json -Compress
