# shellcheck shell=bash
# Sourced by each recipe's build.sh: the MSYS2 UCRT64 toolchain and the flags
# that make a static, self-contained Windows build of a Unix tool.
set -euo pipefail

# rattler-build runs this with MSYS2's bash; put UCRT64's GCC and static
# libraries first, whatever the caller's PATH.
export MSYSTEM=UCRT64
export PATH="/ucrt64/bin:/usr/bin:$PATH"

# rattler-build supplies the package prefix.
# shellcheck disable=SC2153
PREFIX_U=$(cygpath -u "$PREFIX")
BIN="$PREFIX_U/Library/bin"
mkdir -p "$BIN"

COMPAT=$(cygpath -u "$RECIPE_DIR")/../../compat
COMPAT_INC="$COMPAT/include"
mkdir -p "$SRC_DIR/.wgse"
COMPAT_OBJ=$(cygpath -u "$SRC_DIR")/.wgse/wgse_compat.o
gcc -O2 -Wall -D_FILE_OFFSET_BITS=64 -I"$COMPAT_INC" -c "$COMPAT/wgse_compat.c" -o "$COMPAT_OBJ"

# x86-64-v2 (SSE4.2/POPCNT; every x64 PC since ~2009) with generic tuning.
# Tools that need more pick it at run time (bwa-mem2 ships one binary per ISA).
OPT="-O3 -march=x86-64-v2 -mtune=generic"
# 64-bit off_t, and POSIX functions MinGW lacks.
# Other recipes use these flags after sourcing this file.
# shellcheck disable=SC2034
COMPAT_CPPFLAGS="-I$COMPAT_INC -include wgse_compat.h -D_FILE_OFFSET_BITS=64"
# Everything static (libgcc, libstdc++, winpthreads and the compression
# libraries), so the .exe needs only Windows' own DLLs.
STATIC_LDFLAGS="-static -s"
# Linked into every tool: besides the POSIX shims, its constructor makes all
# I/O binary, as on Unix (UCRT's binmode.o is empty).
COMPAT_LIBS="$COMPAT_OBJ -lpsapi"
JOBS=${CPU_COUNT:-4}

# install_exe NAME...: copy built executables into the package's bin and
# prove each runs and links only system DLLs.
install_exe() {
    local exe deps raw_bin
    raw_bin="$(cygpath -u "$RECIPE_DIR")/../../../../out/raw"
    mkdir -p "$raw_bin"
    for exe in "$@"; do
        install -m 755 "$exe" "$BIN/$(basename "${exe%.exe}").exe"
        install -m 755 "$exe" "$raw_bin/$(basename "${exe%.exe}").exe"
        deps=$(ldd "$BIN/$(basename "${exe%.exe}").exe" | grep -viE '/c/windows/' || true)
        if [ -n "$deps" ]; then
            echo "$exe links non-system DLLs:" >&2
            echo "$deps" >&2
            exit 1
        fi
    done
}

# build_htslib DIR: configure and build a static libhts in DIR (htslib's own
# MinGW support; regex from libsystre). Remote file access (libcurl, S3, GCS)
# and plugins are off: the CLI reads local files only.
HTS_LIBS="-lsystre -ltre -lintl -liconv $COMPAT_LIBS"
build_htslib() {
    (
        cd "$1"
        ./configure --disable-libcurl --disable-gcs --disable-s3 --disable-plugins \
            CFLAGS="$OPT" LDFLAGS="$STATIC_LDFLAGS" LIBS="$HTS_LIBS"
        make -j"$JOBS" lib-static htslib_static.mk
        cat > .wgse-cram-stop.mk <<'MAKEFILE'
include htslib_static.mk
.PHONY: codec-test
codec-test:
	$(CC) $(CFLAGS) -I. "$(CODEC_TEST)" $(HTSLIB_static_LDFLAGS) libhts.a $(HTSLIB_static_LIBS) $(CODEC_LIBS) -o .wgse-cram-stop.exe
	./.wgse-cram-stop.exe
	$(CC) $(CFLAGS) -I. "$(SEQ_TEST)" $(HTSLIB_static_LDFLAGS) libhts.a $(HTSLIB_static_LIBS) $(CODEC_LIBS) -o .wgse-bam-seq.exe
	./.wgse-bam-seq.exe
	$(CC) $(CFLAGS) -I. "$(BUFFER_TEST)" $(HTSLIB_static_LDFLAGS) libhts.a $(HTSLIB_static_LIBS) $(CODEC_LIBS) -o .wgse-cram-buffers.exe
	./.wgse-cram-buffers.exe
MAKEFILE
        make -f .wgse-cram-stop.mk codec-test CC=gcc CFLAGS="$OPT $STATIC_LDFLAGS" \
            CODEC_TEST="$(cygpath -u "$RECIPE_DIR")/../../tests/cram_stop.c" \
            SEQ_TEST="$(cygpath -u "$RECIPE_DIR")/../../tests/bam_seq.c" \
            BUFFER_TEST="$(cygpath -u "$RECIPE_DIR")/../../tests/cram_buffers.c" \
            CODEC_LIBS="$HTS_LIBS"
    )
}
