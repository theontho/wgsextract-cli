#!/usr/bin/env bash
# Build and run tests/kbench.cc against a patched GKL native tree.
# Usage: kbench.sh GKL_NATIVE_DIR SIMDE_DIR OUT_DIR [CASES] [REPS]
# SIMDE_DIR is only used on AArch64. Uses $CXX (default g++).
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
N=$(realpath "$1"); S=$(realpath -m "$2"); O=$(realpath -m "$3")
if command -v cygpath > /dev/null; then # MSYS2: native compilers need Windows paths
    HERE=$(cygpath -m "$HERE"); N=$(cygpath -m "$N"); S=$(cygpath -m "$S"); O=$(cygpath -m "$O")
fi
CXX=${CXX:-g++}
rm -rf "$O"; mkdir -p "$O"
# Ask the compiler: MSYS2 shells on Windows Arm64 are x64 processes (uname -m lies).
if [[ $("$CXX" -dumpmachine) == *aarch64* ]]; then
    ARCH=(-march=armv8-a "-I$S"); AVX=(); AVX2=()
else
    ARCH=(); AVX=(-mavx); AVX2=(-mavx -mavx2)
fi
F=(-std=c++11 -O3 -ffp-contract=off -fno-fast-math -Wno-ignored-attributes -DWGSE_GKL_AVX_ONLY
    "${ARCH[@]}" "-I$N" "-I$N/common" "-I$N/pairhmm" "-I$N/smithwaterman")
"$CXX" "${F[@]}" "${AVX[@]}" -c "$N/pairhmm/avx_impl.cc" -o "$O/avx_impl.o"
"$CXX" "${F[@]}" "${AVX2[@]}" -c "$N/smithwaterman/avx2_impl.cc" -o "$O/avx2_impl.o"
"$CXX" "${F[@]}" -c "$N/pairhmm/pairhmm_common.cc" -o "$O/pairhmm_common.o"
"$CXX" "${F[@]}" -c "$N/smithwaterman/smithwaterman_common.cc" -o "$O/smithwaterman_common.o"
"$CXX" "${F[@]}" "${AVX[@]}" -c "$HERE/kbench.cc" -o "$O/kbench.o"
"$CXX" "$O"/*.o -o "$O/kbench"
# Input self-test: a missing, empty, odd-length or blank-line SW input must fail, not hash.
printf '' > "$O/sw-empty.in"
printf 'ACGT\n' > "$O/sw-odd.in"
printf 'ACGT\n\n' > "$O/sw-blank.in"
for bad in "$O/sw-missing.in" "$O/sw-empty.in" "$O/sw-odd.in" "$O/sw-blank.in"; do
    if "$O/kbench" "$bad" 1 1 > /dev/null 2>&1; then
        echo "kbench accepted invalid SW input $bad" >&2
        exit 1
    fi
done
echo "kbench SW input self-test passed"
"$O/kbench" "$(dirname "$N")/../test/resources/smith-waterman.SOFTCLIP.in" "${4:-20000}" "${5:-3}"
