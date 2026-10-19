#!/bin/sh
# Snapshot machine state an installer could touch, one "kind<TAB>path" per line.
#
# Usage: inventory.sh <output-file> [exclude-dir]
# The host diffs snapshots taken before install, after install and after
# uninstall; it never trusts the guest to judge what changed.
set -u

out="$1"
exclude="${2:-}"

# find can be interrupted mid-traversal (EINTR) and silently skip subtrees, which
# would make a snapshot look like files appeared or vanished. Retry until a walk
# finishes with no error other than a permission denial; otherwise emit an
# error line, which fails the run instead of comparing an incomplete snapshot.
walk() {
    root="$1"
    depth="$2"
    errors="$(mktemp)"
    attempt=1
    while :; do
        listing="$(find "$root" -maxdepth "$depth" \( -type f -o -type d -o -type l \) 2>"$errors")"
        if ! grep -v -e 'Permission denied' -e 'Operation not permitted' "$errors" | grep -q .; then
            break
        fi
        if [ "$attempt" -ge 5 ]; then
            printf 'error\tinventory walk of %s failed: %s\n' "$root" "$(head -n 1 "$errors")"
            break
        fi
        attempt=$((attempt + 1))
        sleep 1
    done
    rm -f "$errors"
    printf '%s\n' "$listing"
}

list_tree() {
    root="$1"
    depth="$2"
    [ -e "$root" ] || return 0
    walk "$root" "$depth" |
        while IFS= read -r path; do
            case "$path" in
                ""|error"	"*) [ -n "$path" ] && printf '%s\n' "$path"; continue ;;
            esac
            if [ -n "$exclude" ]; then
                case "$path" in "$exclude"|"$exclude"/*) continue ;; esac
            fi
            if [ -L "$path" ]; then kind="link"; elif [ -d "$path" ]; then kind="dir"; else kind="file"; fi
            printf '%s\t%s\n' "$kind" "$path"
        done
}

{
    list_tree "$HOME" 6
    list_tree /Applications 1
    list_tree /usr/local 3
    list_tree /opt 2
    list_tree /etc/paths.d 1
    list_tree /usr/share/applications 1
    list_tree /Library/LaunchAgents 1
    list_tree /Library/LaunchDaemons 1
    # Shell startup files: content matters, not just existence.
    for rc in .profile .bashrc .bash_profile .zshrc .zprofile .zshenv .config/fish/config.fish; do
        if [ -f "$HOME/$rc" ]; then
            printf 'rc\t%s\t%s\n' "$HOME/$rc" "$(cksum < "$HOME/$rc" | awk '{print $1 "-" $2}')"
        fi
    done
    printf 'env\tPATH=%s\n' "$PATH"
} | LC_ALL=C sort -u > "$out"
