#!/bin/sh
# Reports which Pixi the WGS Extract installer will use: an explicit $PIXI,
# then one on PATH or in ~/.pixi; with none, it embeds Pixi in the bundle
# runtime. Fails only for an unusable $PIXI, which the installer rejects too.
set -eu

if [ -n "${PIXI:-}" ]; then
  if [ ! -x "$PIXI" ]; then
    printf 'Error: PIXI is set but is not executable: %s\n' "$PIXI" >&2
    exit 1
  fi
  printf 'Setup will use the Pixi set in PIXI: %s\n' "$PIXI"
elif command -v pixi >/dev/null 2>&1; then
  printf 'Setup will reuse the Pixi on PATH: %s\n' "$(command -v pixi)"
elif [ -x "$HOME/.pixi/bin/pixi" ]; then
  printf 'Setup will reuse the Pixi in ~/.pixi: %s\n' "$HOME/.pixi/bin/pixi"
else
  printf 'Pixi is not pre-installed; setup will install it inside the bundle runtime.\n'
fi
