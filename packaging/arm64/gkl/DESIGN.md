# Native Arm64 GKL for GATK 4.6.2 HaplotypeCaller — design (issue #236, phase 2)

Status: prototype built and validated on Azure Cobalt 100 for linux-aarch64 and
win-arm64 (Windows 11 Arm64, native AA64 DLLs). Measurements are summarised in the
PR and recorded in `out/arm64/gkl/prototype-results.md` of the issue #236 work.

## 1. Problem

GATK 4.6.2 accelerates HaplotypeCaller (HC) through Intel GKL 0.8.11 JNI kernels:

- PairHMM: AVX float/double, plus an OpenMP variant;
- Smith-Waterman (SW): AVX2/AVX-512;
- deflate/inflate: ISA-L/libdeflate.

`IntelPairHmm.load()` and `IntelSmithWaterman.load()` return false unless the CPU
reports AVX. On Arm64, GATK therefore falls back to the Java `LoglessPairHMM` and
the Java SW aligner. The goal is native Arm64 HC within 10–20% of x86 on Linux and
Windows, with no emulation and no change to x64 artifacts.

## 2. Research: is there an existing port?

| Candidate | Status | Licence | Reusable? |
|---|---|---|---|
| IntelLabs/GKL 0.8.11 | Upstream; archived. x86 only (AVX/AVX2/AVX-512, ISA-L, libdeflate). | BSD-3 / MIT parts | Yes — the base we patch. |
| syalavarthi/GKL `devel` | Abandoned partial NEON port. Never released or validated; no bit-exactness claim. | MIT (inherits GKL) | Ideas only. |
| fulcrumgenomics **fgkl** (Rust) | Maintained. Fast NEON/AVX kernels. Its docs say results are **not** bit-identical to GKL (FMA, FTZ), and it needs a GATK dependency swap. | MIT | Documented alternative; not chosen, because exactness to x64 golden is a project gate. |
| AWS/Arm "GATK on Graviton" posts | Run GATK with the Java fallback or with Parabricks/Sentieon. No open GKL NEON kernels. | — | No. |
| broadinstitute/gatk issues | Requests for Arm support (Apple Silicon, Graviton); answer is "Java fallback". No Vector-API PairHMM in 4.6.x. | BSD-3 | No. |
| NVIDIA Parabricks | Proprietary GPU/Grace build. | Proprietary | Not used; noted only. |
| sse2neon | SSE→NEON header. No AVX/AVX2 `__m256`. | MIT | Not enough on its own. |
| SIMDe v0.8.2 | Header-only portable AVX/AVX2 → NEON. | MIT | **Used** for the AVX scaffolding (types, setup code). Hot loops are replaced, see §3. |
| Java Vector API | Incubator in JDK 17/21. GATK has no Vector-API PairHMM. | — | No. |

Conclusion: no validated, exact Arm64 GKL exists. We port GKL's own kernels.

## 3. Design

### 3.1 Kernels

