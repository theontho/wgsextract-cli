#!/bin/sh
# shellcheck disable=SC1007,SC2016
# SC1007: `CDPATH= cd "$dir"` intentionally clears CDPATH for one command.
# SC2016: launcher templates use literal $install_dir for runtime expansion.
set -eu

REPO_URL="${WGSEXTRACT_REPO_URL:-https://github.com/theontho/wgsextract-cli}"
REQUESTED_REF="${WGSEXTRACT_REF:-${WGSEXTRACT_RELEASE_TAG:-latest}}"

log() {
    printf '%s\n' "$*"
}

print_banner() {
    printf '%s\n' " __        ______ ____  _____      _                  _"
    printf '%s\n' " \ \      / / ___/ ___|| ____|_  _| |_ _ __ __ _  ___| |_"
    printf '%s\n' "  \ \ /\ / / |  _\___ \|  _| \ \/ / __| '__/ _\` |/ __| __|"
    printf '%s\n' "   \ V  V /| |_| |___) | |___ >  <| |_| | | (_| | (__| |_"
    printf '%s\n' "    \_/\_/  \____|____/|_____/_/\_\\\\__|_|  \__,_|\___|\__|"
    printf '%s\n' ""
}

fail() {
    printf 'Error: %s\n' "$*" >&2
    exit 1
}

quote_sh() {
    printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

command_exists() {
    command -v "$1" >/dev/null 2>&1
}

is_truthy() {
    case "$1" in
        1|true|TRUE|yes|YES|on|ON)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

download_with_retry() {
    download_url="$1"
    download_output="$2"

    if [ -f "$download_url" ]; then
        cp "$download_url" "$download_output"
        return
    fi

    if [ -t 2 ]; then
        curl -fL --progress-bar --retry 5 --retry-delay 2 -o "$download_output" "$download_url"
    else
        curl -fL --silent --show-error --retry 5 --retry-delay 2 -o "$download_output" "$download_url"
    fi
}

github_codeload_url() {
    repo_url="$1"
    ref="$2"
    case "$repo_url" in
        https://github.com/*/*)
            repo_path="${repo_url#https://github.com/}"
            repo_path="${repo_path%.git}"
            owner="${repo_path%%/*}"
            repo_name="${repo_path#*/}"
            repo_name="${repo_name%%/*}"
            printf 'https://codeload.github.com/%s/%s/tar.gz/%s\n' "$owner" "$repo_name" "$ref"
            ;;
        *)
            return 1
            ;;
    esac
}

download_source_archive() {
    primary_url="$1"
    fallback_url="${2:-}"
    output="$3"

    if download_with_retry "$primary_url" "$output"; then
        return 0
    fi
    if [ -n "$fallback_url" ] && [ "$fallback_url" != "$primary_url" ]; then
        log "Primary source archive download failed; downloading from $fallback_url"
        download_with_retry "$fallback_url" "$output"
        return
    fi
    return 1
}

pixi_asset_name() {
    platform="$(uname -s)"
    arch="${PIXI_ARCH:-$(uname -m)}"
    case "$platform" in
        Darwin)
            platform="apple-darwin"
            ;;
        Linux)
            if [ "$arch" = "riscv64" ]; then
                platform="unknown-linux-gnu"
            else
                platform="unknown-linux-musl"
            fi
            ;;
        *)
            fail "Unsupported Pixi install platform: $platform"
            ;;
    esac
    case "$arch" in
        arm64|aarch64)
            arch="aarch64"
            ;;
        riscv64)
            arch="riscv64gc"
            ;;
    esac
    printf 'pixi-%s-%s.tar.gz' "$arch" "$platform"
}

