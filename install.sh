#!/bin/sh
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
GUI_SH="$INSTALL_DIR/start-wgsextract-gui.sh"
GUI_COMMAND="$INSTALL_DIR/WGS Extract GUI.command"
UNINSTALL_SH="$INSTALL_DIR/uninstall.sh"
ARCHIVE_URL="${WGSEXTRACT_ARCHIVE_URL:-}"

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
            printf 'set -eu\n'
            printf 'install_dir=%s\n' "$(quote_sh "$INSTALL_DIR")"
            write_pixi_exports
            printf 'cd %s || exit 1\n' "$(quote_sh "$APP_DIR")"
            printf 'exec %s run wgsextract "$@"\n' "$(quote_sh "$PIXI")"
        } > "$LAUNCHER"
    fi
    chmod +x "$LAUNCHER"
}

write_gui_launcher() {
    output_path="$1"
    gui_flag="$2"
    {
        printf '#!/bin/sh\n'
        printf 'set -eu\n'
        printf 'script_dir=$(CDPATH= cd "$(dirname "$0")" && pwd)\n'
        printf 'install_dir="$script_dir"\n'
        write_pixi_exports
        printf 'cd "$script_dir/app" || exit 1\n'
        printf 'exec %s run wgsextract gui %s\n' "$(quote_sh "$PIXI")" "$gui_flag"
    } > "$output_path"
    chmod +x "$output_path"
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

remove_legacy_bin_launcher() {
    legacy_launcher="$INSTALL_DIR/bin/wgsextract"
    if [ "$LAUNCHER" != "$legacy_launcher" ]; then
        rm -f "$legacy_launcher"
        rmdir "$INSTALL_DIR/bin" 2>/dev/null || true
    fi
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
        log "  5. Create the macOS desktop GUI launcher:"
        log "     $GUI_COMMAND"
        log "  6. Create the uninstaller:"
        log "     $UNINSTALL_SH"
        log "  7. Verify the app starts and required dependencies are visible."
        log "  8. Open the install folder in Finder when finished."
        ;;
    Linux)
        log "  5. Create the Linux desktop GUI shell launcher:"
        log "     $GUI_SH"
        log "  6. Create the uninstaller:"
        log "     $UNINSTALL_SH"
        log "  7. Verify the app starts and required dependencies are visible."
        ;;
esac
log ""

PIXI="${PIXI:-}"
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
        curl -fsSL https://pixi.sh/install.sh | sh
        if [ -x "$HOME/.pixi/bin/pixi" ]; then
            PIXI="$HOME/.pixi/bin/pixi"
        elif command_exists pixi; then
            PIXI="$(command -v pixi)"
        else
            fail "Pixi installation completed, but pixi was not found. Open a new terminal and rerun this installer."
        fi
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
curl -fL --retry 3 --retry-delay 2 -o "$ARCHIVE" "$ARCHIVE_URL"
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
case "$OS_NAME" in
    Darwin)
        write_gui_launcher "$GUI_COMMAND" "--desktop"
        rm -f "$GUI_SH" "$INSTALL_DIR/start-wgsextract-web-gui.sh" "$INSTALL_DIR/WGS Extract Web GUI.command"
        ;;
    Linux)
        write_gui_launcher "$GUI_SH" "--desktop"
        rm -f "$GUI_COMMAND" "$INSTALL_DIR/start-wgsextract-web-gui.sh" "$INSTALL_DIR/WGS Extract Web GUI.command"
        ;;
esac
write_uninstaller
remove_legacy_bin_launcher
if uses_default_pixi_layout; then
    rm -rf "$INSTALL_DIR/pixi-cache" "$INSTALL_DIR/pixi-envs"
fi
rm -rf "$INSTALL_DIR/tmp"

log "Checking installation..."
"$PIXI" run wgsextract --help >/dev/null
"$PIXI" run wgsextract deps check >/dev/null

case "$OS_NAME" in
    Darwin)
        log "Opening install directory in Finder..."
        open "$INSTALL_DIR"
        ;;
esac

log ""
log "WGS Extract CLI is installed."
log "Install directory: $INSTALL_DIR"
log "Launcher: $LAUNCHER"
log "Uninstaller: $UNINSTALL_SH"
case "$OS_NAME" in
    Darwin)
        log "Desktop GUI launcher: $GUI_COMMAND"
        ;;
    Linux)
        log "Desktop GUI launcher: $GUI_SH"
        ;;
esac
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