1. **Base:** GKL 0.8.11 + the project's Windows patch `0001-windows-avx-native.patch`
   (#207/#220/#227–#229), plus `0002-aarch64-neon-simde.patch`.
2. **SIMDe layer** (`common/simd_compat.h`): includes SIMDe AVX2, maps `_mm_malloc`,
   and maps `_MM_SET_FLUSH_ZERO_MODE` to FPCR.FZ. This alone made the unchanged AVX
   templates compile and run **bit-exactly**. However, they ran 2.5–5× slower than x64,
   because GCC keeps 32-byte generic vectors in memory and clang adds many register
   shuffles. Neoverse N2 has 2×128-bit SIMD pipes, so instruction count matters.
3. **Native NEON PairHMM** (`pairhmm/neon-pairhmm.h`, new, MIT). This rewrites
   `compute_full_prob<float|double>` with 2×`float32x4`/2×`float64x2` lanes (8/4 lanes,
   the same stripe geometry as AVX). Every IEEE operation keeps its order:
   - `M = (((M2·pMM) + (X2·pGAPM)) + (Y2·pGAPM)) · distm_sel`
   - `X = M1·pMX + X1·pXX`
   - `Y = M_y·pMY + Y1·pYY`
   - Lane shifts use `EXT`. The blend is `cmlt + bsl`, as in the AVX sign-bit
     `blendv`. The final sum is taken over the last stripe before the shift.
   - The float→double fallback (`result < MIN_ACCEPTED` → recompute in double) is
     unchanged in `avx_impl.cc`.
   - About 36 SIMD ops per anti-diagonal step.
4. **Native NEON SW** (`smithwaterman/avx2-functions.h`, `WGSE_GKL_NEON` branch).
   The `VEC_*` macros used by the unchanged `PairWiseSW.h` are redefined over
   `struct {int32x4_t lo, hi;}`:
   - `add`, `max`, `cmpgt`, `cmpeq`, `bic`, `bsl`;
   - per-lane `sqxtn/sqxtn2` for `packs_epi32`;
   - half swaps for `permute2f128`.

   All of these are integer operations, so exactness is by construction.
5. **Exactness guards:**
   - `-ffp-contract=off` and `-fno-fast-math`;
   - the build fails if `objdump` finds any `fmla/fmadd/...` instruction. The guard
     (`fma_guard.sh`) fails closed: it writes the whole disassembly to a file first,
     fails if `objdump` fails or prints no functions, and a self-test proves it
     rejects a sample built with `__builtin_fma` before each build trusts it;
   - `-march=armv8-a`, so the build runs on every Arm64 CPU.
6. **FTZ:** GKL sets FTZ (x86 MXCSR) around the kernels and the OMP workers. On Arm we
   set FPCR.FZ only for the duration of each compute call:
   - `ScopedFtzDaz` has an FPCR branch that saves, sets and restores FPCR;
   - `computeLikelihoodsNative` holds one on the calling thread, and each OMP worker
     region holds its own;
   - stock `initialize` sets FTZ persistently on x86. The Arm build skips that, so
     FPCR is unchanged after `initialize`, compute and `done()`, and Java code never
     runs with FZ on;
   - `omp_fpcr_state.cc` verifies each worker's FZ and the full FPCR restore, and
     `JavaThreadFtzProbe` asserts that the JVM's FP state is clean (see risk 2).

   Semantics differ:
   - Arm FZ also flushes denormal *inputs* (DAZ).
   - Arm detects tininess *before* rounding.

   (§5; `fp_semantics.tsv`.) No PairHMM/SW result changed across 20,104 + 56,784
   cases, because the kernels' denormal region is already below the
   `MIN_ACCEPTED` fallback threshold.
7. **Not ported:** AVX-512 (no Arm equivalent; AVX2/AVX widths are what x64 golden
   runs used on the AVX2 baseline) and ISA-L/libdeflate compression (GATK uses the
   JDK deflater when GKL compression is unavailable; the I/O cost is minor for HC).

### 3.2 Java loader overlay (truthful dispatch)

The patch changes five GKL classes, compiled against the GATK jar and shipped in
the sidecar jar ahead of `gatk-package-4.6.2.0-local.jar` on the classpath:

- `IntelGKLUtils`: `isNeonSupported()` (JNI `isNeonSupportedNative`). On aarch64,
  `isAvxSupported()` stays **false** — we never claim AVX.
- `IntelPairHmm` / `IntelPairHmmOMP` / `IntelSmithWaterman`: accept AVX *or*
  (aarch64 and NEON). They log `Using Arm NEON ... (WGSE port of the GKL ... kernel)`.
- `NativeLibraryLoader`: resource directories `native/linux-aarch64/` and
  `native/windows-aarch64/`. The x86 path `native/` is unchanged. On Windows Arm64 it
  first extracts and loads the shipped `libomp.dll` under its exact name, so the
  `gkl_utils`/`gkl_pairhmm_omp` imports resolve to that one copy.

GATK still constructs `PairHMMNativeArguments` and calls the same JNI entry points,
so no GATK code changes are needed. With the stock jar alone, Arm64 behaviour is
unchanged (Java fallback).

### 3.3 Packaging

- **linux-aarch64 (prototype, built):** `build-linux-aarch64.sh WORK GATK_JAR OUT`
  produces `gkl-native-wgse-linux-aarch64.jar` with
  `libgkl_{utils,pairhmm,pairhmm_omp,smithwaterman}.so`. The build:
  - pins and SHA-checks the GKL 0.8.11 and SIMDe v0.8.2 sources;
  - links libstdc++/libgcc statically; only glibc and libgomp are dynamic;
  - uses a fixed `--date` and touch times, so the jar is reproducible;
  - includes licences in `META-INF/licenses/`.

  Production should build this in CI on a GitHub `ubuntu-24.04-arm` runner and
  publish it as a conda package (`gkl-wgse` for `linux-aarch64`), mirroring the
  Windows `gkl-0.8.11-wgse_0.conda` flow. The CLI puts the sidecar first on the
  GATK classpath when `platform.machine()` is arm64.
- **win-arm64 (prototype, built):** `build-windows-arm64.sh WORK GATK_JAR OUT`, run in
  an MSYS2 **CLANGARM64** shell on Windows 11 Arm64, produces
  `gkl-native-wgse-windows-aarch64.jar` with `gkl_{utils,pairhmm,pairhmm_omp,smithwaterman}.dll`
  and `libomp.dll` under `native/windows-aarch64/`.
  - clang, lld and every output are native ARM64 PE (`0xAA64`); the build checks each
    DLL's machine type and import table (system DLLs, plus `libomp.dll` for the two
    OpenMP users). Only the MSYS2 base tools (bash, tar, patch) run x64-emulated, at
    build time. At run time the JVM process is native (`IsWow64Process2`
    `process_machine=0`).
  - Same pinned sources and patches as Linux; same `-march=armv8-a -ffp-contract=off`
    and no-FMA guard (which exempts only `wgse_libm::`, see below).
  - libc++, libunwind, compiler-rt and winpthreads are static. **LLVM OpenMP is
    shared:** `gkl_utils` and `gkl_pairhmm_omp` both load into the JVM, and two static
    libomp copies in one process abort with OMP Error #15.
  - **UCRT libm is not glibc.** GKL's PairHMM uses `log10`/`log10f` for every result
    and `pow`/`log1p` for its jacobian and match-to-match tables. The Windows Arm64 UCRT
    differs from glibc in the last bit, which changed 31 float and 3 double results of
    the 20,104 PairHMM vectors, and 265 of 400k jacobian and 2,811 of 16k `log1p` table
    inputs. (The chr20 VCF happened to be unaffected.) On `_WIN32 && __aarch64__`,
    `common/wgse_libm.h` replaces those four functions with ports of glibc 2.39's AArch64
    implementations (fdlibm `log10`/`log10f`/`log1p`, Arm optimized-routines
    `log`/`logf`/`pow`/`exp` and their tables), with GCC's fused multiply-adds written
    as explicit `fma` calls. They were proven bit-identical to glibc under qemu: every
    positive float for `log10f`, and 100 M random doubles each for `log10`, `pow` and `log1p`.
    Elsewhere the header forwards to the platform libm, so Linux and x64 objects are
    unchanged. `powf` stays on the CRT, because GKL evaluates it only on 128 fixed
    inputs, where UCRT and glibc agree.
  - JDK: the phase-1 native Arm64 runtime (Microsoft Build of OpenJDK 17.0.21 ARM64,
    recipe `packaging/windows-arm64/recipes/openjdk` on `p2-arm64-windows`).
  - Production: build on a GitHub `windows-11-arm` runner and publish as the
    `win-arm64` conda package, mirroring the x64 `gkl-0.8.11-wgse` flow.
