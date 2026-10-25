source "$(cygpath -u "$RECIPE_DIR")/../env.sh"

# The release tarball bundles the matching htslib. Plugins need dlopen of
# DLLs built against a shared libhts, so this static build leaves them out.
build_htslib htslib-*
./configure --with-htslib="$(echo htslib-*)" --disable-bcftools-plugins \
    CFLAGS="$OPT" LDFLAGS="$STATIC_LDFLAGS" LIBS="$HTS_LIBS"
make -j"$JOBS" bcftools
install_exe bcftools.exe
