#!/usr/bin/env bash
# Compile the exact static cache in isolation, with no production hooks.
set -euo pipefail
if (( $# != 2 )); then
    echo "Usage: $0 PATCHED_HTSLIB_SOURCE NEW_OUT_DIRECTORY" >&2
    exit 2
fi
root=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
source_dir=$(realpath "$1")
build=$(realpath -m "$2")
case "$build" in "$root"/out/*|"$root"/tmp/*) ;; *) echo 'Output must be under repository out/ or tmp/' >&2; exit 2 ;; esac
[[ ! -e $build ]]
mkdir -p "$build"
exec > >(tee "$build/validation.log") 2>&1
awk '/^\/\* Reuse large malloc-owned CRAM buffers/ { copy=1 } /^#define CRAM_DEFAULT_LEVEL/ { exit } copy' "$source_dir/cram/cram_io.c" > "$build/cram_cache.inc"
test -s "$build/cram_cache.inc"
gcc -O2 -g -fsanitize=address,undefined -fno-sanitize-recover=all \
    -I"$build" "$root/packaging/windows/tests/cram_cache.c" -pthread -o "$build/cram_cache"
"$build/cram_cache"
