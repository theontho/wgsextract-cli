#!/usr/bin/env bash
# Build the WGSE Arm64 GKL sidecar jar for Windows on Arm (win-arm64): pinned GKL
# 0.8.11 with the native NEON PairHMM and Smith-Waterman kernels (patch 0002), SIMDe
# for the remaining AVX scaffolding, and the truthful loader overlay.
# Run in an MSYS2 CLANGARM64 shell on a Windows Arm64 host; the compiler, linker and
# all outputs are native AArch64 PE (machine 0xAA64).
# Usage: build-windows-arm64.sh WORK_DIR GATK_JAR OUT_DIR
set -euo pipefail

# Native (non-MSYS) tools need Windows paths; mixed C:/x form also works in bash.
HERE=$(cygpath -m "$(cd "$(dirname "$0")" && pwd)")
WINDOWS_RECIPE="$HERE/../../windows/recipes/gkl"
WORK=$(cygpath -m "$(realpath -m "$1")")
GATK_JAR=$(cygpath -m "$(realpath "$2")")
OUT=$(cygpath -m "$(realpath -m "$3")")
GKL_URL=https://github.com/IntelLabs/GKL/archive/refs/tags/0.8.11.tar.gz
GKL_SHA256=23f1aa7de874e79aedc35901397545e9e406266dfb1584f21300e440806a8950
SIMDE_URL=https://github.com/simd-everywhere/simde/archive/refs/tags/v0.8.2.tar.gz
SIMDE_SHA256=ed2a3268658f2f2a9b5367628a85ccd4cf9516460ed8604eed369653d49b25fb

test "${MSYSTEM:-}" = CLANGARM64
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
tar --force-local -xzf "$WORK/gkl-0.8.11.tar.gz" -C "$WORK"
tar --force-local -xzf "$WORK/simde-0.8.2.tar.gz" -C "$WORK"
SRC="$WORK/GKL-0.8.11"
SIMDE="$WORK/simde-0.8.2"
patch -d "$SRC" -p1 --quiet --forward < "$WINDOWS_RECIPE/patches/0001-windows-avx-native.patch"
patch -d "$SRC" -p1 --quiet --forward < "$HERE/patches/0002-aarch64-neon-simde.patch"
CXX=${CXX:-clang++}
JAVA_HOME=$(cygpath -m "${JAVA_HOME:?set JAVA_HOME to a Windows Arm64 JDK 17}")

NATIVE="$SRC/src/main/native"
BUILD="$OUT/build"
RES="$BUILD/jar/com/intel/gkl/native/windows-aarch64"
rm -rf "$BUILD"
mkdir -p "$RES" "$BUILD/classes"

# -ffp-contract=off: clang fuses a*b+c into FMA by default on AArch64, which would
# change rounding relative to the AVX kernels. armv8-a keeps every Arm64 CPU.
COMMON=(--target=aarch64-w64-windows-gnu -std=c++11 -O3 -march=armv8-a -ffp-contract=off
    -fno-fast-math -DWGSE_GKL_AVX_ONLY -Wno-ignored-attributes
    "-I$SIMDE" "-I$JAVA_HOME/include" "-I$JAVA_HOME/include/win32" "-I$NATIVE/common")
# -static: libc++, libunwind, compiler-rt and winpthreads are linked in. LLVM OpenMP is
# the exception: gkl_utils and gkl_pairhmm_omp both load into the JVM, and two static
# libomp copies in one process abort (OMP Error #15). Both import the shipped
# libomp.dll, which the NativeLibraryLoader overlay preloads under its exact name.
# shellcheck disable=SC2054 # commas are linker -Wl arguments, not separators
LINK=(-shared -static -Wl,--no-insert-timestamp -s)

OBJ="$BUILD/obj"
mkdir -p "$OBJ"
compile() { # variant [flags...] -- sources...
    local variant=$1 flags=() src
    shift
    while [ "$1" != -- ]; do flags+=("$1"); shift; done
    shift
    for src in "$@"; do
        "$CXX" "${COMMON[@]}" "${flags[@]}" -c "$NATIVE/$src.cc" \
            -o "$OBJ/$variant-$(basename "$src").o"
    done
}
compile utils -fopenmp -- utils/utils
compile pairhmm -- pairhmm/IntelPairHmm pairhmm/avx_impl pairhmm/pairhmm_common
compile pairhmm_omp -fopenmp -- pairhmm/IntelPairHmm pairhmm/avx_impl pairhmm/pairhmm_common
compile smithwaterman -- smithwaterman/IntelSmithWaterman smithwaterman/avx2_impl \
    smithwaterman/smithwaterman_common
