source "$(cygpath -u "$RECIPE_DIR")/../env.sh"

build_htslib .
make -j"$JOBS" bgzip htsfile tabix
install_exe bgzip.exe htsfile.exe tabix.exe
