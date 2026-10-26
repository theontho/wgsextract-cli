source "$(cygpath -u "$RECIPE_DIR")/../env.sh"

make -j"$JOBS" CC=gcc \
    CFLAGS="$OPT -g0 -Wall -Wno-unused-function" \
    INCLUDES="$COMPAT_CPPFLAGS" \
    LDFLAGS="$STATIC_LDFLAGS" \
    LIBS="-lm -lz -lpthread $COMPAT_LIBS"
install_exe bwa.exe

if grep -q 'bwt_sa_many' bwt.h; then
    gcc $OPT $COMPAT_CPPFLAGS -I. \
        "$(cygpath -u "$RECIPE_DIR")/batched-sa-test.c" libbwa.a \
        -o .wgse/batched-sa-test.exe $STATIC_LDFLAGS -lm -lz -lpthread $COMPAT_LIBS
    mkdir -p out
    ./.wgse/batched-sa-test.exe --write-ref out/sa-test.fa
    ./bwa.exe index out/sa-test.fa
    ./.wgse/batched-sa-test.exe out/sa-test.fa.bwt out/sa-test.fa.sa
fi
