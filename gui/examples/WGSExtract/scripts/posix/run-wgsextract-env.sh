#!/bin/sh
# Runs COMMAND in the WGS Extract Pixi environment installed by setup.
# Pixi resolution: an explicit $PIXI, then the Pixi embedded in the bundle
# runtime, then a Pixi on PATH or in ~/.pixi (which setup's installer reuses
# instead of embedding one when present).
set -eu

if [ "$#" -lt 1 ]; then
  printf 'Usage: %s COMMAND [ARG...]\n' "$0" >&2
  exit 64
fi

script_dir="$(CDPATH= cd "$(dirname "$0")" && pwd)"
runtime="$(CDPATH= cd "$script_dir/../.." && pwd)/runtime/wgsextract-cli"
app_dir="${WGSEXTRACT_APP_DIR:-$runtime/app}"

if [ ! -d "$app_dir" ]; then
  printf 'Error: WGS Extract bundle runtime is not installed at %s.\n' "$app_dir" >&2
  printf 'Run the WGS Extract setup action before running commands.\n' >&2
  exit 127
fi

if [ -n "${PIXI:-}" ]; then
  if [ ! -x "$PIXI" ]; then
    printf 'Error: PIXI is set but is not executable: %s\n' "$PIXI" >&2
    exit 127
  fi
  pixi="$PIXI"
elif [ -x "$runtime/.pixi/bin/pixi" ]; then
  pixi="$runtime/.pixi/bin/pixi"
  # The embedded Pixi keeps its state in the runtime, not in ~/.pixi (see setup).
  export PIXI_HOME="$runtime/.pixi"
elif command -v pixi >/dev/null 2>&1; then
  pixi="$(command -v pixi)"
elif [ -x "$HOME/.pixi/bin/pixi" ]; then
  pixi="$HOME/.pixi/bin/pixi"
else
  printf 'Error: Pixi was not found. Run the WGS Extract setup action again.\n' >&2
  exit 127
fi

# Data sources must answer within seconds, so they never let `pixi run` finish
# an interrupted install: they need the environment Pixi marks as installed
# and run it as it is.
run_flags=""
if [ "${GUI_FOR_CLI_DATA_SOURCE:-}" = "1" ]; then
  if [ ! -f "$app_dir/.pixi/envs/default/conda-meta/pixi" ]; then
    printf 'Error: WGS Extract CLI setup has not finished: its Pixi environment in %s is not installed.\n' "$app_dir" >&2
    printf 'Run the WGS Extract setup action again.\n' >&2
    exit 127
  fi
  run_flags="--no-install"
fi

# Match the launcher written by the installer, which keeps Pixi's package
# cache inside the bundle runtime.
export PIXI_CACHE_DIR="${PIXI_CACHE_DIR:-$runtime/.pixi/cache}"
cd "$app_dir"
# shellcheck disable=SC2086 # run_flags is empty or a single flag.
exec "$pixi" run $run_flags "$@"
