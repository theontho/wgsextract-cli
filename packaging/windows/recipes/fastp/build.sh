source "$(cygpath -u "$RECIPE_DIR")/../env.sh"

# fastp counts reads and bases in `long`, which is 64-bit on Linux and macOS
# but 32-bit on Windows: a 30x genome (~90 Gbases) overflows it. Widen every
# `long` type to `long long` (and ftell/fseek to their 64-bit forms), outside
# string literals and // comments, so the Windows build counts like the others.
perl -pi -e '
    s{("(?:[^"\\]|\\.)*")|(//.*$)|(?<!long )\blong\b(?!\s+(?:long|double)\b)|\bftell\(|\bfseek\(}{
        defined $1 ? $1 : defined $2 ? $2
        : $& eq "long" ? "long long" : $& eq "ftell(" ? "ftello(" : "fseeko("
    }ge' src/*.cpp src/*.h
grep -q "long long mBases;" src/stats.h || { echo "fastp: LLP64 rewrite didn't apply" >&2; exit 1; }
if grep -nE '(^|[^g] )long [A-Za-z_]' src/*.cpp src/*.h | grep -vE '"|//|long long|long double'; then
    echo "fastp: LLP64 rewrite missed a declaration" >&2
    exit 1
fi

# Static libraries: pkg-config's flags ask for the DLLs (HWY_SHARED_DEFINE,
# LIBDEFLATE_DLL), so leave them out and define Highway's static build.
CXXFLAGS="$OPT -g0 $COMPAT_CPPFLAGS -DHWY_STATIC_DEFINE" make -j"$JOBS" CXX=g++ HWY_CFLAGS= DEFLATE_CFLAGS= ISAL_CFLAGS= \
    LD_FLAGS="$STATIC_LDFLAGS -lisal -ldeflate -lhwy -lpthread $COMPAT_LIBS" fastp
install_exe fastp.exe
