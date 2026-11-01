#!/usr/bin/env bash
# Build the WGSE Arm64 GKL sidecar jar for Linux AArch64: pinned GKL 0.8.11 with
# native NEON PairHMM and Smith-Waterman vector code (patch 0002), SIMDe for the
# remaining AVX scaffolding, and the truthful loader overlay.
# Usage: build-linux-aarch64.sh WORK_DIR GATK_JAR OUT_DIR
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=fma_guard.sh disable=SC1091
. "$HERE/fma_guard.sh"
WINDOWS_RECIPE="$HERE/../../windows/recipes/gkl"
WORK=$(realpath -m "$1")
GATK_JAR=$(realpath "$2")
OUT=$(realpath -m "$3")
GKL_URL=https://github.com/IntelLabs/GKL/archive/refs/tags/0.8.11.tar.gz
GKL_SHA256=23f1aa7de874e79aedc35901397545e9e406266dfb1584f21300e440806a8950
SIMDE_URL=https://github.com/simd-everywhere/simde/archive/refs/tags/v0.8.2.tar.gz
SIMDE_SHA256=ed2a3268658f2f2a9b5367628a85ccd4cf9516460ed8604eed369653d49b25fb

fetch() { # url sha256 dest
    if [ ! -f "$3" ]; then
        curl -fsSL -o "$3.part" "$1"
        mv "$3.part" "$3"
    fi
    echo "$2  $3" | sha256sum -c --quiet
}
mkdir -p "$WORK"
fetch "$GKL_URL" "$GKL_SHA256" "$WORK/gkl-0.8.11.tar.gz"
fetch "$SIMDE_URL" "$SIMDE_SHA256" "$WORK/simde-0.8.2.tar.gz"
rm -rf "$WORK/GKL-0.8.11" "$WORK/simde-0.8.2"
tar -xzf "$WORK/gkl-0.8.11.tar.gz" -C "$WORK"
tar -xzf "$WORK/simde-0.8.2.tar.gz" -C "$WORK"
SRC="$WORK/GKL-0.8.11"
SIMDE="$WORK/simde-0.8.2"
patch -d "$SRC" -p1 --quiet --forward < "$WINDOWS_RECIPE/patches/0001-windows-avx-native.patch"
patch -d "$SRC" -p1 --quiet --forward < "$HERE/patches/0002-aarch64-neon-simde.patch"
CXX=${CXX:-g++}
JAVA_HOME=${JAVA_HOME:-$(dirname "$(dirname "$(readlink -f "$(command -v javac)")")")}

test "$(uname -m)" = aarch64
NATIVE="$SRC/src/main/native"
BUILD="$OUT/build"
RES="$BUILD/jar/com/intel/gkl/native/linux-aarch64"
rm -rf "$BUILD"
mkdir -p "$RES" "$BUILD/classes"

# -ffp-contract=off: AArch64 GCC fuses a*b+c into FMA by default, which would
# change rounding relative to the AVX kernels. armv8-a keeps every Arm64 CPU.
COMMON=(-std=c++11 -O3 -march=armv8-a -ffp-contract=off -fno-fast-math -fPIC
    -DWGSE_GKL_AVX_ONLY -Wno-ignored-attributes
    "-I$SIMDE" "-I$JAVA_HOME/include" "-I$JAVA_HOME/include/linux" "-I$NATIVE/common")
fma_guard_selftest "$CXX" objdump "$BUILD/fma-selftest" "${COMMON[@]}"
# shellcheck disable=SC2054 # commas are linker -Wl arguments, not separators
LINK=(-shared -static-libstdc++ -static-libgcc -Wl,--no-undefined -Wl,-z,noexecstack
    -Wl,--build-id=none -s)

"$CXX" "${COMMON[@]}" -fopenmp "$NATIVE/utils/utils.cc" "${LINK[@]}" -fopenmp \
    -o "$RES/libgkl_utils.so"
"$CXX" "${COMMON[@]}" "$NATIVE/pairhmm/IntelPairHmm.cc" "$NATIVE/pairhmm/avx_impl.cc" \
    "$NATIVE/pairhmm/pairhmm_common.cc" "${LINK[@]}" -o "$RES/libgkl_pairhmm.so"
