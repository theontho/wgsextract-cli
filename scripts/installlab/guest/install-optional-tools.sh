#!/bin/sh
# Explicit lab provisioning, equivalent to the user's optional install command.
set -eu
support="$1"
manifest="$(find "$support" -path '*/app/pixi.toml' -type f | head -n 1)"
[ -n "$manifest" ] || { echo "No installed CLI manifest under $support" >&2; exit 1; }
install_dir="$(dirname "$(dirname "$manifest")")"
pixi="$install_dir/.pixi/bin/pixi"
if [ ! -x "$pixi" ]; then
    pixi="$(command -v pixi || printf '%s' "$HOME/.pixi/bin/pixi")"
fi
PIXI_CACHE_DIR="$install_dir/.pixi/cache" \
RATTLER_CACHE_DIR="$install_dir/.pixi/cache/rattler" \
    "$pixi" install --locked --manifest-path "$manifest" -e gatk
