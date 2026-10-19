#!/bin/sh
# Snapshot machine state an installer could touch, one "kind<TAB>path" per line.
#
# Usage: inventory.sh <output-file> [exclude-dir]
# The host diffs snapshots taken before install, after install and after
# uninstall; it never trusts the guest to judge what changed.
set -u

out="$1"
exclude="${2:-}"

list_tree() {
    root="$1"
    depth="$2"
    [ -e "$root" ] || return 0
    find "$root" -maxdepth "$depth" \( -type f -o -type d -o -type l \) 2>/dev/null |
        while IFS= read -r path; do
            if [ -n "$exclude" ]; then
                case "$path" in "$exclude"|"$exclude"/*) continue ;; esac
            fi
            if [ -L "$path" ]; then kind=link; elif [ -d "$path" ]; then kind=dir; else kind=file; fi
            printf '%s\t%s\n' "$kind" "$path"
        done
}

{
    list_tree "$HOME" 6
    list_tree /Applications 1
    list_tree /usr/local 3
    list_tree /opt 2
    list_tree /etc/paths.d 1
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
