#!/usr/bin/env bash
# Linux/glibc + GCC. Uses a supplied archive; never downloads or installs.
set -euo pipefail
if (( $# < 2 || $# > 3 )); then
    echo "Usage: $0 HTSLIB_1.24_TARBALL NEW_OUT_DIRECTORY [JOBS]" >&2
    exit 2
fi
root=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
tests="$root/packaging/windows/tests"
archive=$(realpath "$1")
build=$(realpath -m "$2")
jobs=${3:-2}
[[ $jobs =~ ^[1-9][0-9]*$ ]]
case "$build" in "$root"/out/*|"$root"/tmp/*) ;; *) echo 'Output must be under repository out/ or tmp/' >&2; exit 2 ;; esac
for tool in gcc make tar patch sha256sum awk; do command -v "$tool" >/dev/null; done
printf '%s  %s\n' 28a8de191381c7a97a35675ceac76fa1ea95e7b678d6a2e9d600a7874e4077de "$archive" | sha256sum --check
# Require fresh sources: stale configuration can silently disable sanitizers.
[[ ! -e $build ]]
mkdir -p "$build"
exec > >(tee "$build/validation.log") 2>&1
tar -xjf "$archive" --strip-components=1 -C "$build"
for name in 0001-bounded-cram-stop-copy 0002-vector-bam-sequence-packing 0003-reuse-cram-buffers; do
    patch --batch --forward -p1 -d "$build" < "$root/packaging/windows/recipes/htslib/patches/$name.patch"
done
cd "$build"
flags=(-O2 -g -march=x86-64-v2 -DHTS_ALLOW_UNALIGNED=0 '-fsanitize=address,undefined' -fno-sanitize-recover=all -fno-omit-frame-pointer)
# Verify the macro selecting the glibc test path before the expensive build.
printf '#include <features.h>\n#if !defined(__GLIBC__) || !defined(__SANITIZE_ADDRESS__)\n#error Requires glibc and GCC ASan\n#endif\nint main(void){return 0;}\n' > capability.c
gcc "${flags[@]}" capability.c -o capability
./capability
./configure --disable-libcurl --disable-gcs --disable-s3 --disable-plugins \
    CFLAGS="${flags[*]}" LDFLAGS='-fsanitize=address,undefined'
make -j"$jobs" lib-static htslib_static.mk
cat > validation.mk <<'MAKEFILE'
include htslib_static.mk
.PHONY: validation
validation:
	$(CC) $(CFLAGS) -I. "$(TEST)" $(HTSLIB_static_LDFLAGS) libhts.a $(HTSLIB_static_LIBS) -o "$(OUTPUT)"
MAKEFILE
for test in cram_stop bam_seq cram_buffers; do
    make -f validation.mk validation CC=gcc CFLAGS="${flags[*]}" TEST="$tests/$test.c" OUTPUT="$test"
    "./$test"
done
"$tests/validate_cache.sh" "$build" "$build/cache-invariants"
for probe in uaf bounds; do
    status=0
    ./cram_buffers "--$probe-probe" > "$probe-probe.log" 2>&1 || status=$?
    [[ $status -ne 0 ]]
    # A crash without the intended ASan diagnostic does not pass.
    grep -q 'AddressSanitizer: use-after-poison' "$probe-probe.log"
done
echo 'All sanitizer tests and stale-pointer/logical-boundary negative probes passed'
