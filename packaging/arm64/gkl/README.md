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
  - rejects unexpected dynamic dependencies and any fused multiply-add instruction,
    using the fail-closed guard in `fma_guard.sh` after its self-test proves it
    rejects an FMA-bearing sample;
  - runs the OpenMP FPCR test;
  - writes a reproducible `gkl-native-wgse-linux-aarch64.jar` (patched classes,
    `.so` files and licences).
- `build-windows-arm64.sh WORK GATK_JAR OUT` is the win-arm64 counterpart. Run it
  in an MSYS2 CLANGARM64 shell on Windows 11 Arm64 with `JAVA_HOME` set to an Arm64
  JDK 17. It does the following:
  - builds native ARM64 (`0xAA64`) `gkl_{utils,pairhmm,pairhmm_omp,smithwaterman}.dll`
    with clang and the same flags and no-FMA guard;
  - links the LLVM runtimes statically, but ships `libomp.dll` shared, because
    two static OpenMP runtimes in one JVM abort;
  - checks each DLL's machine type and imports;
  - writes `gkl-native-wgse-windows-aarch64.jar`.

  On this target `common/wgse_libm.h` replaces the UCRT `log10/log10f/pow/log1p`
  with glibc-exact ports, so results match Linux bit for bit
  (`licenses/wgse_libm/NOTICE.txt`).
- Launch: `java -cp gkl-native-wgse-linux-aarch64.jar:gatk-package-4.6.2.0-local.jar org.broadinstitute.hellbender.Main HaplotypeCaller ...`.
  The sidecar classes shadow GATK's bundled GKL classes. With the stock GATK
  jar alone, Arm64 keeps the Java fallback. Compression stays on the JDK
  deflater/inflater. On Windows, use the `windows-aarch64` jar and `;` as the
  classpath separator.

Tests (`tests/`):

- `fp_semantics.cc`: reports control-register flush/denormal/tininess behaviour and
  the fingerprints of the libm results GKL uses (`libm_*`, which must match across
  platforms; on Windows also the raw UCRT `crt_*`).
- `omp_fpcr_state.cc`: on a cold OpenMP pool, runs the production FZ arrangement and
  then asserts in an unguarded region that every pool thread is back to its original
  FPCR. It also checks per-worker FZ and full FPCR restoration. `legacy` mode runs the
  pre-fix caller guard; the Linux build requires it to leak (exit 2).
- `GklVectorProbe.java`: native float/double and Java `LoglessPairHMM` bits over
  GKL test vectors and seeded random batches, plus all 14,196 GKL Smith-Waterman
  test pairs × 4 overhang strategies.
- `kbench.cc` / `kbench.sh GKL_NATIVE_DIR SIMDE_DIR OUT`: kernel-only timing
  and exact-bit hashes over 20,000 seeded PairHMM cases and the GKL SW pairs.
  x86-64 and AArch64 must print the same `hash_*` values.
- `compare_vectors.py`: compares the Arm64 probe output against the x64 output.
- `JavaThreadFtzProbe.java THREADS`: asserts that the FP state of the calling thread
  and of a newly created thread is clean (subnormals survive, FTZ off) after
  `initialize`, compute and `done()`, for float and double. `1` tests `IntelPairHmm`
  and `4` tests `IntelPairHmmOMP`; run each in its own JVM. It exits 1 on Arm64 if
  FP state leaks; on x86 the stock leak is reported only.
- `packaging/windows/recipes/gkl/tests/GklParityProbe.java` accepts a third argument,
  `neon`, so that it can qualify this sidecar against `linux-avx2-golden.tsv`.

See `DESIGN.md` for the design rationale; the prototype results are in
`out/arm64/gkl/prototype-results.md` of the issue #236 work.