"$CXX" "${COMMON[@]}" -fopenmp "$NATIVE/pairhmm/IntelPairHmm.cc" \
    "$NATIVE/pairhmm/avx_impl.cc" "$NATIVE/pairhmm/pairhmm_common.cc" "${LINK[@]}" -fopenmp \
    -o "$RES/libgkl_pairhmm_omp.so"
"$CXX" "${COMMON[@]}" "$NATIVE/smithwaterman/IntelSmithWaterman.cc" \
    "$NATIVE/smithwaterman/avx2_impl.cc" "$NATIVE/smithwaterman/smithwaterman_common.cc" \
    "${LINK[@]}" -o "$RES/libgkl_smithwaterman.so"

for lib in libgkl_utils libgkl_pairhmm libgkl_pairhmm_omp libgkl_smithwaterman; do
    so="$RES/$lib.so"
    # Capture before grep -q: an early grep exit would SIGPIPE the producer under pipefail.
    hdr=$(readelf -h "$so")
    grep -q 'Machine:.*AArch64' <<<"$hdr"
    syms=$(nm -D --defined-only "$so")
    grep -q ' Java_com_intel_gkl_' <<<"$syms"
    # Only glibc (libc, libm, loader) and libgomp may be dynamic; libstdc++ is static.
    deps=$(readelf -d "$so" | sed -n 's/.*Shared library: \[\(.*\)\]/\1/p' |
        grep -vE '^(libc|libm|libgomp)\.so|^ld-linux-aarch64\.so' || true)
    if [ -n "$deps" ]; then
        echo "$lib links unexpected libraries: $deps" >&2
        exit 1
    fi
    # Bit-exactness guard: no fused multiply-add anywhere in the library.
    reject_fma objdump "$so"
done

"$CXX" "${COMMON[@]}" -std=c++17 -fopenmp "$HERE/tests/fp_semantics.cc" -o "$BUILD/fp_semantics"
"$CXX" "${COMMON[@]}" -fopenmp "$HERE/tests/omp_fpcr_state.cc" -o "$BUILD/omp_fpcr_state"
# Negative control: the pre-fix caller guard must visibly leak FZ into libgomp's pool,
# otherwise the cold-pool check above proves nothing.
status=0
"$BUILD/omp_fpcr_state" legacy || status=$?
if [ "$status" != 2 ]; then
    echo "omp_fpcr_state legacy did not reproduce the FZ pool leak (status $status)" >&2
    exit 1
fi
"$BUILD/omp_fpcr_state"

JAVA_SRC="$SRC/src/main/java/com/intel/gkl"
"$JAVA_HOME/bin/javac" --release 17 -nowarn -cp "$GATK_JAR" -d "$BUILD/classes" \
    "$JAVA_SRC/IntelGKLUtils.java" "$JAVA_SRC/NativeLibraryLoader.java" \
    "$JAVA_SRC/pairhmm/IntelPairHmm.java" "$JAVA_SRC/pairhmm/IntelPairHmmOMP.java" \
    "$JAVA_SRC/smithwaterman/IntelSmithWaterman.java"
cp -r "$BUILD/classes/." "$BUILD/jar/"
mkdir -p "$BUILD/jar/META-INF/licenses/gkl" "$BUILD/jar/META-INF/licenses/simde" \
    "$BUILD/jar/META-INF/licenses/gcc-runtime"
cp "$SRC/LICENSE" "$BUILD/jar/META-INF/licenses/gkl/"
cp "$SIMDE/COPYING" "$BUILD/jar/META-INF/licenses/simde/"
gcc_lic=$(find /usr/share/doc/libstdc++6 /usr/share/doc/gcc-*-base -maxdepth 1 \
    -name copyright 2>/dev/null | head -n 1)
test -n "$gcc_lic" && cp "$gcc_lic" "$BUILD/jar/META-INF/licenses/gcc-runtime/copyright"
find "$BUILD/jar" -exec touch -h -d 2024-01-01T00:00:00Z {} +
mkdir -p "$OUT"
"$JAVA_HOME/bin/jar" --create --date=2024-01-01T00:00:00Z \
    --file "$OUT/gkl-native-wgse-linux-aarch64.jar" -C "$BUILD/jar" .
sha256sum "$OUT/gkl-native-wgse-linux-aarch64.jar" "$RES"/*.so
