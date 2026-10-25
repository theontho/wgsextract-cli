source "$(cygpath -u "$RECIPE_DIR")/../env.sh"

# The release tarball bundles the matching htslib.
build_htslib htslib-*
./configure --with-htslib="$(echo htslib-*)" --without-curses \
    CFLAGS="$OPT" LDFLAGS="$STATIC_LDFLAGS" LIBS="$HTS_LIBS"
make -j"$JOBS" samtools
install_exe samtools.exe
