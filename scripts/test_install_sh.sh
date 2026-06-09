#!/bin/sh
# shellcheck disable=SC1007
# SC1007: `CDPATH= cd "$dir"` intentionally clears CDPATH for one command.
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
fake_bin="$work_dir/fake-bin"
curl_log="$work_dir/curl.log"
open_log="$work_dir/open.log"
release_tag="v0.0.0-installer-test"
pixi_path="$(command -v pixi)"
fake_pixi_source="$work_dir/fake-pixi-src"
fake_pixi_archive="$work_dir/fake-pixi.tar.gz"

rm -rf "$work_dir"
mkdir -p "$source_dir" "$home_dir" "$pixi_cache_dir" "$pixi_env_dir" "$fake_bin" "$fake_pixi_source"

cd "$repo_root"
git ls-files | while IFS= read -r path; do
    target_dir="$source_dir/$(dirname "$path")"
    mkdir -p "$target_dir"
    cp -pP "$repo_root/$path" "$source_dir/$path"
done

tar -czf "$archive" -C "$source_parent" "$(basename "$source_dir")"

cat > "$fake_pixi_source/pixi" <<'SH'
#!/bin/sh
set -eu

case "${1:-}" in
    --version)
        printf 'pixi 0.0.0-test\n'
        ;;
    install)
        exit 0
        ;;
    run)
        shift
        if [ "${1:-}" = "wgsextract" ]; then
            shift
            case "${1:-}" in
                ""|--help|deps)
                    exit 0
                    ;;
            esac
        fi
        printf 'unexpected fake pixi command: %s\n' "$*" >&2
        exit 2
        ;;
    *)
        printf 'unexpected fake pixi command: %s\n' "$*" >&2
        exit 2
        ;;
esac
SH
chmod +x "$fake_pixi_source/pixi"
tar -czf "$fake_pixi_archive" -C "$fake_pixi_source" pixi

cat > "$fake_bin/curl" <<'SH'
#!/bin/sh
set -eu

archive="${WGSEXTRACT_INSTALLER_TEST_ARCHIVE:?}"
pixi_archive="${WGSEXTRACT_INSTALLER_TEST_PIXI_ARCHIVE:?}"
tag="${WGSEXTRACT_INSTALLER_TEST_RELEASE_TAG:?}"
log_path="${WGSEXTRACT_INSTALLER_TEST_CURL_LOG:?}"
last_arg=""
output_path=""
write_effective=0

while [ "$#" -gt 0 ]; do
    case "$1" in
        -o)
            shift
            output_path="$1"
            ;;
        -w)
            shift
            if [ "$1" = "%{url_effective}" ]; then
                write_effective=1
            fi
            ;;
    esac
    last_arg="$1"
    shift
done

printf '%s\n' "$last_arg" >> "$log_path"
case "$last_arg" in
    */releases/latest)
        if [ "$write_effective" -ne 1 ]; then
            printf 'expected curl -w %%{url_effective} for latest release lookup\n' >&2
            exit 2
        fi
        printf 'https://github.com/theontho/wgsextract-cli/releases/tag/%s' "$tag"
        ;;
    */archive/"$tag".tar.gz)
        if [ -z "$output_path" ]; then
            printf 'expected curl -o destination for archive download\n' >&2
            exit 2
        fi
        cp "$archive" "$output_path"
        ;;
    */pixi-*.tar.gz)
        if [ -z "$output_path" ]; then
            printf 'expected curl -o destination for pixi download\n' >&2
            exit 2
        fi
        cp "$pixi_archive" "$output_path"
        ;;
    *)
        printf 'unexpected curl URL in installer test: %s\n' "$last_arg" >&2
        exit 2
        ;;
esac
SH
chmod +x "$fake_bin/curl"

cat > "$fake_bin/open-install-dir" <<'SH'
#!/bin/sh
set -eu

printf '%s\n' "$1" >> "${WGSEXTRACT_INSTALLER_TEST_OPEN_LOG:?}"
SH
chmod +x "$fake_bin/open-install-dir"

HOME="$home_dir" \
PATH="$fake_bin:$PATH" \
PIXI="$pixi_path" \
WGSEXTRACT_INSTALLER_TEST_ARCHIVE="$archive" \
WGSEXTRACT_INSTALLER_TEST_PIXI_ARCHIVE="$fake_pixi_archive" \
WGSEXTRACT_INSTALLER_TEST_RELEASE_TAG="$release_tag" \
WGSEXTRACT_INSTALLER_TEST_CURL_LOG="$curl_log" \
WGSEXTRACT_INSTALL_DIR="$install_dir" \
WGSEXTRACT_PIXI_CACHE_DIR="$pixi_cache_dir" \
WGSEXTRACT_PIXI_ENV_DIR="$pixi_env_dir" \
WGSEXTRACT_NO_OPEN=1 \
sh "$repo_root/install.sh"