install_embedded_pixi() {
    pixi_version="${PIXI_VERSION:-latest}"
    pixi_repo_url="${PIXI_REPOURL:-https://github.com/prefix-dev/pixi}"
    pixi_home="${WGSEXTRACT_PIXI_HOME:-$INSTALL_DIR/.pixi}"
    case "$pixi_home" in
        "~"|"~"/*)
            pixi_home="$HOME${pixi_home#\~}"
            ;;
    esac
    pixi_bin_dir="${WGSEXTRACT_PIXI_BIN_DIR:-$pixi_home/bin}"
    pixi_asset="$(pixi_asset_name)"
    if [ "$pixi_version" = "latest" ]; then
        pixi_url="${PIXI_DOWNLOAD_URL:-${pixi_repo_url%/}/releases/latest/download/$pixi_asset}"
    else
        pixi_url="${PIXI_DOWNLOAD_URL:-${pixi_repo_url%/}/releases/download/v${pixi_version#v}/$pixi_asset}"
    fi

    log "Downloading Pixi from $pixi_url"
    pixi_work_dir="$(mktemp -d "${TMPDIR:-/tmp}/pixi-install.XXXXXX")"
    pixi_archive="$pixi_work_dir/$pixi_asset"
    pixi_extract_dir="$pixi_work_dir/pixi"
    mkdir -p "$pixi_extract_dir" "$pixi_bin_dir"
    if download_with_retry "$pixi_url" "$pixi_archive"; then
        tar -xzf "$pixi_archive" -C "$pixi_extract_dir"
        pixi_binary="$(find "$pixi_extract_dir" -type f -name pixi | head -n 1)"
        [ -n "$pixi_binary" ] || fail "Downloaded Pixi archive did not contain a pixi binary."
        mv "$pixi_binary" "$pixi_bin_dir/pixi"
    else
        pixi_binary_url="${pixi_url%.tar.gz}"
        log "Pixi archive download failed; downloading raw binary from $pixi_binary_url"
        download_with_retry "$pixi_binary_url" "$pixi_bin_dir/pixi"
    fi
    chmod +x "$pixi_bin_dir/pixi"
    rm -rf "$pixi_work_dir"
    PIXI="$pixi_bin_dir/pixi"
    PIXI_INSTALL_ROOT="$(CDPATH= cd "$pixi_bin_dir/.." && pwd)"
    PIXI_BIN_DIR="$(CDPATH= cd "$pixi_bin_dir" && pwd)"
}

verify_xcode_command_line_tools() {
    [ "$OS_NAME" = "Darwin" ] || return 0

    if ! /usr/bin/xcode-select -p >/dev/null 2>&1; then
        cat >&2 <<'EOF'
Error: Xcode Command Line Tools are required on macOS.
Install them with `xcode-select --install`, then rerun this installer.
EOF
        exit 1
    fi

    if ! /usr/bin/xcrun --find clang >/dev/null 2>&1; then
        developer_dir="$(/usr/bin/xcode-select -p 2>/dev/null || true)"
        cat >&2 <<EOF
Error: Xcode Command Line Tools are selected but clang is not available.
Selected developer directory: ${developer_dir:-unknown}
Run \`sudo xcode-select --reset\` or reinstall with \`xcode-select --install\`, then rerun this installer.
EOF
        exit 1
    fi

    developer_dir="$(/usr/bin/xcode-select -p)"
    log "Xcode Command Line Tools are available: $developer_dir"
}

resolve_latest_release_tag() {
    latest_url="$REPO_URL/releases/latest"
    effective_url="$(curl -fsIL -o /dev/null -w '%{url_effective}' "$latest_url")" || fail "Could not resolve latest release from $latest_url. To bypass latest-release resolution, set WGSEXTRACT_RELEASE_TAG=<tag>, WGSEXTRACT_REF=main, or WGSEXTRACT_ARCHIVE_URL=<url>."
    latest_tag="${effective_url##*/}"
    case "$latest_tag" in
        ""|latest|releases)
            fail "Could not determine latest release tag from $effective_url. To bypass latest-release resolution, set WGSEXTRACT_RELEASE_TAG=<tag>, WGSEXTRACT_REF=main, or WGSEXTRACT_ARCHIVE_URL=<url>."
            ;;
    esac
    printf '%s\n' "$latest_tag"
}

