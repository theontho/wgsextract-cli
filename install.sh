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

toml_string() {
    # TOML basic string: escape backslashes and double quotes.
    printf '"%s"' "$(printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g')"
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

expand_home_path() {
    case "$1" in
        "~"|"~"/*)
            printf '%s\n' "$HOME${1#\~}"
            ;;
        *)
            printf '%s\n' "$1"
            ;;
    esac
}

download_with_retry() {
    download_url="$1"
    download_output="$2"
    download_url="$(expand_home_path "$download_url")"

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
        if download_with_retry "$fallback_url" "$output"; then
            return 0
        fi
    fi
    fail "Could not download source archive from $primary_url"
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

make_staging_directory() {
    staging_suffix="$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
    [ "${#staging_suffix}" = "32" ] || fail "Could not generate a staging directory name."
    staging_directory="$1/$2.$staging_suffix"
    (umask 077; mkdir "$staging_directory") || fail "Could not create private staging directory."
    printf '%s\n' "$staging_directory"
}

install_embedded_pixi() {
    pixi_version="$PIXI_VERSION"
    [ "${pixi_version#v}" = "0.82.0" ] || fail "No trusted Pixi checksums for $pixi_version; use v0.82.0."
    pixi_repo_url="${PIXI_REPOURL:-https://github.com/prefix-dev/pixi}"
    pixi_home="${WGSEXTRACT_PIXI_HOME:-$INSTALL_DIR/.pixi}"
    pixi_home="$(expand_home_path "$pixi_home")"
    pixi_bin_dir="${WGSEXTRACT_PIXI_BIN_DIR:-$pixi_home/bin}"
    pixi_asset="$(pixi_asset_name)"
    if [ "$pixi_version" = "latest" ]; then
        pixi_url="${PIXI_DOWNLOAD_URL:-${pixi_repo_url%/}/releases/latest/download/$pixi_asset}"
    else
        pixi_url="${PIXI_DOWNLOAD_URL:-${pixi_repo_url%/}/releases/download/v${pixi_version#v}/$pixi_asset}"
    fi

    log "Downloading Pixi from $pixi_url"
    mkdir -p "$INSTALL_DIR"
    pixi_work_dir="$(make_staging_directory "$INSTALL_DIR" ".pixi-download")"
    pixi_archive="$pixi_work_dir/$pixi_asset"
    pixi_extract_dir="$pixi_work_dir/pixi"
    mkdir -p "$pixi_extract_dir" "$pixi_bin_dir"
    if download_with_retry "$pixi_url" "$pixi_archive"; then
        if ! verify_pixi_download "$pixi_archive" "$pixi_asset"; then
            rm -rf "$pixi_work_dir"
            fail "Pixi archive SHA-256 verification failed."
        fi
        if ! tar -xzf "$pixi_archive" -C "$pixi_extract_dir"; then
            rm -rf "$pixi_work_dir"
            fail "Downloaded Pixi archive could not be extracted."
        fi
        pixi_binary="$(find "$pixi_extract_dir" -type f -name pixi | head -n 1)"
        if [ -z "$pixi_binary" ]; then
            rm -rf "$pixi_work_dir"
            fail "Downloaded Pixi archive did not contain a pixi binary."
        fi
        if ! mv "$pixi_binary" "$pixi_bin_dir/pixi"; then
            rm -rf "$pixi_work_dir"
            fail "Could not install Pixi into $pixi_bin_dir."
        fi
    else
        pixi_binary_url="${pixi_url%.tar.gz}"
        log "Pixi archive download failed; downloading raw binary from $pixi_binary_url"
        pixi_binary="$pixi_work_dir/pixi"
        if ! download_with_retry "$pixi_binary_url" "$pixi_binary"; then
            rm -rf "$pixi_work_dir"
            fail "Could not download Pixi from $pixi_url or $pixi_binary_url"
        fi
        if ! verify_pixi_download "$pixi_binary" "${pixi_asset%.tar.gz}"; then
            rm -rf "$pixi_work_dir"
            fail "Pixi binary SHA-256 verification failed."
        fi
        mv "$pixi_binary" "$pixi_bin_dir/pixi" || fail "Could not install verified Pixi."
    fi
    if ! chmod +x "$pixi_bin_dir/pixi"; then
        rm -rf "$pixi_work_dir"
        fail "Could not make Pixi executable at $pixi_bin_dir/pixi."
    fi
    rm -rf "$pixi_work_dir"
    PIXI="$pixi_bin_dir/pixi"
    PIXI_INSTALL_ROOT="$(CDPATH= cd "$pixi_bin_dir/.." && pwd)"
    PIXI_BIN_DIR="$(CDPATH= cd "$pixi_bin_dir" && pwd)"
}

verify_pixi_download() {
    # v0.82.0 release asset SHA-256s. Update these together with PIXI_VERSION.
    case "$2" in
        pixi-aarch64-apple-darwin.tar.gz) expected="3b48bd27de4db31381dc1d5b3d069c98852c0e923aefa30dae0452a088c0810e" ;;
        pixi-x86_64-apple-darwin.tar.gz) expected="a0e5dc45a2ced8987a4df3c597d96528c87089ea3481ed4bb7c27404699540f1" ;;
        pixi-aarch64-unknown-linux-musl.tar.gz) expected="b18dadba196c0c77781533184ec3e7757422c0ae9bed0047ff7ca1c688c0f62b" ;;
        pixi-x86_64-unknown-linux-musl.tar.gz) expected="d305406a7630aab6484419833f9204b2fe9ea40632da8bb536496f123ca86e7f" ;;
        pixi-riscv64gc-unknown-linux-gnu.tar.gz) expected="03261cc2fa85e6167d99af219bf8d49a608306cb6c6a8ac30f7113b3d925a33b" ;;
        pixi-aarch64-apple-darwin) expected="92aa551c67a56ec0ba105fa20c3f48094b02586de523e24b96974ba1f092455b" ;;
        pixi-x86_64-apple-darwin) expected="db4bd6dc3a5f88f2d94cf2e1f8a377b53d3aaac95b45b058854fbf53919d6d36" ;;
        pixi-aarch64-unknown-linux-musl) expected="c6e3c4d98bb41560b5cfdebbe6cbf766f3547603a0fb96e1450a6ccff74b5e14" ;;
        pixi-x86_64-unknown-linux-musl) expected="1dff1db704884f078c44b4c7fb3c5bc6f946fc3d1fa679edf214ad79187a9c64" ;;
        pixi-riscv64gc-unknown-linux-gnu) expected="e34c641729f54bc32c67bc70c07ba2a3fbdc0fbb5651f0d175ea30b42096d74c" ;;
        *) log "No trusted checksum for Pixi asset $2"; return 1 ;;
    esac
    if command_exists sha256sum; then
        actual="$(sha256sum "$1" | awk '{print $1}')"
    elif command_exists shasum; then
        actual="$(shasum -a 256 "$1" | awk '{print $1}')"
    else
        log "SHA-256 verification requires sha256sum or shasum."
        return 1
    fi
    [ "$actual" = "$expected" ]
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
# Pixi keeps project environments in <project>/.pixi/envs unless told otherwise.
DEFAULT_PIXI_ENV_DIR="$APP_DIR/.pixi/envs"
BIN_DIR="$(absolute_path "${WGSEXTRACT_BIN_DIR:-$DEFAULT_BIN_DIR}")"
LAUNCHER="$BIN_DIR/wgsextract"
PIXI_CACHE_DIR="$(absolute_path "${WGSEXTRACT_PIXI_CACHE_DIR:-$DEFAULT_PIXI_CACHE_DIR}")"
# The Pixi version this release is tested with (CI pins the same one); a user's
# own PIXI_VERSION is accepted only when trusted checksums are available.
PIXI_VERSION="${PIXI_VERSION:-v0.82.0}"
# Pixi writes its own markers (e.g. the telemetry notice) under the Rattler cache,
# ~/.cache/rattler by default; keep them inside the install so uninstall is clean.
RATTLER_CACHE_DIR="$PIXI_CACHE_DIR/rattler"
export PIXI_VERSION RATTLER_CACHE_DIR
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
        printf 'export RATTLER_CACHE_DIR="$install_dir/.pixi/cache/rattler"\n'
    else
        printf 'export PIXI_CACHE_DIR=%s\n' "$(quote_sh "$PIXI_CACHE_DIR")"
        printf 'export RATTLER_CACHE_DIR=%s\n' "$(quote_sh "$PIXI_CACHE_DIR/rattler")"
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
            printf 'exec %s run --manifest-path "$install_dir/app/pixi.toml" wgsextract "$@"\n' "$(quote_sh "$PIXI")"
        } > "$LAUNCHER"
    else
        {
            printf '#!/bin/sh\n'
            printf '# WGS Extract CLI installer launcher\n'
            printf 'set -eu\n'
            printf 'install_dir=%s\n' "$(quote_sh "$INSTALL_DIR")"
            write_pixi_exports
            printf 'exec %s run --manifest-path %s wgsextract "$@"\n' "$(quote_sh "$PIXI")" "$(quote_sh "$APP_DIR/pixi.toml")"
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
    printf '%s' "$1" | awk '
        BEGIN { ORS = "" }
        NR > 1 { printf "\\n" }
        {
            gsub(/\\/, "\\\\")
            gsub(/"/, "\\\"")
            gsub(/\t/, "\\t")
            gsub(/\r/, "\\r")
            printf "%s", $0
        }
    '
}

open_install_dir() {
    if [ -n "${WGSEXTRACT_OPEN_COMMAND:-}" ]; then
        "$WGSEXTRACT_OPEN_COMMAND" "$INSTALL_DIR"
    else
        open "$INSTALL_DIR"
    fi
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
CREATED_PROFILES=""
WORK_DIR=""
pixi_work_dir=""
cleanup() {
    [ -z "$WORK_DIR" ] || rm -rf "$WORK_DIR"
    [ -z "$pixi_work_dir" ] || rm -rf "$pixi_work_dir"
}
trap cleanup EXIT
trap 'cleanup; exit 1' INT HUP TERM
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
        install_embedded_pixi
        PIXI_INSTALLED_BY_SETUP=1
        shell_name="${SHELL:-}"
        case "${shell_name##*/}" in
            fish)
                log "For fish, add Pixi to PATH with: fish_add_path $(quote_sh "$PIXI_BIN_DIR")"
                ;;
            *)
                log "To use Pixi directly, add this to your shell profile: export PATH=$(quote_sh "$PIXI_BIN_DIR"):\"\$PATH\""
                ;;
        esac
    fi
