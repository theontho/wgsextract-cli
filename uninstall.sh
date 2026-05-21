#!/bin/sh
set -eu

log() {
    printf '%s\n' "$*"
}

fail() {
    printf 'Error: %s\n' "$*" >&2
    exit 1
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

script_dir() {
    script_path="${0:-}"
    script_name="$(basename "$script_path")"

    case "$script_name" in
        sh|bash|dash|zsh|ksh|-*)
            pwd -P
            return
            ;;
    esac

    if [ -n "$script_path" ] && [ -f "$script_path" ]; then
        dir_name="$(dirname "$script_path")"
        (CDPATH= cd "$dir_name" && pwd -P) || pwd -P
        return
    fi

    pwd -P
}

default_install_dir() {
    dir="$(script_dir)"
    base="$(basename "$dir")"
    parent="$(dirname "$dir")"

    if [ "$base" = "app" ] && [ -f "$dir/pixi.toml" ]; then
        printf '%s\n' "$parent"
        return
    fi

    if [ -d "$dir/app" ] && [ -f "$dir/app/pixi.toml" ]; then
        printf '%s\n' "$dir"
        return
    fi

    printf '%s/wgsextract-cli\n' "$dir"
}

usage() {
    cat <<'EOF'
WGS Extract CLI uninstaller for macOS and Linux installs created by install.sh.

Usage:
  uninstall.sh [options]

Options:
  --yes, -y                 Do not prompt for confirmation.
  --install-dir PATH        Install directory to remove.
  --bin-dir PATH            Directory containing the wgsextract launcher.
  --pixi-cache-dir PATH     Pixi cache directory used by the install.
  --pixi-env-dir PATH       Pixi environment directory used by the install.
  --keep-pixi-cache         Leave the Pixi cache directory in place.
  --keep-pixi-envs          Leave the Pixi environment directory in place.
  --remove-pixi             Also remove Pixi from ~/.pixi without prompting.
  --keep-pixi               Do not ask about removing Pixi.
  --remove-config           Also remove WGS Extract config.toml.
  --dry-run                 Print resolved paths and exit without changing anything.
  --help, -h                Show this help.

The uninstaller removes the WGS Extract install tree and launchers. Interactive
runs ask whether to remove Pixi from ~/.pixi too; noninteractive --yes runs keep
Pixi unless --remove-pixi is also set.
EOF
}

need_value() {
    [ "$#" -gt 1 ] || fail "$1 requires a path argument."
}

