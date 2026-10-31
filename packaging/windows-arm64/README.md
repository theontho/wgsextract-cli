# Native Windows ARM64 qualification

This directory is **not an enabled platform or a release promise**. The existing
`packaging/windows` UCRT64/GCC recipes target x64 and are unchanged. Windows
ARM64 must not use those packages under Prism.

The first qualification recipe repackages the PSF's official `pythonarm64`
NuGet 3.13.17 artifact into the project's existing rattler-build/conda mechanism,
with a fixed URL and SHA-256. It removes four unused x86/x64 pip launchers
(retaining `t64-arm.exe` and `w64-arm.exe`) and the unused x64
`vcruntime140_1.dll`. All remaining executables, extension modules and DLLs must
have PE machine `AA64`; importing the extension modules proves they do not need
the removed DLL. The package must be tested on Windows, not only inspected.
Upstream's latest source release is 3.13.18, whereas its ARM64 NuGet is currently
3.13.17. Qualification is not approval to ship the older security patch level.

The free public `windows-11-arm` GitHub runner is available and has 4 cores and
16 GiB RAM. Its tools are not all native: inspect the actual installed Python,
Pixi, compilers and every application artifact rather than relying on the label.
The `windows-11-arm` image switched to VS 2026, so VS 2022 is not a valid
generator there; the recipe explicitly requires VS 2026 with clang-cl.
Use `scripts/audit_windows_arm64.ps1 -Prefix <environment> -ProcessIds <pids>`
to capture file machine types, process architecture and loaded-module paths.
ARM64EC (`A641`), ARM64X (`A64E`), x64 (`8664`) and x86 (`014C`) application
artifacts cannot pass the native package audit.

The BWA 0.7.19 recipe reuses the unchanged x64 portability and batched
suffix-array patches, but builds with clang-cl's ARM64 target and KSW NEON.
Static zlib, PThreads4W and getopt come from a fixed vcpkg baseline; its port
recipes pin and checksum their sources. Compiler include output must show
`neon_sse.h`; `_M_ARM64` is asserted and ARM64EC/x86 macros rejected.
The native suffix-array equivalence probe is part of the recipe. This build
is not yet a full-genome acceptance or performance result.
The paired-read smoke gate truth-checks 600 alignments, compares one/four
threads and requires the complete ordered SAM body to match Linux BWA
0.7.19-r1273, SHA-256
`efb0d8004175411365b5f740cd833ebcbaf78f39c6aa1fa72a232d5e92b121b6`.
The reference and reads are generated with Python's seeded `random.Random(236)`;
no genome download is needed and genuine `@PG` command headers are preserved.

The minimap2 recipe translates the upstream `arm_neon=1` Makefile selection
into clang-cl/CMake: the three extension kernels use the bundled sse2neon
header, not x86 dispatch. `_M_ARM64` excludes hybrid code generation and
compiler include logs prove the NEON selection. A seeded short-read smoke test
checks mapping truth, native index round-trip and one/four-thread equality.
Its ordered 300-record SAM body must match Linux 2.31-r1302 SHA-256
`f3075dde712eb8219e2ab446f636f51c5d6ce009c5b10189eb09a26cdee05fa3`;
comparison preserves raw newlines rather than hiding Windows text-mode changes.
The binary-I/O constructor is linked directly into the executable, not left in
an unreferenced archive member.
Recipes retain evidence under `out/windows-arm64`, outside rattler's build home
(whose Windows junctions cannot safely be traversed by artifact globbing).
`scripts/qualify_windows_arm64_process.ps1` holds each real aligner on an input
pipe, records its live architecture and modules, then closes input and requires
normal completion. Package file headers alone are not a process audit.

The staged BWA-MEM2 2.3 recipe uses sse2neon 1.9.1, compiles one NEON
executable and excludes x86 CPUID/dispatch. It pins #268's index64 patch,
header and regression sources at `f95d285bbd4a7b781cb51550b07616c6b56b40d9`
with independent SHA-256 values, without changing the x64 recipes. Native
>4-GiB sparse seeks/chunk/partial-read tests and five Linux tiny-index hashes
are required. This staging is not approval of the still-open #268 release gate.
The 2.3 source archive still defaults its version string to 2.2.1; the ARM
recipe explicitly sets the actual pinned source release, 2.3. A matching 2.3
full-genome oracle remains required. The #188 owner confirms that the preserved
2.2.1 golden uses the older scientific baseline, not the new 2.3 source.

The staged HTS 1.24 recipe reuses all three unchanged x64 CRAM/BAM optimization
patches, static pinned compression/TRE dependencies and the existing regressions.
It includes the native CRAM rANS NEON translation unit; compiler evidence is
required. This recipe has not yet passed native qualification. Remote access
and plugins remain disabled, as in the x64 package.
Native Release builds retain upstream assertions so assertion-based regression
fixtures do not become silent no-ops through CMake's `NDEBUG` default.

On an ARM64 Windows OS, configured executable paths and version probes reject
x64 PE files before launching them. Feature entry also rejects a Prism-hosted
Python, including pure-Python info/export operations. Discovery does not spawn
other Pixi environments on ARM64: use the active native environment or direct
native executable paths; cross-environment Pixi wrappers are not qualified.
WSL, Unix compatibility shells and container wrappers are not native tool
substitutes. This guard does not enable an unqualified environment or install
missing software.

Before enabling `win-arm64` in `pixi.toml`:

1. Build BWA 0.7.19 with the existing Windows fixes and KSW `neon_sse.h`,
   BWA-MEM2 2.3 with sse2neon, and minimap2 2.31 with `arm_neon=1`.
   MSVC/clang-cl ARM64 must be used and selected NEON translation units proved
   in compiler/preprocessor output, not inferred from a successful build.
2. Build/pin the native HTS and compression stack, samtools, bcftools, fastp,
   samblaster, and any promised optional tools. The existing Windows HTS recipe
   disables libcurl/S3/GCS/plugins; do not accidentally promise remote HTS I/O.
3. Solve all Python/Yleaf transitive dependencies, preserving x64 and Linux
   defaults and byte-identical pins. The presence of Python alone is insufficient.
4. Publish arch-specific packages and hashes, perform a fresh documented
   installation, then small-genome and full public 30x decoded-CRAM/VCF exactness
   with fixed `-K 100000000` and the matched x64 Windows golden.
5. Benchmark comparable hardware and publish topology, storage, wall time,
   peak working set and performance per dollar. Median ARM/x64 must be ≤1.20.

GATK and DeepVariant fail fast in phase 1. GATK's Java-only kernel fallback,
WSL and x86 containers cannot satisfy qualification. Phase 2 needs native
GKL NEON PairHMM/SW, truthful Java capability dispatch and per-worker ARM FPCR
preservation/restoration. ARM FZ is not x86 MXCSR FTZ/DAZ; test subnormal input,
underflow, worker reuse and Java/native transitions against the golden.
