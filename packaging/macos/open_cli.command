#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_BUNDLE="${SCRIPT_DIR}/WGS Extract.app"

if [[ ! -d "${APP_BUNDLE}" ]]; then
    APP_BUNDLE="/Applications/WGS Extract.app"
fi

if [[ ! -d "${APP_BUNDLE}" ]]; then
    printf '%s\n' "WGS Extract.app was not found next to this helper or in /Applications."
    printf '%s\n' "Drag WGS Extract.app into Applications, then run this helper again."
    read -r -p "Press Return to close... " _
    exit 1
fi

WGSE_CONTENTS_DIR="${APP_BUNDLE}/Contents"
WGSE_RESOURCES_DIR="${WGSE_CONTENTS_DIR}/Resources"
source "${WGSE_RESOURCES_DIR}/app_env.sh"

wgse_init_logging
wgse_log "Opening CLI helper for ${APP_BUNDLE}"

PIXI_BIN="$(wgse_find_pixi || true)"
if [[ -z "${PIXI_BIN}" ]]; then
    wgse_print_pixi_help
    read -r -p "Press Return to close... " _
    exit 1
fi

APP_PROJECT_DIR="$(wgse_prepare_runtime "${WGSE_RESOURCES_DIR}/app" "${WGSE_CONTENTS_DIR}/Info.plist")"
SESSION_DIR="$(mktemp -d "${TMPDIR:-/tmp}/wgsextract-cli.XXXXXX")"

cat >"${SESSION_DIR}/.zshrc" <<EOF
function wgsextract() {
    "${PIXI_BIN}" run --manifest-path "${APP_PROJECT_DIR}/pixi.toml" wgsextract "\$@"
}

cd "${APP_PROJECT_DIR}"
printf '%s\\n' 'WGS Extract CLI shell'
printf '%s\\n' 'Use: wgsextract --help'
printf '%s\\n' 'Use: wgsextract gui --desktop'
printf '%s\\n' 'Runtime: ${APP_PROJECT_DIR}'
printf '%s\\n' 'Type exit to close this shell.'
EOF

export PATH="${HOME}/.pixi/bin:${HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:${PATH}"
export ZDOTDIR="${SESSION_DIR}"
exec /bin/zsh -i