grep -F "/releases/latest" "$curl_log" >/dev/null
grep -F "/archive/$release_tag.tar.gz" "$curl_log" >/dev/null
test -x "$install_dir/wgsextract"
test -x "$install_dir/uninstall.sh"
test -f "$install_dir/install-manifest.json"
grep -F "\"wgsextractRef\": \"$release_tag\"" "$install_dir/install-manifest.json" >/dev/null
grep -F "# WGS Extract CLI installer launcher" "$install_dir/wgsextract" >/dev/null
sh -n "$install_dir/uninstall.sh"
"$install_dir/wgsextract" --help >/dev/null
test -d "$install_dir/app/src/wgsextract_cli"

embedded_install_dir="$work_dir/embedded-install"
embedded_home_dir="$work_dir/embedded-home"
rm -f "$curl_log"
mkdir -p "$embedded_home_dir"

HOME="$embedded_home_dir" \
PATH="$fake_bin:/usr/bin:/bin" \
WGSEXTRACT_INSTALLER_TEST_ARCHIVE="$archive" \
WGSEXTRACT_INSTALLER_TEST_PIXI_ARCHIVE="$fake_pixi_archive" \
WGSEXTRACT_INSTALLER_TEST_RELEASE_TAG="$release_tag" \
WGSEXTRACT_INSTALLER_TEST_CURL_LOG="$curl_log" \
WGSEXTRACT_INSTALL_DIR="$embedded_install_dir" \
WGSEXTRACT_ARCHIVE_URL="$archive" \
WGSEXTRACT_PIXI_HOME="$embedded_install_dir/.pixi" \
WGSEXTRACT_PIXI_CACHE_DIR="$embedded_install_dir/.pixi/cache" \
WGSEXTRACT_PIXI_ENV_DIR="$embedded_install_dir/app/.pixi/envs" \
WGSEXTRACT_NO_OPEN=1 \
sh "$repo_root/install.sh"

test -x "$embedded_install_dir/.pixi/bin/pixi"
test -x "$embedded_install_dir/wgsextract"
"$embedded_install_dir/wgsextract" --help >/dev/null
grep -F "/pixi-" "$curl_log" >/dev/null
grep -F "\"path\":\"$embedded_install_dir/.pixi\"" "$embedded_install_dir/install-manifest.json" >/dev/null
grep -F "\"type\":\"userPathEntry\"" "$embedded_install_dir/install-manifest.json" >/dev/null

if [ "$(uname -s)" = "Darwin" ]; then
    interactive_install_dir="$work_dir/interactive-install"
    interactive_home_dir="$work_dir/interactive-home"
    rm -f "$curl_log" "$open_log"
    mkdir -p "$interactive_home_dir"

    HOME="$interactive_home_dir" \
    PATH="$fake_bin:/usr/bin:/bin" \
    WGSEXTRACT_INSTALLER_TEST_ARCHIVE="$archive" \
    WGSEXTRACT_INSTALLER_TEST_PIXI_ARCHIVE="$fake_pixi_archive" \
    WGSEXTRACT_INSTALLER_TEST_RELEASE_TAG="$release_tag" \
    WGSEXTRACT_INSTALLER_TEST_CURL_LOG="$curl_log" \
    WGSEXTRACT_INSTALLER_TEST_OPEN_LOG="$open_log" \
    WGSEXTRACT_INSTALL_DIR="$interactive_install_dir" \
    WGSEXTRACT_ARCHIVE_URL="$archive" \
    WGSEXTRACT_PIXI_HOME="$interactive_install_dir/.pixi" \
    WGSEXTRACT_PIXI_CACHE_DIR="$interactive_install_dir/.pixi/cache" \
    WGSEXTRACT_PIXI_ENV_DIR="$interactive_install_dir/app/.pixi/envs" \
    WGSEXTRACT_OPEN_COMMAND="$fake_bin/open-install-dir" \
    sh "$repo_root/install.sh"

    test -x "$interactive_install_dir/wgsextract"
    grep -Fx "$interactive_install_dir" "$open_log" >/dev/null
fi
