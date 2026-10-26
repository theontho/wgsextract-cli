# Native HTS patch validation

The Windows recipes run `cram_stop.c`, `bam_seq.c` and `cram_buffers.c` against
all three patched HTS builds. Linux/glibc with GCC can also check the cache's
exact static implementation without adding production test hooks:

```sh
packaging/windows/tests/validate_cache.sh /path/to/patched-htslib out/cache-invariants
```

The ASan/UBSan harness checks retained-memory and size-class caps, min/max
bypass, the 12.5% reuse boundary, exclusive ownership and shutdown. It takes
less than a second and writes all artifacts under the fresh output directory.
It requires installed build tools and downloads nothing.

For the optional single-sample bulk-copy experiment, supply pristine bcftools
1.24 source, the isolated candidate patch, and an existing sanitized HTSlib build:

```sh
packaging/windows/tests/validate_group_copy.sh /path/to/bcftools-1.24 \
  /path/to/0004-bulk-copy-single-sample.patch /path/to/sanitized-htslib out/group-copy
```

This leaves recipes untouched. The oracle compiles the original and candidate
`group_smpl` bodies with real pileup types, compares empty/repeated calls and
allocation growth boundaries, and checks multi-input/multisample fallback.
It uses bcftools' real sample selector to check that an excluded read group is
removed before grouping, matching `mpileup_read`'s negative-sample-ID filter.
This checks the fast path's sample-zero assumption; it does not replace
end-to-end bcftools or full-genome validation. Compilation and execution take
seconds when the sanitized HTSlib build already exists.
