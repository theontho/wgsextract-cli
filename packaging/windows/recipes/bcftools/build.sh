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

# Check the actual patched grouping function against the release's original,
# including multisample fallback and sample selection, before packaging it.
mkdir -p .wgse
patch --batch --reverse -p1 --output=.wgse/original-mpileup.c \
    < "$(cygpath -u "$RECIPE_DIR")/patches/0004-bulk-copy-single-sample.patch"
for version in original candidate; do
    source_file=mpileup.c
    if [[ $version == original ]]; then source_file=.wgse/original-mpileup.c; fi
    awk '/^static void group_smpl\(/ { copy=1 } copy { print } copy && /^}/ { exit }' \
        "$source_file" > ".wgse/group_$version.inc"
    test -s ".wgse/group_$version.inc"
done
cat > .wgse/group.mk <<'MAKEFILE'
include $(wildcard htslib-*/htslib_static.mk)
.PHONY: group-test
group-test:
	$(CC) $(CFLAGS) -I. -I.wgse -I"$(HTSDIR)" "$(GROUP_TEST)" bam_sample.c "$(HTSDIR)/libhts.a" $(HTSLIB_static_LDFLAGS) $(HTSLIB_static_LIBS) $(GROUP_LIBS) -o .wgse/group-copy.exe
	./.wgse/group-copy.exe
MAKEFILE
make -f .wgse/group.mk group-test CC=gcc CFLAGS="$OPT $STATIC_LDFLAGS" \
    HTSDIR="$(echo htslib-*)" GROUP_LIBS="$HTS_LIBS" \
    GROUP_TEST="$(cygpath -u "$RECIPE_DIR")/../../tests/group_copy.c"
install_exe bcftools.exe
