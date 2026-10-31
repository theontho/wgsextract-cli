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
test_installer="$work_dir/install-under-test.sh"

rm -rf "$work_dir"
mkdir -p "$source_dir" "$home_dir" "$pixi_cache_dir" "$pixi_env_dir" "$fake_bin" "$fake_pixi_source"

cd "$repo_root"
git ls-files --cached --others --exclude-standard | while IFS= read -r path; do
    # Mirror the working tree: skip tracked files deleted but not yet committed.
    [ -e "$repo_root/$path" ] || [ -L "$repo_root/$path" ] || continue
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
        if [ "${1:-}" = "--manifest-path" ]; then
            test -f "${2:-}" || { printf 'missing manifest: %s\n' "${2:-}" >&2; exit 3; }
            shift 2
        fi
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
if command -v sha256sum >/dev/null 2>&1; then
    test_digest="$(sha256sum "$fake_pixi_archive" | awk '{print $1}')"
else
    test_digest="$(shasum -a 256 "$fake_pixi_archive" | awk '{print $1}')"
fi
# Substitute the locally trusted fixture digest in a disposable installer copy.
# Production has no checksum-bypass option.
sed "/\\.tar\\.gz) expected=/s/expected=\"[a-f0-9]*\"/expected=\"$test_digest\"/" \
    "$repo_root/install.sh" > "$test_installer"

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
sh "$test_installer"

grep -F "/releases/latest" "$curl_log" >/dev/null
grep -F "/archive/$release_tag.tar.gz" "$curl_log" >/dev/null
test -x "$install_dir/wgsextract"
test -x "$install_dir/uninstall.sh"
test -f "$install_dir/install-manifest.json"
grep -F "\"wgsextractRef\": \"$release_tag\"" "$install_dir/install-manifest.json" >/dev/null
grep -F "# WGS Extract CLI installer launcher" "$install_dir/wgsextract" >/dev/null
# The launcher must keep the caller's working directory so relative paths work.
grep -F -- "--manifest-path" "$install_dir/wgsextract" >/dev/null
if grep -E '^cd ' "$install_dir/wgsextract" >/dev/null; then
    printf 'launcher changes directory; relative input paths would break\n' >&2
    exit 1
fi
sh -n "$install_dir/uninstall.sh"
"$install_dir/wgsextract" --help >/dev/null
test -d "$install_dir/app/src/wgsextract_cli"
# A custom env dir must really hold the environments (pixi reads it from config).
grep -F "detached-environments" "$install_dir/app/.pixi/config.toml" >/dev/null
find "$pixi_env_dir" -path "*/envs/default/bin/python*" | grep . >/dev/null
# Pixi may leave app/.pixi/envs as a symlink to the detached location, but the
# environment itself must not be installed inside app/.
if [ -e "$install_dir/app/.pixi/envs" ] && [ ! -L "$install_dir/app/.pixi/envs" ]; then
    echo "Pixi environment was installed inside app/ despite detached-environments" >&2
    exit 1
fi

# Updating must keep user data that lives inside app/.
mkdir -p "$install_dir/app/reference/genomes"
printf 'genome\n' > "$install_dir/app/reference/genomes/keep.fa"
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
sh "$test_installer"
test "$(cat "$install_dir/app/reference/genomes/keep.fa")" = "genome"
"$install_dir/wgsextract" --help >/dev/null

# Uninstall without repeating the custom directories: install-layout.txt has them.
HOME="$home_dir" PATH="$fake_bin:$PATH" "$install_dir/uninstall.sh" --yes >/dev/null
test ! -e "$install_dir"
test ! -e "$pixi_env_dir"
test ! -e "$pixi_cache_dir"

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
sh "$test_installer"

test -x "$embedded_install_dir/.pixi/bin/pixi"
test -x "$embedded_install_dir/wgsextract"
"$embedded_install_dir/wgsextract" --help >/dev/null
grep -F "/pixi-" "$curl_log" >/dev/null
grep -F "\"path\":\"$embedded_install_dir/.pixi\"" "$embedded_install_dir/install-manifest.json" >/dev/null
grep -F "\"type\":\"userPathEntry\"" "$embedded_install_dir/install-manifest.json" >/dev/null

# A corrupted payload must fail before an executable reaches the install prefix.
printf 'corrupt archive' > "$fake_pixi_archive"
rejected_install_dir="$work_dir/rejected-install"
if HOME="$embedded_home_dir" \
    PATH="$fake_bin:/usr/bin:/bin" \
    WGSEXTRACT_INSTALLER_TEST_ARCHIVE="$archive" \
    WGSEXTRACT_ARCHIVE_URL="$archive" \
    WGSEXTRACT_INSTALLER_TEST_RELEASE_TAG="$release_tag" \
    WGSEXTRACT_INSTALLER_TEST_PIXI_ARCHIVE="$fake_pixi_archive" \
    WGSEXTRACT_INSTALLER_TEST_CURL_LOG="$curl_log" \
    WGSEXTRACT_INSTALL_DIR="$rejected_install_dir" \
    WGSEXTRACT_PIXI_HOME="$rejected_install_dir/.pixi" \
    sh "$test_installer" > "$work_dir/rejected.log" 2>&1; then
    printf 'corrupt Pixi archive was accepted\n' >&2
    exit 1
fi
grep -F "Pixi archive SHA-256 verification failed" "$work_dir/rejected.log" >/dev/null
test ! -e "$rejected_install_dir/.pixi/bin/pixi"
tar -czf "$fake_pixi_archive" -C "$fake_pixi_source" pixi

fish_home_dir="$work_dir/fish-home"
mkdir -p "$fish_home_dir"
printf '# Existing user configuration\n' > "$fish_home_dir/.bashrc"
cp "$fish_home_dir/.bashrc" "$work_dir/bashrc-before"
HOME="$fish_home_dir" \
SHELL="/usr/bin/fish" \
PATH="$fake_bin:/usr/bin:/bin" \
WGSEXTRACT_INSTALLER_TEST_ARCHIVE="$archive" \
WGSEXTRACT_INSTALLER_TEST_PIXI_ARCHIVE="$fake_pixi_archive" \
WGSEXTRACT_INSTALLER_TEST_RELEASE_TAG="$release_tag" \
WGSEXTRACT_INSTALLER_TEST_CURL_LOG="$curl_log" \
WGSEXTRACT_INSTALL_DIR="$work_dir/fish-install" \
WGSEXTRACT_ARCHIVE_URL="$archive" \
WGSEXTRACT_NO_OPEN=1 \
sh "$test_installer" > "$work_dir/fish.log" 2>&1
grep -F "fish_add_path" "$work_dir/fish.log" >/dev/null
test -x "$work_dir/fish-install/.pixi/bin/pixi"
test ! -e "$fish_home_dir/.pixi"
cmp "$fish_home_dir/.bashrc" "$work_dir/bashrc-before"
test ! -e "$fish_home_dir/.zshrc"

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
    sh "$test_installer"

    test -x "$interactive_install_dir/wgsextract"
    grep -Fx "$interactive_install_dir" "$open_log" >/dev/null
fi
