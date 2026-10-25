source "$(cygpath -u "$RECIPE_DIR")/../env.sh"

make -j"$JOBS" CC=gcc \
    CFLAGS="$OPT -g0 -Wall -Wno-unused-function" \
    INCLUDES="$COMPAT_CPPFLAGS" \
    LDFLAGS="$STATIC_LDFLAGS" \
    LIBS="-lm -lz -lpthread $COMPAT_LIBS"
install_exe bwa.exe
