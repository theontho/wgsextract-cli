#!/bin/sh
set -eu

script_dir="$(CDPATH= cd "$(dirname "$0")" && pwd)"

index_input_message() {
  case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
    *.crai)
      data_path=${1%.[Cc][Rr][Aa][Ii]}
      printf 'Selected CRAM index file: %s\nChoose the CRAM data file instead: %s\n' "$1" "$data_path" >&2
      return 0
      ;;
    *.bam.bai)
      data_path=${1%.[Bb][Aa][Ii]}
      printf 'Selected BAM index file: %s\nChoose the BAM data file instead: %s\n' "$1" "$data_path" >&2
      return 0
      ;;
    *.bai)
      printf 'Selected BAM index file: %s\nChoose the BAM data file, not its .bai index.\n' "$1" >&2
      return 0
      ;;
  esac
  return 1
}

previous=""
microarray_ref_path=""
for argument in "$@"; do
  input_path=""
  if [ "$previous" = "--input" ]; then
    input_path="$argument"
  elif [ "$previous" = "--ref" ]; then
    microarray_ref_path="$argument"
  else
    case "$argument" in
      --input=*) input_path="${argument#--input=}" ;;
      --ref=*) microarray_ref_path="${argument#--ref=}" ;;
    esac
  fi
  if [ -n "$input_path" ] && index_input_message "$input_path"; then
    exit 1
  fi
  previous="$argument"
done

if [ "${1:-}" = "microarray" ]; then
  if [ -z "$microarray_ref_path" ]; then
    printf '%s\n' "Reference genome is required before generating microarray kits." >&2
    printf '%s\n' "Install/download the reference library from the Library page or rerun setup, then choose an existing reference FASTA." >&2
    exit 1
  fi
  if [ -d "$microarray_ref_path" ]; then
    printf 'Reference genome must be a FASTA file, not the reference library directory: %s\n' "$microarray_ref_path" >&2
    printf '%s\n' "Choose an installed reference FASTA from the Reference genome dropdown on the Microarray page." >&2
    exit 1
  fi
  if [ ! -f "$microarray_ref_path" ]; then
    printf 'Reference genome was not found: %s\n' "$microarray_ref_path" >&2
    printf '%s\n' "Install/download the reference library from the Library page or rerun setup, then choose an existing reference FASTA." >&2
    exit 1
  fi
fi

# The bundle's settings file is this runtime's config.toml, so what the CLI saves
# (ref bootstrap's reference_library, for one) stays in the workspace instead of
# the user's own CLI config, and leaves with the app.
workspace="${GUI_FOR_CLI_BUNDLE_WORKSPACE:-$(CDPATH= cd "$script_dir/../.." && pwd)}"
config_path="${GUI_FOR_CLI_CONFIG_PATH:-$workspace/settings/config.toml}"
exec sh "$script_dir/run-wgsextract-env.sh" wgsextract --config "$config_path" "$@"