- **x64 byte-identity:**
  - x64 never loads the sidecar. All patch hunks are guarded by `__aarch64__` /
    `_M_ARM64` / `WGSE_GKL_NEON`.
  - The Java changes only add an `aarch64 && NEON` branch. The `native/` resource
    path and the existing Windows x64 package/pins are not touched.
  - The kernel benchmark proves the x64 kernel hashes match our Arm build.

## 4. Licensing

- GKL: BSD-3-Clause (IntelLabs).
- SIMDe: MIT.
- New NEON code: MIT (SPDX headers), compatible with both.
- `wgse_libm.h` (win-arm64 only): fdlibm (Sun, "freely distributable with this
  notice") and Arm optimized-routines (MIT OR Apache-2.0 WITH LLVM-exception, used under
  MIT). The tables come from upstream optimized-routines, not glibc's LGPL sources.
  Both notices ship in `META-INF/licenses/wgse_libm/`.
- Windows runtimes: LLVM libc++/libunwind/compiler-rt (static) and `libomp.dll`
  (Apache-2.0 WITH LLVM-exception); mingw-w64 CRT and winpthreads (ZPL/MIT-style).
  Their licence files are copied from the MSYS2 packages.
- Static libstdc++/libgcc fall under the GCC Runtime Library Exception; their copyright
  file is included.
