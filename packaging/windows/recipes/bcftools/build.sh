# shellcheck shell=bash
# shellcheck source-path=SCRIPTDIR
# shellcheck source=../env.sh
# The shared recipe is sourced through MSYS2's cygpath at build time.
# shellcheck disable=SC1091
source "$(cygpath -u "$RECIPE_DIR")/../env.sh"

# The release tarball bundles the matching htslib. Plugins need dlopen of
# DLLs built against a shared libhts, so this static build leaves them out.
build_htslib htslib-*
./configure --with-htslib="$(echo htslib-*)" --disable-bcftools-plugins \
    CFLAGS="$OPT" LDFLAGS="$STATIC_LDFLAGS" LIBS="$HTS_LIBS"
make -j"$JOBS" AR=gcc-ar bcftools
install_exe bcftools.exe
