#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WGSE_APP_BUNDLE="$(cd "${SCRIPT_DIR}/../.." && pwd)"
WGSE_CONTENTS_DIR="${WGSE_APP_BUNDLE}/Contents"
WGSE_RESOURCES_DIR="${WGSE_CONTENTS_DIR}/Resources"

source "${WGSE_RESOURCES_DIR}/app_env.sh"

wgse_init_logging
wgse_log "Launching WGS Extract from ${WGSE_APP_BUNDLE}"

PIXI_BIN="$(wgse_find_pixi || true)"
if [[ -z "${PIXI_BIN}" ]]; then
    wgse_log "Pixi was not found"
    wgse_dialog "WGS Extract" "Pixi is required to run WGS Extract. Install Pixi from https://pixi.sh, then reopen this app."
    exit 1
fi

wgse_log "Using Pixi at ${PIXI_BIN}"
"${PIXI_BIN}" --version >>"${WGSE_LOG_FILE}" 2>&1 || true

APP_PROJECT_DIR="$(wgse_prepare_runtime "${WGSE_RESOURCES_DIR}/app" "${WGSE_CONTENTS_DIR}/Info.plist")"
export PATH="${HOME}/.pixi/bin:${HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:${PATH}"
export PYTHONUNBUFFERED=1

wgse_notify "WGS Extract" "Starting the desktop GUI. First launch may take several minutes while Pixi prepares the runtime."
wgse_log "Running desktop GUI from ${APP_PROJECT_DIR}"

cd "${APP_PROJECT_DIR}"
set +e
"${PIXI_BIN}" run --manifest-path "${APP_PROJECT_DIR}/pixi.toml" wgsextract gui --desktop >>"${WGSE_LOG_FILE}" 2>&1
EXIT_CODE=$?
set -e

if [[ "${EXIT_CODE}" -ne 0 ]]; then
    wgse_log "WGS Extract exited with code ${EXIT_CODE}"
    wgse_dialog "WGS Extract" "WGS Extract exited with code ${EXIT_CODE}. See the launcher log for details: ${WGSE_LOG_FILE}"
fi

exit "${EXIT_CODE}"
