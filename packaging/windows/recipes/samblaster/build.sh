source "$(cygpath -u "$RECIPE_DIR")/../env.sh"

# No source changes: the compat layer supplies getline, anonymous mmap,
# getrusage and times. Its Makefile has no hook for extra objects, so build
# the two sources directly with the same flags.
for src in samblaster sbhash; do
    g++ $OPT -Wall -D BUILDNUM=26 $COMPAT_CPPFLAGS -c "$src.cpp" -o "$src.o"
done
g++ $STATIC_LDFLAGS -o samblaster.exe samblaster.o sbhash.o $COMPAT_LIBS
install_exe samblaster.exe
