# Native HTS patch validation

The Windows recipes run `cram_stop.c`, `bam_seq.c` and `cram_buffers.c` against
all three patched HTS builds. On Linux/glibc with GCC, run the same tests under
ASan/UBSan from a fresh, checksum-verified HTSlib 1.24 release archive:

```sh
packaging/windows/tests/validate_sanitizers.sh /path/to/htslib-1.24.tar.bz2 out/hts-sanitizers 2
```

The runner requires installed compiler/build dependencies, downloads nothing,
and refuses existing output directories. It also runs `cram_cache.c` against
the exact cache implementation extracted from the patched source. This checks
retained-memory and size-class caps, min/max bypass, the 12.5% reuse boundary,
exclusive ownership and shutdown without adding production instrumentation.
Expected stale-pointer and logical-tail violations must produce ASan's
`use-after-poison` diagnostic; an unrelated crash fails validation. Logs and
build products stay in the supplied ignored output directory. A full sanitizer
build takes several minutes. To run only the cache checks (<1 second):

```sh
packaging/windows/tests/validate_cache.sh /path/to/patched-htslib out/cache-invariants
```

For the optional single-sample bulk-copy experiment, supply pristine bcftools
1.24 source, the isolated candidate patch, and that sanitizer HTSlib build:

```sh
packaging/windows/tests/validate_group_copy.sh /path/to/bcftools-1.24 \
  /path/to/0004-bulk-copy-single-sample.patch out/hts-sanitizers out/group-copy
```

This leaves recipes untouched. The oracle compiles the original and candidate
`group_smpl` bodies with real pileup types, compares empty/repeated calls and
allocation growth boundaries, and checks multi-input/multisample fallback.
It uses bcftools' real sample selector to check that an excluded read group is
removed before grouping, matching `mpileup_read`'s negative-sample-ID filter.
This checks the fast path's sample-zero assumption; it does not replace
end-to-end bcftools or full-genome validation. Compilation and execution take
seconds when the sanitized HTSlib build already exists.