- libgomp is dynamic (GPL + runtime exception).
- No fgkl, Parabricks or syalavarthi code is used.

## 5. Risks and findings

1. **Java Math intrinsics differ between x86 and AArch64.** HotSpot x86-64 uses Intel
   libm stubs for `Math.log10/exp/pow` (`UseLibmIntrinsic`). AArch64 uses
   fdlibm/SharedRuntime. These show up as 51 of 20,104 bit differences in Java
   `LoglessPairHMM` and as **8 of 127,210 chr20 VCF records** differing between
   ARM-NEON and x64-AVX HC.
   - This is *not* a kernel effect. x64 HC with
     `-XX:+UnlockDiagnosticVMOptions -XX:-UseLibmIntrinsic` produces output
     **byte-identical** to ARM on the affected regions and on full chr20 (see
     results).
   - It affects every Arm64 GATK, including the Java fallback.
   - Mitigation: accept and document, or gate x64-vs-Arm parity with
     `-XX:-UseLibmIntrinsic` on the x64 side.
2. **FZ semantics** (DAZ and tininess before rounding on Arm). The kernels are
   unaffected on all probes.
   - Stock GKL leaves FTZ set on the JVM thread after `initialize` on x86. Doing the
     same on Arm leaked further: on AArch64 Linux, threads created afterwards inherit
     FPCR.FZ, so Java code on any new thread silently flushed subnormals.
   - The Arm build therefore scopes FZ to each compute call (item 6 above). The kernels
     see the same FPCR as before, so their results are unchanged; Java code now sees
     IEEE subnormals on every thread. `JavaThreadFtzProbe 1|4` fails the Arm qualification
     if the calling thread or a new thread is not clean after `initialize`, compute or
     `done()`.
   - x86 keeps the stock leak, because x64 must stay byte-identical to upstream.
3. **128-bit SIMD throughput.** Neoverse N2/V2 have 2–4×128-bit pipes, so per-core
   kernel speed is about 1.25–1.3× slower than Zen 3 AVX2 (kbench). Cobalt vCPUs are
   full cores, while x64 vCPUs are SMT threads, so the gap closes at equal vCPU count.
4. **Compiler drift.** A future compiler could introduce contraction or
   reassociation. The fail-closed FMA objdump guard and the kbench/probe hashes catch this.
5. **C runtime libm on Windows.** The UCRT's last-bit differences from glibc changed
   0.15% of PairHMM vector results and the kbench hashes until `wgse_libm.h` replaced
   `log10/log10f/pow/log1p`. The chr20 VCF was identical even before the fix, but the
   kernels would not otherwise be bit-exact. A new libm call in the kernel path would reopen this;
   `fp_semantics.cc` fingerprints and the kbench hashes catch it. `crt_*` lines record
   the UCRT's own fingerprints for comparison.
6. **Upstream churn.** GKL is archived and GATK 4.6.2 pins GKL 0.8.11, so the patch
   target is stable.

## 6. Effort estimate (to production)

| Item | Effort |
|---|---|
| Upstream patch review and hardening, CI job on `ubuntu-24.04-arm`, conda recipe | 2–3 days |
| CLI classpath wiring, capability check, tests | 1 day |
| win-arm64 build (CLANGARM64, libomp, glibc-exact libm): **done in prototype**; remaining work is the `windows-11-arm` CI job and conda recipe | 1–2 days |
| Full-genome and second-sample validation on Arm (the parity gate needs the `UseLibmIntrinsic` caveat) | 1–2 days plus VM time |
| Optional: SVE/SVE2 kernels for V2/Graviton4, a further kernel speed-up | 1–2 weeks; not needed for the target |
