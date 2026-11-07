#!/bin/sh
set -eu

workspace="${GUI_FOR_CLI_BUNDLE_WORKSPACE:-$(pwd)}"
config_path="${GUI_FOR_CLI_CONFIG_PATH:-$workspace/settings/config.toml}"
output_path="$workspace/output"
reference_path="$workspace/reference"
genome_library_path="$workspace/genomes"

json_escape() {
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

printf '{\n'
printf '  "path": "%s",\n' "$(json_escape "$config_path")"
printf '  "contents": "output_directory = \\"%s\\"\\nreference_library = \\"%s\\"\\ngenome_library = \\"%s\\"\\nreference_fasta = \\"\\"\\ndefault_input_vcf = \\"\\"\\nmother_vcf_path = \\"\\"\\nfather_vcf_path = \\"\\"\\nyleaf_executable = \\"\\"\\nhaplogrep_executable = \\"\\"\\n"\n' \
  "$(json_escape "$output_path")" \
  "$(json_escape "$reference_path")" \
  "$(json_escape "$genome_library_path")"
printf '}\n'
