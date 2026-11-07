$ErrorActionPreference = "Stop"

$workspace = if ($env:GUI_FOR_CLI_BUNDLE_WORKSPACE) { $env:GUI_FOR_CLI_BUNDLE_WORKSPACE } else { (Get-Location).Path }
$configPath = if ($env:GUI_FOR_CLI_CONFIG_PATH) { $env:GUI_FOR_CLI_CONFIG_PATH } else { Join-Path $workspace "settings\config.toml" }
$outputPath = Join-Path $workspace "output"
$referencePath = Join-Path $workspace "reference"
$genomeLibraryPath = Join-Path $workspace "genomes"

function ConvertTo-TomlBasicString {
    param([string]$Value)
    return $Value.Replace("\", "\\").Replace('"', '\"')
}

$tomlOutputPath = ConvertTo-TomlBasicString $outputPath
$tomlReferencePath = ConvertTo-TomlBasicString $referencePath
$tomlGenomeLibraryPath = ConvertTo-TomlBasicString $genomeLibraryPath

[ordered]@{
    path = $configPath
    contents = "output_directory = `"$tomlOutputPath`"`nreference_library = `"$tomlReferencePath`"`ngenome_library = `"$tomlGenomeLibraryPath`"`nreference_fasta = `"`"`ndefault_input_vcf = `"`"`nmother_vcf_path = `"`"`nfather_vcf_path = `"`"`nyleaf_executable = `"`"`nhaplogrep_executable = `"`"`n"
} | ConvertTo-Json -Compress