is_within_or_same() {
    child="$1"
    parent="$2"
    case "$child" in
        "$parent"|"$parent"/*)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

remove_file() {
    remove_file_target="$1"
    if [ ! -e "$remove_file_target" ] && [ ! -L "$remove_file_target" ]; then
        log "Not found: $remove_file_target"
        return
    fi
    log "Removing file: $remove_file_target"
    rm -f "$remove_file_target"
}

remove_launcher_file() {
    launcher_target="$1"
    if [ ! -e "$launcher_target" ] && [ ! -L "$launcher_target" ]; then
        log "Not found: $launcher_target"
        return
    fi
    if has_symlink_component "$launcher_target"; then
        fail "Refusing to remove launcher through a symlink: $launcher_target"
    fi
    if ! grep -F "# WGS Extract CLI installer launcher" "$launcher_target" >/dev/null 2>&1; then
        fail "Refusing to remove launcher that does not look like WGS Extract CLI: $launcher_target"
    fi
    remove_file "$launcher_target"
}

remove_dir() {
    remove_dir_target="$1"
    if [ ! -e "$remove_dir_target" ]; then
        log "Not found: $remove_dir_target"
        return
    fi
    validate_removal_path "$remove_dir_target"
    if [ ! -d "$remove_dir_target" ]; then
        fail "Expected a directory but found something else: $remove_dir_target"
    fi
    log "Removing directory: $remove_dir_target"
    rm -rf "$remove_dir_target"
}

remove_pixi_home() {
    if [ ! -e "$USER_PIXI_HOME" ]; then
        log "Pixi was not found at: $USER_PIXI_HOME"
        return
    fi
    if [ "$USER_PIXI_HOME" != "$HOME/.pixi" ]; then
        fail "Refusing to remove unexpected Pixi home: $USER_PIXI_HOME"
    fi
    if has_symlink_component "$USER_PIXI_HOME"; then
        fail "Refusing to remove Pixi through a symlink: $USER_PIXI_HOME"
    fi
    log "Removing Pixi directory: $USER_PIXI_HOME"
    rm -rf "$USER_PIXI_HOME" || fail "Failed to remove Pixi directory: $USER_PIXI_HOME"
}

remove_empty_dir() {
    remove_empty_dir_target="$1"
    rmdir "$remove_empty_dir_target" 2>/dev/null || true
}

trim_trailing_slashes() {
    trim_path_input="$1"
    while [ "$trim_path_input" != "/" ] && [ "${trim_path_input%/}" != "$trim_path_input" ]; do
        trim_path_input="${trim_path_input%/}"
    done
    printf '%s\n' "$trim_path_input"
}

has_symlink_component() {
    symlink_path="$(trim_trailing_slashes "$1")"
    while [ -n "$symlink_path" ] && [ "$symlink_path" != "/" ] && [ "$symlink_path" != "." ]; do
        if [ -L "$symlink_path" ]; then
            return 0
        fi
        symlink_parent="$(dirname "$symlink_path")"
        [ "$symlink_parent" = "$symlink_path" ] && break
        symlink_path="$symlink_parent"
    done
    return 1
}

physical_path() {
    physical_input="$1"
    if [ -d "$physical_input" ]; then
        (CDPATH= cd "$physical_input" && pwd -P) && return
    fi

    physical_dir="$(dirname "$physical_input")"
    physical_base="$(basename "$physical_input")"
    if [ -d "$physical_dir" ]; then
        physical_resolved_dir="$(CDPATH= cd "$physical_dir" && pwd -P)" || return 1
        printf '%s/%s\n' "$physical_resolved_dir" "$physical_base"
        return
    fi

    printf '%s\n' "$physical_input"
}

validate_removal_path() {
    validation_target="$(trim_trailing_slashes "$1")"
    if has_symlink_component "$validation_target"; then
        fail "Refusing to remove directory through a symlink: $1"
    fi
    validation_physical_target="$(physical_path "$validation_target")"
    case "$validation_physical_target" in
        ""|"."|".."|"/"|"$HOME"|"$HOME/.pixi"|"/Applications"|"/Library"|"/System"|"/Users"|"/bin"|"/etc"|"/opt"|"/opt/homebrew"|"/private"|"/private/tmp"|"/private/var/tmp"|"/sbin"|"/tmp"|"/usr"|"/usr/bin"|"/usr/local"|"/var"|"/var/tmp")
            fail "Refusing to remove unsafe directory: $1"
            ;;
    esac
}

validate_install_marker() {
    if [ -d "$INSTALL_DIR" ] && [ ! -f "$INSTALL_DIR/app/pixi.toml" ]; then
        fail "$INSTALL_DIR does not look like a WGS Extract CLI install created by install.sh."
    fi
}

config_paths() {
    case "$(uname -s)" in
        Darwin)
            if [ -n "${XDG_CONFIG_HOME:-}" ]; then
                printf '%s\n' "$XDG_CONFIG_HOME/wgsextract/config.toml"
            fi
            if [ -d "$HOME/.config" ]; then
                printf '%s\n' "$HOME/.config/wgsextract/config.toml"
            fi
            printf '%s\n' "$HOME/Library/Application Support/wgsextract/config.toml"
            ;;
        Linux)
            printf '%s\n' "${XDG_CONFIG_HOME:-$HOME/.config}/wgsextract/config.toml"
            ;;
        *)
            ;;
    esac
}

remove_pixi_shell_profile_entries() {
    for profile_path in "$HOME/.profile" "$HOME/.bash_profile" "$HOME/.bashrc" "$HOME/.zshrc" "$HOME/.config/fish/config.fish"; do
        if [ ! -f "$profile_path" ]; then
            continue
        fi
        if [ -L "$profile_path" ]; then
            fail "Refusing to edit symlinked shell profile: $profile_path"
        fi
        profile_tmp="$profile_path.wgsextract-uninstall.$$"
        awk '
            /^# >>> pixi initialize >>>/ {
                in_pixi_block = 1
                next
            }
            /^# <<< pixi initialize <<</ && in_pixi_block {
                in_pixi_block = 0
                next
            }
            in_pixi_block {
                next
            }
            {
                path_line = $0
                comment_start = index(path_line, "#")
                if (comment_start > 0) {
                    path_line = substr(path_line, 1, comment_start - 1)
                }
            }
            index(path_line, ".pixi/bin") && (path_line ~ /^[[:space:]]*(export[[:space:]]+)?PATH=/ || path_line ~ /^[[:space:]]*set[[:space:]]+(-[-[:alnum:]]+[[:space:]]+)*PATH[[:space:]]/) {
                next
            }
            {
                print
            }
            END {
                if (in_pixi_block) {
                    exit 2
                }
            }
        ' "$profile_path" > "$profile_tmp" || {
            rm -f "$profile_tmp"
            fail "Could not safely update shell profile: $profile_path"
        }
        if cmp -s "$profile_path" "$profile_tmp"; then
            rm -f "$profile_tmp"
        else
            profile_backup="$profile_path.wgsextract-uninstall-backup.$(date +%Y%m%d%H%M%S).$$"
            cp -p "$profile_path" "$profile_backup" || {
                rm -f "$profile_tmp"
                fail "Could not back up shell profile before editing: $profile_path"
            }
            mv "$profile_tmp" "$profile_path" || {
                rm -f "$profile_tmp"
                fail "Could not update shell profile: $profile_path"
            }
            log "Removed Pixi PATH entries from: $profile_path"
            log "Backup saved as: $profile_backup"
        fi
    done
}

select_pixi_removal() {
    REMOVE_PIXI_SELECTED=0
    if [ "$PIXI_REMOVAL" = "keep" ]; then
        return
    fi
    if [ "$PIXI_REMOVAL" = "remove" ]; then
        REMOVE_PIXI_SELECTED=1
        return
    fi
    if [ ! -x "$USER_PIXI_HOME/bin/pixi" ]; then
        log "Pixi was not found at: $USER_PIXI_HOME"
        return
    fi
    if [ "$ASSUME_YES" = "1" ]; then
        log "Keeping Pixi. Pass --remove-pixi to remove it during a --yes uninstall."
        return
    fi

    log ""
    log "Pixi is installed at:"
    log "  $USER_PIXI_HOME"
    log "Pixi may be shared with other projects."
    printf 'Remove Pixi too? [y/N] '
    if ! read -r REMOVE_PIXI_CONFIRM; then
        log "Keeping Pixi."
        return
    fi
    case "$REMOVE_PIXI_CONFIRM" in
        y|Y)
            REMOVE_PIXI_SELECTED=1
            ;;
        *)
            log "Keeping Pixi."
            ;;
    esac
}

ASSUME_YES=0
DRY_RUN=0
KEEP_PIXI_CACHE=0
KEEP_PIXI_ENVS=0
REMOVE_CONFIG=0
PIXI_REMOVAL=ask
INSTALL_DIR_RAW="${WGSEXTRACT_INSTALL_DIR:-$(default_install_dir)}"
BIN_DIR_RAW="${WGSEXTRACT_BIN_DIR:-}"
PIXI_CACHE_DIR_RAW="${WGSEXTRACT_PIXI_CACHE_DIR:-}"
PIXI_ENV_DIR_RAW="${WGSEXTRACT_PIXI_ENV_DIR:-}"
REMOVE_PIXI_SELECTED=0

while [ "$#" -gt 0 ]; do
    case "$1" in
        --help|-h)
            usage
            exit 0
            ;;
        --yes|-y)
            ASSUME_YES=1
            ;;
        --dry-run)
            DRY_RUN=1
            ;;
        --keep-pixi-cache)
            KEEP_PIXI_CACHE=1
            ;;
        --keep-pixi-envs)
            KEEP_PIXI_ENVS=1
            ;;
        --remove-pixi)
            PIXI_REMOVAL=remove
            ;;
        --keep-pixi)
            PIXI_REMOVAL=keep
            ;;
        --remove-config)
            REMOVE_CONFIG=1
            ;;
        --install-dir)
            need_value "$@"
            shift
            INSTALL_DIR_RAW="$1"
            ;;
        --install-dir=*)
            INSTALL_DIR_RAW="${1#*=}"
            ;;
        --bin-dir)
            need_value "$@"
            shift
            BIN_DIR_RAW="$1"
            ;;
        --bin-dir=*)
            BIN_DIR_RAW="${1#*=}"
            ;;
        --pixi-cache-dir)
            need_value "$@"
            shift
            PIXI_CACHE_DIR_RAW="$1"
            ;;
        --pixi-cache-dir=*)
            PIXI_CACHE_DIR_RAW="${1#*=}"
            ;;
        --pixi-env-dir)
            need_value "$@"
            shift
            PIXI_ENV_DIR_RAW="$1"
            ;;
        --pixi-env-dir=*)
            PIXI_ENV_DIR_RAW="${1#*=}"
            ;;
        *)
            fail "Unknown option: $1. Run uninstall.sh --help for usage."
            ;;
    esac
    shift
done

if [ -z "${HOME:-}" ] || [ "$HOME" = "/" ]; then
    fail "HOME environment variable is not set or is unsafe."
fi

INSTALL_DIR="$(absolute_path "$INSTALL_DIR_RAW")"
DEFAULT_BIN_DIR="$INSTALL_DIR"
DEFAULT_PIXI_CACHE_DIR="$INSTALL_DIR/.pixi/cache"
DEFAULT_PIXI_ENV_DIR="$INSTALL_DIR/.pixi/envs"
BIN_DIR="$(absolute_path "${BIN_DIR_RAW:-$DEFAULT_BIN_DIR}")"
LAUNCHER="$BIN_DIR/wgsextract"
PIXI_CACHE_DIR="$(absolute_path "${PIXI_CACHE_DIR_RAW:-$DEFAULT_PIXI_CACHE_DIR}")"
PIXI_ENV_DIR="$(absolute_path "${PIXI_ENV_DIR_RAW:-$DEFAULT_PIXI_ENV_DIR}")"
USER_PIXI_HOME="$HOME/.pixi"

validate_removal_path "$INSTALL_DIR"

log "--- WGS Extract CLI uninstaller ---"
log "Install directory: $INSTALL_DIR"
log "Launcher:          $LAUNCHER"
log "Pixi envs:         $PIXI_ENV_DIR"
log "Pixi cache:        $PIXI_CACHE_DIR"
log "Pixi home:         $USER_PIXI_HOME"
if [ "$REMOVE_CONFIG" = "1" ]; then
    log "Config removal:    enabled"
else
    log "Config removal:    disabled"
fi
case "$PIXI_REMOVAL" in
    remove)
        log "Pixi removal:      enabled"
        ;;
    keep)
        log "Pixi removal:      disabled"
        ;;
    *)
        log "Pixi removal:      ask"
        ;;
esac
log ""
log "This removes the WGS Extract CLI install."
log ""

if [ "$DRY_RUN" = "1" ]; then
    log "Dry run only; no changes were made."
    exit 0
fi

validate_install_marker

if [ "$ASSUME_YES" = "0" ]; then
    printf 'Continue? [y/N] '
    if ! read -r CONFIRM; then
        log "Uninstall cancelled."
        exit 0
    fi
    case "$CONFIRM" in
        y|Y)
            ;;
        *)
            log "Uninstall cancelled."
            exit 0
            ;;
    esac
fi

select_pixi_removal
validate_install_marker

if is_within_or_same "$LAUNCHER" "$INSTALL_DIR"; then
    log "Launcher is inside the install directory and will be removed with it."
else
    remove_launcher_file "$LAUNCHER"
    remove_empty_dir "$BIN_DIR"
fi

if [ "$KEEP_PIXI_ENVS" = "0" ]; then
    if is_within_or_same "$PIXI_ENV_DIR" "$INSTALL_DIR"; then
        log "Pixi envs are inside the install directory and will be removed with it."
    else
        remove_dir "$PIXI_ENV_DIR"
    fi
else
    log "Keeping Pixi environments."
fi

if [ "$KEEP_PIXI_CACHE" = "0" ]; then
    if is_within_or_same "$PIXI_CACHE_DIR" "$INSTALL_DIR"; then
        log "Pixi cache is inside the install directory and will be removed with it."
    else
        remove_dir "$PIXI_CACHE_DIR"
    fi
else
    log "Keeping Pixi cache."
fi

remove_dir "$INSTALL_DIR"

if [ "$REMOVE_CONFIG" = "1" ]; then
    config_paths | while IFS= read -r config_path; do
        remove_file "$config_path"
        remove_empty_dir "$(dirname "$config_path")"
    done
else
    log "Keeping WGS Extract config.toml."
fi

if [ "$REMOVE_PIXI_SELECTED" = "1" ]; then
    remove_pixi_shell_profile_entries
    remove_pixi_home
else
    log "Keeping Pixi."
fi

log ""
log "Uninstall complete."
