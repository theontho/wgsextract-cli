#!/bin/sh
set -eu

repo_root="$(CDPATH= cd "$(dirname "$0")/.." && pwd)"
work_dir="${WGSEXTRACT_INSTALLER_TEST_DIR:-$repo_root/out/ci-installer}"
source_parent="$work_dir/source"
source_dir="$source_parent/wgsextract-cli-installer-test"
archive="$work_dir/wgsextract-cli-installer-test.tar.gz"
install_dir="$work_dir/install"
home_dir="$work_dir/home"
pixi_cache_dir="$work_dir/pixi-cache"
pixi_env_dir="$work_dir/pixi-envs"

rm -rf "$work_dir"
mkdir -p "$source_dir" "$home_dir" "$pixi_cache_dir" "$pixi_env_dir"

cd "$repo_root"
git ls-files | while IFS= read -r path; do
    target_dir="$source_dir/$(dirname "$path")"
    mkdir -p "$target_dir"
    cp -pP "$repo_root/$path" "$source_dir/$path"
done

tar -czf "$archive" -C "$source_parent" "$(basename "$source_dir")"

HOME="$home_dir" \
WGSEXTRACT_ARCHIVE_URL="file://$archive" \
WGSEXTRACT_INSTALL_DIR="$install_dir" \
WGSEXTRACT_PIXI_CACHE_DIR="$pixi_cache_dir" \
WGSEXTRACT_PIXI_ENV_DIR="$pixi_env_dir" \
sh "$repo_root/install.sh"

test -x "$install_dir/wgsextract"
"$install_dir/wgsextract" --help >/dev/null
test -d "$install_dir/app/src/wgsextract_cli"
