#!/bin/bash

set -euo pipefail

WGSE_APP_SUPPORT_DIR="${HOME}/Library/Application Support/WGS Extract"
WGSE_LOG_DIR="${HOME}/Library/Logs/WGS Extract"
WGSE_LOG_FILE=""

wgse_init_logging() {
    mkdir -p "${WGSE_LOG_DIR}"
    WGSE_LOG_FILE="${WGSE_LOG_DIR}/launcher-$(date +%Y%m%d-%H%M%S).log"
    touch "${WGSE_LOG_FILE}"
}

wgse_log() {
    local message="$1"
    if [[ -n "${WGSE_LOG_FILE}" ]]; then
        printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "${message}" >>"${WGSE_LOG_FILE}"
    fi
}

wgse_dialog() {
    local title="$1"
    local message="$2"
    /usr/bin/osascript <<OSA >/dev/null 2>&1 || true
display dialog "${message}" with title "${title}" buttons {"OK"} default button "OK"
OSA
}

wgse_notify() {
    local title="$1"
    local message="$2"
    /usr/bin/osascript <<OSA >/dev/null 2>&1 || true
display notification "${message}" with title "${title}"
OSA
}

wgse_find_pixi() {
    local candidates=(
        "${WGSE_RESOURCES_DIR:-}/bin/pixi"
        "${HOME}/.pixi/bin/pixi"
        "${HOME}/.local/bin/pixi"
        "/opt/homebrew/bin/pixi"
        "/usr/local/bin/pixi"
    )

    local candidate
    for candidate in "${candidates[@]}"; do
        if [[ -n "${candidate}" && -x "${candidate}" ]]; then
            printf '%s\n' "${candidate}"
            return 0
        fi
    done

    if command -v pixi >/dev/null 2>&1; then
        command -v pixi
        return 0
    fi

    return 1
}

wgse_bundle_version() {
    local info_plist="$1"
    local version=""

    if [[ -f "${info_plist}" ]]; then
        version="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "${info_plist}" 2>/dev/null || true)"
    fi

    if [[ -z "${version}" ]]; then
        version="dev"
    fi

    printf '%s\n' "${version}"
}

wgse_prepare_runtime() {
    local template_dir="$1"
    local info_plist="$2"
    local version
    version="$(wgse_bundle_version "${info_plist}")"

    local runtime_root="${WGSE_APP_SUPPORT_DIR}/runtime/${version}"
    local runtime_project="${runtime_root}/app"

    mkdir -p "${runtime_root}"

    local template_marker="${template_dir}/.wgsextract-app-build"
    local runtime_marker="${runtime_project}/.wgsextract-app-build"

    if [[ ! -f "${runtime_project}/pixi.toml" || ! -f "${runtime_project}/pyproject.toml" || ! -f "${runtime_marker}" || ! -f "${template_marker}" ]] || ! cmp -s "${template_marker}" "${runtime_marker}"; then
        wgse_log "Preparing writable runtime at ${runtime_project}"
        rm -rf "${runtime_project}"
        /usr/bin/ditto "${template_dir}" "${runtime_project}"
    else
        wgse_log "Using existing runtime at ${runtime_project}"
    fi

    printf '%s\n' "${runtime_project}"
}

wgse_print_pixi_help() {
    printf '%s\n' "Pixi is required to run WGS Extract."
    printf '%s\n' "Install Pixi from https://pixi.sh, then reopen WGS Extract."
    printf '%s\n' "macOS install command: curl -fsSL https://pixi.sh/install.sh | bash"
}
