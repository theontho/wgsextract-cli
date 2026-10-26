#!/usr/bin/env bash
# Optional diagnostic: does not install the candidate patch in any recipe.
set -euo pipefail
if (( $# != 4 )); then
    echo "Usage: $0 PRISTINE_BCFTOOLS_1.24_DIR CANDIDATE_PATCH SANITIZED_HTSLIB_BUILD NEW_OUT_DIR" >&2
    exit 2
fi
root=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
source_dir=$(realpath "$1")
candidate_patch=$(realpath "$2")
hts=$(realpath "$3")
build=$(realpath -m "$4")
case "$build" in "$root"/out/*|"$root"/tmp/*) ;; *) echo 'Output must be under repository out/ or tmp/' >&2; exit 2 ;; esac
for tool in gcc make patch awk; do command -v "$tool" >/dev/null; done
test -f "$source_dir/mpileup.c"
test -f "$source_dir/bam_sample.c"
test -f "$hts/htslib_static.mk"
[[ ! -e $build ]]
mkdir -p "$build"
exec > >(tee "$build/validation.log") 2>&1
cp "$source_dir/mpileup.c" "$build/mpileup.c"
patch --batch --forward -p1 -d "$build" < "$candidate_patch"
extract() {
    # group_smpl has no nested braces starting at column 1. Fail if its
    # signature is missing; a source layout change requires review.
    awk '/^static void group_smpl\(/ { copy=1 } copy { print } copy && /^}/ { exit }' "$1" > "$2"
    test -s "$2"
}
extract "$source_dir/mpileup.c" "$build/group_original.inc"
extract "$build/mpileup.c" "$build/group_candidate.inc"
if cmp -s "$build/group_original.inc" "$build/group_candidate.inc"; then
    echo 'Candidate did not change group_smpl' >&2
    exit 1
fi
cat > "$build/validation.mk" <<'MAKEFILE'
include $(HTS)/htslib_static.mk
.PHONY: validation
validation:
	gcc $(FLAGS) -I"$(HTS)" -I"$(SOURCE)" -I"$(BUILD)" "$(TEST)" "$(SOURCE)/bam_sample.c" "$(HTS)/libhts.a" $(HTSLIB_static_LIBS) -o "$(BUILD)/group_copy"
MAKEFILE
flags='-O2 -g -march=x86-64-v2 -DHTS_ALLOW_UNALIGNED=0 -fsanitize=address,undefined -fno-sanitize-recover=all -fno-omit-frame-pointer'
make -C "$hts" -f "$build/validation.mk" validation FLAGS="$flags" \
    HTS="$hts" SOURCE="$source_dir" BUILD="$build" TEST="$root/packaging/windows/tests/group_copy.c"
"$build/group_copy"