fi

mkdir -p "$TMP_DIR" "$BIN_DIR" "$PIXI_CACHE_DIR" "$PIXI_ENV_DIR"
WORK_DIR="$(make_staging_directory "$TMP_DIR" "install")"

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
# An update replaces the app source, but reference genomes, example genomes,
# downloaded jars and the Pixi environments inside app/ belong to the user (and
# config.toml may point at them), so carry them into the new tree.
for preserved in reference genomes jartools .pixi/envs; do
    if [ -e "$APP_DIR/$preserved" ] && [ ! -e "$APP_DIR.new/$preserved" ]; then
        mkdir -p "$(dirname "$APP_DIR.new/$preserved")"
        mv "$APP_DIR/$preserved" "$APP_DIR.new/$preserved"
    fi
done
rm -rf "$APP_DIR"
mv "$APP_DIR.new" "$APP_DIR"
mkdir -p "$TMP_DIR"

if [ "$PIXI_ENV_DIR" != "$DEFAULT_PIXI_ENV_DIR" ]; then
    # Pixi reads the environment location from project config, not from env vars.
    mkdir -p "$APP_DIR/.pixi"
    printf 'detached-environments = %s\n' "$(toml_string "$PIXI_ENV_DIR")" > "$APP_DIR/.pixi/config.toml"
fi

log "Installing Pixi environment..."
cd "$APP_DIR"
export PIXI_CACHE_DIR
# --locked installs exactly the versions the release was tested with.
"$PIXI" install --locked

# The CLI creates its config directory on first save. Remember config parents
# that don't exist yet, so `uninstall.sh --remove-config` can remove them again.
case "$OS_NAME" in
    Linux)
        config_parent="${XDG_CONFIG_HOME:-$HOME/.config}"
        if [ ! -e "$config_parent" ] && [ ! -e "$INSTALL_DIR/created-dirs.txt" ]; then
            printf '%s\n' "$config_parent" > "$INSTALL_DIR/created-dirs.txt"
        fi
        ;;
esac

if [ -n "$CREATED_PROFILES" ]; then
    printf '%s\n' "$CREATED_PROFILES" > "$INSTALL_DIR/created-profiles.txt"
fi
# Where this install put things, so uninstall.sh finds them without being told.
printf 'bin_dir\t%s\npixi_cache_dir\t%s\npixi_env_dir\t%s\n' \
    "$BIN_DIR" "$PIXI_CACHE_DIR" "$PIXI_ENV_DIR" > "$INSTALL_DIR/install-layout.txt"

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
            open_install_dir
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