# Bit-exactness guard on the GKL code itself: no fused multiply-add outside
# wgse_libm, whose explicit fma calls reproduce glibc's log10/log10f/log1p/pow.
# (The static CRT's printf float formatter, __gdtoa, legitimately uses FMA and is
# not on any kernel path.)
for o in "$OBJ"/*.o; do
    fused=$(llvm-objdump -d -C "$o" | awk '/^[0-9a-f]+ <.*>:$/ { fn = $0 }
        /[[:space:]](fmla|fmls|fmadd|fmsub|fnmadd|fnmsub)[[:space:]]/ && fn !~ /wgse_libm::/ { print fn }' | sort -u)
    if [ -n "$fused" ]; then
        echo "$o contains fused multiply-add instructions in: $fused" >&2
        exit 1
    fi
done
LIBOMP="$MINGW_PREFIX/lib/libomp.dll.a"
"$CXX" "$OBJ"/utils-*.o "${LINK[@]}" "$(cygpath -m "$LIBOMP")" -o "$RES/gkl_utils.dll"
"$CXX" "$OBJ"/pairhmm-*.o "${LINK[@]}" -o "$RES/gkl_pairhmm.dll"
"$CXX" "$OBJ"/pairhmm_omp-*.o "${LINK[@]}" "$(cygpath -m "$LIBOMP")" \
    -o "$RES/gkl_pairhmm_omp.dll"
cp "$MINGW_PREFIX/bin/libomp.dll" "$RES/libomp.dll"
"$CXX" "$OBJ"/smithwaterman-*.o "${LINK[@]}" -o "$RES/gkl_smithwaterman.dll"

SYSTEM_DLLS='^(kernel32|advapi32|psapi|user32|ucrtbase|msvcrt|api-ms-win-[a-z0-9-]+)\.dll$'
for lib in libomp gkl_utils gkl_pairhmm gkl_pairhmm_omp gkl_smithwaterman; do
    dll="$RES/$lib.dll"
    llvm-readobj --file-headers "$dll" | grep -q 'Machine: IMAGE_FILE_MACHINE_ARM64 (0xAA64)'
    llvm-objdump -p "$dll" > "$BUILD/$lib-pe.txt"
    [ "$lib" = libomp ] || grep -q 'Java_com_intel_gkl_' "$BUILD/$lib-pe.txt"
    allowed=$SYSTEM_DLLS
    case $lib in gkl_utils | gkl_pairhmm_omp) allowed="$SYSTEM_DLLS|^libomp\.dll\$" ;; esac
    deps=$(sed -n 's/^ *DLL Name: //p' "$BUILD/$lib-pe.txt" | tr '[:upper:]' '[:lower:]' |
        grep -vE "$allowed" || true)
    if [ -n "$deps" ]; then
        echo "$lib imports non-system DLLs: $deps" >&2
        exit 1
    fi
done

"$CXX" "${COMMON[@]}" -std=c++17 -fopenmp "$HERE/tests/fp_semantics.cc" -static -fopenmp \
    -o "$BUILD/fp_semantics.exe"
"$CXX" "${COMMON[@]}" -fopenmp "$HERE/tests/omp_fpcr_state.cc" -static -fopenmp \
    -o "$BUILD/omp_fpcr_state.exe"
"$BUILD/omp_fpcr_state.exe"

JAVA_SRC="$SRC/src/main/java/com/intel/gkl"
"$JAVA_HOME/bin/javac" --release 17 -nowarn -cp "$GATK_JAR" -d "$BUILD/classes" \
    "$JAVA_SRC/IntelGKLUtils.java" "$JAVA_SRC/NativeLibraryLoader.java" \
    "$JAVA_SRC/pairhmm/IntelPairHmm.java" "$JAVA_SRC/pairhmm/IntelPairHmmOMP.java" \
    "$JAVA_SRC/smithwaterman/IntelSmithWaterman.java"
cp -r "$BUILD/classes/." "$BUILD/jar/"
L="$BUILD/jar/META-INF/licenses"
mkdir -p "$L/gkl" "$L/simde" "$L/llvm-runtime" "$L/mingw-w64"
cp "$SRC/LICENSE" "$L/gkl/"
cp "$SIMDE/COPYING" "$L/simde/"
cp -r "$HERE/licenses/wgse_libm" "$L/"
# Statically linked runtimes libc++/libunwind/compiler-rt and the shipped libomp.dll
# (Apache-2.0 WITH LLVM-exception), plus the mingw-w64 CRT and winpthreads.
for pkg in libc++ libunwind compiler-rt openmp; do
    d="$MINGW_PREFIX/share/licenses/$pkg"
    if [ -d "$d" ]; then mkdir -p "$L/llvm-runtime/$pkg" && cp -r "$d/." "$L/llvm-runtime/$pkg/"; fi
done
for pkg in crt winpthreads headers; do
    d="$MINGW_PREFIX/share/licenses/$pkg"
    if [ -d "$d" ]; then mkdir -p "$L/mingw-w64/$pkg" && cp -r "$d/." "$L/mingw-w64/$pkg/"; fi
done
test -n "$(ls -A "$L/llvm-runtime")" && test -n "$(ls -A "$L/mingw-w64")"
find "$BUILD/jar" -exec touch -h -d 2024-01-01T00:00:00Z {} +
mkdir -p "$OUT"
"$JAVA_HOME/bin/jar" --create --date=2024-01-01T00:00:00Z \
    --file "$OUT/gkl-native-wgse-windows-aarch64.jar" -C "$BUILD/jar" .
sha256sum "$OUT/gkl-native-wgse-windows-aarch64.jar" "$RES"/*.dll
