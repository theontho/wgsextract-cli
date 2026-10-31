# Native Arm64 GKL sidecar (prototype, issue #236)

GATK 4.6.2 only accelerates HaplotypeCaller when GKL reports AVX. On Arm64 it
otherwise falls back to the Java PairHMM and Smith-Waterman. This recipe ports
the pinned GKL 0.8.11 AVX PairHMM and AVX2 Smith-Waterman kernels to AArch64.
The hot loops are native NEON with the same per-cell IEEE operation order, so
the results are bit-identical to x64. The pinned header-only SIMDe v0.8.2
supplies the remaining AVX scaffolding. The recipe also adds a loader overlay
that reports Arm support truthfully.

- `patches/0002-aarch64-neon-simde.patch` applies on top of the Windows
  `0001-windows-avx-native.patch`. It adds:
  - `common/simd_compat.h`: the SIMDe include, `_mm_malloc` and FPCR-backed
    `_MM_SET_FLUSH_ZERO_MODE`;
  - `pairhmm/neon-pairhmm.h`: the native NEON float/double PairHMM, used by
    `avx_impl.cc` (the float→double fallback is unchanged);
  - a native NEON `VEC_*` macro set in `smithwaterman/avx2-functions.h` for the
    unchanged `PairWiseSW.h`;
  - an FPCR branch in `ScopedFtzDaz`;
  - `is_neon_supported()` plus the JNI `isNeonSupportedNative`;
  - NEON gates in `IntelPairHmm`/`IntelSmithWaterman`;
  - `native/linux-aarch64/` and `native/windows-aarch64/` resource directories in
    `NativeLibraryLoader`.

  x86 behaviour and the x86 resource path (`native/`) are unchanged.
- `build-linux-aarch64.sh WORK GATK_JAR OUT` does the following:
  - fetches the GKL and SIMDe sources and verifies their SHA-256;
  - applies both patches;
  - builds `libgkl_{utils,pairhmm,pairhmm_omp,smithwaterman}.so` with
    `-march=armv8-a -ffp-contract=off`, static libstdc++/libgcc and dynamic
    glibc/libgomp;
  - rejects unexpected dynamic dependencies and any fused multiply-add instruction;
  - runs the OpenMP FPCR test;
  - writes a reproducible `gkl-native-wgse-linux-aarch64.jar` (patched classes,
    `.so` files and licences).
- Launch: `java -cp gkl-native-wgse-linux-aarch64.jar:gatk-package-4.6.2.0-local.jar org.broadinstitute.hellbender.Main HaplotypeCaller ...`.
  The sidecar classes shadow GATK's bundled GKL classes. With the stock GATK
  jar alone, Arm64 keeps the Java fallback. Compression stays on the JDK
  deflater/inflater.

Tests (`tests/`):

- `fp_semantics.cc`: reports control-register flush/denormal/tininess behaviour and
  libm fingerprints.
- `omp_fpcr_state.cc`: checks per-worker FZ and full FPCR restoration.
- `GklVectorProbe.java`: native float/double and Java `LoglessPairHMM` bits over
  GKL test vectors and seeded random batches, plus all 14,196 GKL Smith-Waterman
  test pairs × 4 overhang strategies.
- `kbench.cc` / `kbench.sh GKL_NATIVE_DIR SIMDE_DIR OUT`: kernel-only timing
  and exact-bit hashes over 20,000 seeded PairHMM cases and the GKL SW pairs.
  x86-64 and AArch64 must print the same `hash_*` values.
- `compare_vectors.py`: compares the Arm64 probe output against the x64 output.
- `JavaThreadFtzProbe.java`: checks whether initialisation leaves flush-to-zero on
  the JVM thread.
- `packaging/windows/recipes/gkl/tests/GklParityProbe.java` accepts a third argument,
  `neon`, so that it can qualify this sidecar against `linux-avx2-golden.tsv`.

See `DESIGN.md` for the design rationale; the prototype results are in
`out/arm64/gkl/prototype-results.md` of the issue #236 work. Windows Arm64 is
designed but not built here.