absolute_path() {
    case "$1" in
        /*)
            printf '%s\n' "$1"
            ;;
        *)
            printf '%s/%s\n' "$(pwd -P)" "$1"
            ;;
    esac
}

default_install_parent() {
    script_path="${0:-}"
    script_name="$(basename "$script_path")"

    case "$script_name" in
        sh|bash|dash|zsh|ksh|-*)
            pwd -P
            return
            ;;
    esac

    if [ -n "$script_path" ] && [ -f "$script_path" ]; then
        script_dir="$(dirname "$script_path")"
        (CDPATH= cd "$script_dir" && pwd -P) || pwd -P
        return
    fi

    pwd -P
}

DEFAULT_INSTALL_PARENT="$(default_install_parent)"
INSTALL_DIR="$(absolute_path "${WGSEXTRACT_INSTALL_DIR:-$DEFAULT_INSTALL_PARENT/wgsextract-cli}")"
APP_DIR="$INSTALL_DIR/app"
TMP_DIR="$APP_DIR/tmp"
DEFAULT_BIN_DIR="$INSTALL_DIR"
DEFAULT_PIXI_CACHE_DIR="$INSTALL_DIR/.pixi/cache"
DEFAULT_PIXI_ENV_DIR="$INSTALL_DIR/.pixi/envs"
BIN_DIR="$(absolute_path "${WGSEXTRACT_BIN_DIR:-$DEFAULT_BIN_DIR}")"
LAUNCHER="$BIN_DIR/wgsextract"
PIXI_CACHE_DIR="$(absolute_path "${WGSEXTRACT_PIXI_CACHE_DIR:-$DEFAULT_PIXI_CACHE_DIR}")"
PIXI_ENV_DIR="$(absolute_path "${WGSEXTRACT_PIXI_ENV_DIR:-$DEFAULT_PIXI_ENV_DIR}")"
UNINSTALL_SH="$INSTALL_DIR/uninstall.sh"
ARCHIVE_URL="${WGSEXTRACT_ARCHIVE_URL:-}"
NO_OPEN="${WGSEXTRACT_NO_OPEN:-0}"

uses_default_pixi_layout() {
    [ "$PIXI_CACHE_DIR" = "$DEFAULT_PIXI_CACHE_DIR" ] && [ "$PIXI_ENV_DIR" = "$DEFAULT_PIXI_ENV_DIR" ]
}

write_pixi_exports() {
    if uses_default_pixi_layout; then
        printf 'export PIXI_CACHE_DIR="$install_dir/.pixi/cache"\n'
        printf 'export PIXI_PROJECT_ENVIRONMENT_DIR="$install_dir/.pixi/envs"\n'
    else
        printf 'export PIXI_CACHE_DIR=%s\n' "$(quote_sh "$PIXI_CACHE_DIR")"
        printf 'export PIXI_PROJECT_ENVIRONMENT_DIR=%s\n' "$(quote_sh "$PIXI_ENV_DIR")"
    fi
}

write_cli_launcher() {
    if [ "$BIN_DIR" = "$DEFAULT_BIN_DIR" ]; then
        {
            printf '#!/bin/sh\n'
            printf '# WGS Extract CLI installer launcher\n'
            printf 'set -eu\n'
            printf 'script_dir=$(CDPATH= cd "$(dirname "$0")" && pwd)\n'
            printf 'install_dir="$script_dir"\n'
            write_pixi_exports
            printf 'cd "$install_dir/app" || exit 1\n'
            printf 'exec %s run wgsextract "$@"\n' "$(quote_sh "$PIXI")"
        } > "$LAUNCHER"
    else
        {
            printf '#!/bin/sh\n'
            printf '# WGS Extract CLI installer launcher\n'
            printf 'set -eu\n'
            printf 'install_dir=%s\n' "$(quote_sh "$INSTALL_DIR")"
            write_pixi_exports
            printf 'cd %s || exit 1\n' "$(quote_sh "$APP_DIR")"
            printf 'exec %s run wgsextract "$@"\n' "$(quote_sh "$PIXI")"
        } > "$LAUNCHER"
    fi
    chmod +x "$LAUNCHER"
}

write_uninstaller() {
    if [ ! -f "$APP_DIR/uninstall.sh" ]; then
        fail "Installer payload is missing uninstall.sh."
    fi
    cp "$APP_DIR/uninstall.sh" "$UNINSTALL_SH"
    [ -s "$UNINSTALL_SH" ] || fail "Copied uninstaller is empty: $UNINSTALL_SH"
    sh -n "$UNINSTALL_SH" || fail "Copied uninstaller failed shell syntax validation: $UNINSTALL_SH"
    chmod +x "$UNINSTALL_SH"
}

json_escape() {
    printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

append_manifest_item() {
    if [ -n "$manifest_items" ]; then
        manifest_items="$manifest_items,"
    fi
    manifest_items="$manifest_items$1"
}

write_install_manifest() {
    manifest_path="$INSTALL_DIR/install-manifest.json"
    manifest_items=""
    if [ "$PIXI_INSTALLED_BY_SETUP" = "1" ]; then
        if [ -n "$PIXI_INSTALL_ROOT" ]; then
            append_manifest_item "{\"type\":\"directory\",\"path\":\"$(json_escape "$PIXI_INSTALL_ROOT")\",\"ownedBy\":\"install.sh\"}"
        fi
        if [ -n "$PIXI_BIN_DIR" ]; then
            append_manifest_item "{\"type\":\"userPathEntry\",\"path\":\"$(json_escape "$PIXI_BIN_DIR")\",\"ownedBy\":\"install.sh\"}"
        fi
    fi
    created_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '{\n  "format": 1,\n  "createdAt": "%s",\n  "wgsextractRef": "%s",\n  "items": [%s]\n}\n' \
        "$created_at" "$(json_escape "${REF:-$REQUESTED_REF}")" "$manifest_items" > "$manifest_path"
}

remove_legacy_bin_launcher() {
    legacy_launcher="$INSTALL_DIR/bin/wgsextract"
    if [ "$LAUNCHER" != "$legacy_launcher" ]; then
        rm -f "$legacy_launcher"
        rmdir "$INSTALL_DIR/bin" 2>/dev/null || true
    fi
}

remove_legacy_gui_launchers() {
    rm -f \
        "$INSTALL_DIR/start-wgsextract-gui.sh" \
        "$INSTALL_DIR/WGS Extract GUI.command" \
        "$INSTALL_DIR/start-wgsextract-web-gui.sh" \
        "$INSTALL_DIR/WGS Extract Web GUI.command"
}

OS_NAME="$(uname -s)"
case "$OS_NAME" in
    Darwin|Linux)
        ;;
    *)
        fail "This Pixi installer supports macOS and Linux."
        ;;
esac

command_exists curl || fail "curl is required to download the installer payload."
command_exists tar || fail "tar is required to extract the installer payload."
command_exists gzip || fail "gzip is required to extract the installer payload."
verify_xcode_command_line_tools

ARCHIVE_FALLBACK_URL=""
if [ -z "$ARCHIVE_URL" ]; then
    case "$REQUESTED_REF" in
        ""|latest)
            REF="$(resolve_latest_release_tag)"
            ;;
        *)
            REF="$REQUESTED_REF"
            ;;
    esac
    ARCHIVE_URL="$REPO_URL/archive/$REF.tar.gz"
    ARCHIVE_FALLBACK_URL="$(github_codeload_url "$REPO_URL" "$REF" || true)"
else
    REF="${REQUESTED_REF:-custom}"
fi

print_banner
log "This installer will:"
log "  1. Use Pixi if it is already installed, or install Pixi if it is missing."
log "  2. Download WGS Extract CLI from:"
log "     $ARCHIVE_URL"
log "  3. Install or update the app in:"
log "     $INSTALL_DIR"
log "  4. Create the CLI launcher:"
log "     $LAUNCHER"
case "$OS_NAME" in
    Darwin)
        log "  5. Create the uninstaller:"
        log "     $UNINSTALL_SH"
        log "  6. Verify the app starts and required dependencies are visible."
        if ! is_truthy "$NO_OPEN"; then
            log "  7. Open the install folder in Finder when finished."
        fi
        ;;
    Linux)
        log "  5. Create the uninstaller:"
        log "     $UNINSTALL_SH"
        log "  6. Verify the app starts and required dependencies are visible."
        ;;
esac
log ""

PIXI="${PIXI:-}"
PIXI_INSTALLED_BY_SETUP=0
PIXI_INSTALL_ROOT=""
PIXI_BIN_DIR=""
if [ -n "$PIXI" ] && [ ! -x "$PIXI" ]; then
    fail "PIXI is set but is not executable: $PIXI"
fi

if [ -z "$PIXI" ]; then
    if command_exists pixi; then
        PIXI="$(command -v pixi)"
    elif [ -x "$HOME/.pixi/bin/pixi" ]; then
        PIXI="$HOME/.pixi/bin/pixi"
    else
        log "Installing Pixi..."
        if [ -n "${WGSEXTRACT_PIXI_HOME:-}" ]; then
            install_embedded_pixi
        else
            curl -fsSL https://pixi.sh/install.sh | sh
            PIXI_INSTALL_ROOT="$HOME/.pixi"
            PIXI_BIN_DIR="$HOME/.pixi/bin"
            if [ -x "$HOME/.pixi/bin/pixi" ]; then
                PIXI="$HOME/.pixi/bin/pixi"
            elif command_exists pixi; then
                PIXI="$(command -v pixi)"
            else
                fail "Pixi installation completed, but pixi was not found. Open a new terminal and rerun this installer."
            fi
        fi
        PIXI_INSTALLED_BY_SETUP=1
    fi
fi

mkdir -p "$TMP_DIR" "$BIN_DIR" "$PIXI_CACHE_DIR" "$PIXI_ENV_DIR"
WORK_DIR="$(mktemp -d "$TMP_DIR/install.XXXXXX")"
cleanup() {
    rm -rf "$WORK_DIR"
}
trap cleanup EXIT INT HUP TERM

ARCHIVE="$WORK_DIR/wgsextract-cli.tar.gz"
EXTRACT_DIR="$WORK_DIR/source"
mkdir -p "$EXTRACT_DIR"

log "Downloading WGS Extract CLI from $ARCHIVE_URL"
download_source_archive "$ARCHIVE_URL" "$ARCHIVE_FALLBACK_URL" "$ARCHIVE"
tar -xzf "$ARCHIVE" -C "$EXTRACT_DIR"

SOURCE_DIR="$(find "$EXTRACT_DIR" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
if [ -z "$SOURCE_DIR" ]; then
    fail "Downloaded archive did not contain a source directory."
fi

rm -rf "$APP_DIR.new"
mv "$SOURCE_DIR" "$APP_DIR.new"
rm -rf "$APP_DIR"
mv "$APP_DIR.new" "$APP_DIR"
mkdir -p "$TMP_DIR"

log "Installing Pixi environment..."
cd "$APP_DIR"
export PIXI_CACHE_DIR
export PIXI_PROJECT_ENVIRONMENT_DIR="$PIXI_ENV_DIR"
"$PIXI" install

log "Writing launchers..."
write_cli_launcher
remove_legacy_gui_launchers
write_uninstaller
remove_legacy_bin_launcher
write_install_manifest
if uses_default_pixi_layout; then
    rm -rf "$INSTALL_DIR/pixi-cache" "$INSTALL_DIR/pixi-envs"
fi
rm -rf "$INSTALL_DIR/tmp"

log "Checking installation..."
"$PIXI" run wgsextract --help >/dev/null
"$PIXI" run wgsextract deps check >/dev/null

case "$OS_NAME" in
    Darwin)
        if ! is_truthy "$NO_OPEN"; then
            log "Opening install directory in Finder..."
            open "$INSTALL_DIR"
        fi
        ;;
esac

log ""
log "WGS Extract CLI is installed."
log "Install directory: $INSTALL_DIR"
log "Launcher: $LAUNCHER"
log "Uninstaller: $UNINSTALL_SH"
case ":$PATH:" in
    *":$BIN_DIR:"*)
        log "Run: wgsextract --help"
        ;;
    *)
        log "Add this to your shell profile to use wgsextract from any directory:"
        log "  export PATH=$(quote_sh "$BIN_DIR"):\$PATH"
        log "Or run directly:"
        log "  $LAUNCHER --help"
        ;;
esac
