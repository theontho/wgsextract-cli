source "$(cygpath -u "$RECIPE_DIR")/../env.sh"
INC=$(cygpath -u "$RECIPE_DIR")/include

# One build per instruction set, like upstream's `make multi`, without
# safestringlib (the UCRT has the functions; see include/).
for isa in sse41 sse42 avx avx2 avx512; do
    rm -f src/*.o libbwa.a
    exe=bwa-mem2.$isa
    [ "$isa" = avx512 ] && exe=bwa-mem2.avx512bw
    CXXFLAGS="-g0" make -j"$JOBS" arch=$isa EXE=$exe.exe CXX=g++ SAFE_STR_LIB= \
        CPPFLAGS="-DENABLE_PREFETCH -DV17=1 -DMATE_SORT=0 -DSAIS=1 $COMPAT_CPPFLAGS" \
        INCLUDES="-Isrc -I$INC" \
        LIBS="-L. -lbwa -lpthread -lm -lz $COMPAT_LIBS $STATIC_LDFLAGS" \
        $exe.exe
    install_exe $exe.exe
done

# The launcher picks one at run time (see runsimd_win.c).
gcc -O2 -Wall -static -s "$(cygpath -u "$RECIPE_DIR")/runsimd_win.c" -o bwa-mem2.exe
install_exe bwa-mem2.exe
