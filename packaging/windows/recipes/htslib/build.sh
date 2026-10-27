# shellcheck shell=bash
# shellcheck source-path=SCRIPTDIR
# shellcheck source=../env.sh
# The shared recipe is sourced through MSYS2's cygpath at build time.
# shellcheck disable=SC1091
source "$(cygpath -u "$RECIPE_DIR")/../env.sh"

build_htslib .
make -j"$JOBS" AR=gcc-ar bgzip htsfile tabix
install_exe bgzip.exe htsfile.exe tabix.exe
