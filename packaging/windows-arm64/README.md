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
The separate `python-source` recipe stages 3.13.18 from source with every
CPython external archive pinned by immutable commit and SHA-256, including
the official native ARM64 OpenSSL/libffi binaries. This avoids a global Python
version change. Native build, extension imports, semaphore/pip regression,
installed-file and live-process/module qualification now pass.
MSVC's optimized Unicode-table compilation failed with C1002 even with a native
ARM64 compiler host and serial projects. Qualification uses CPython's ClangCL
project configuration with Release optimization; this is not an official PSF build.
Its strict Windows API argument checking exposed a junction privilege-buffer
length declared as `int`; the source recipe uses the API's actual `DWORD` type.
Windows semaphore counts likewise use `long`, matching the Windows API;
POSIX retains `int`. A real semaphore count/zero-state regression is required.
The source layout also copied an unused x64 `vcruntime140_1.dll`; remove it
before imports and require strict native audits to pass. Archive build
provenance must not inherit the surrounding CLI checkout's Git revision.

Yleaf's setuptools 84.2.1 wheel contains six unused x86/x64 launcher templates.
The staged native-only recipe preserves the two AA64 launchers, all Python
code/notices and consistent RECORD metadata. Native package qualification passes.
Its conda metadata also declares `pkg:pypi/setuptools`: without that standard
identity, Pixi installs an additional stock PyPI wheel over the native package,
reintroducing six foreign templates. `add_setuptools_purl.py` changes only the
information archive; the qualified runtime payload remains byte-identical.
Python-3.13 NumPy/Pandas/psutil wheels are available; all 68 PE files in the
audited pinned wheels are AA64. `python313-wheels.txt` pins their artifacts and
checksums. Installed numerical operations, Yleaf imports/help/marker tables and
live loaded-library audits pass natively with source Python 3.13.18.
The hosted combined prefix audits 156 PE files; a fresh native-only Pixi
candidate installation on Windows 11 ARM64 audits 153 before test tools are added.
These are candidate qualification results, not release/full-genome approval.

## Explicit fresh candidate installation

This is an opt-in qualification path, **not** an enabled release environment.
Use artifacts from one successful `windows_arm64.yml` run, including its
post-processed setuptools package. Check the run's source revision and package
qualification evidence before running an installer; keep its logs and SHA-256
manifest with the results. The staging command rejects incomplete/duplicate
packages and older Python. It never replaces an existing destination.

```powershell
python packaging/windows-arm64/stage_candidate.py `
  --artifact out/native-pipeline.zip --output scratch/native-packages `
  --qualification-run <successful-run-id>
pwsh -NoProfile -File packaging/windows-arm64/install_candidate.ps1 `
  -Packages scratch/native-packages -Source . -Destination scratch/native-install
```

The explicit setup requires already-installed native ARM64 Pixi and PowerShell 7;
it rejects an emulated bootstrap process and checks Pixi's PE header. It pins
the native package artifacts and numerical wheels, creates a fresh Pixi
environment, and audits every installed PE file. Feature execution never
installs missing dependencies.

Fresh Windows images may not yet contain a public website's trusted root:
Python's default Windows-store import cannot itself trigger Windows' automatic
trusted-root download. A fresh Azure guest initially failed UCSC HTTPS with
22 roots. Verified native Schannel HTTPS triggered Windows' normal root update
(23 roots), then unchanged Python HTTPS passed. The installer performs this
verified preflight and requires Python verification too; it neither disables
TLS verification nor downloads/imports arbitrary CA certificates. After this
preflight, the selected 11 native CLI tests and all three aligner truth/thread
gates passed on the fresh guest.

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
This build/qualification helper requires PowerShell 7 and drains both output
pipes concurrently. A real native regression floods each pipe after stdin EOF.
Loaded modules must be AA64 even when their paths are under the Windows directory.

The BWA-MEM2 2.3 recipe uses sse2neon 1.9.1, compiles one NEON
executable and excludes x86 CPUID/dispatch. It pins #268's index64 patch,
header and regression sources at `f95d285bbd4a7b781cb51550b07616c6b56b40d9`
with independent SHA-256 values, without changing the x64 recipes. Native
>4-GiB sparse seeks/chunk/partial-read tests and five Linux tiny-index hashes
pass natively. The 600-record paired-read truth/thread gate matches Linux 2.3
raw SAM body `de8085bffa55e5578e3b39de5e939a55c917e87334103804e01e9ce0dbac1d72`.
The Linux package's embedded recipe confirms the identical pinned source SHA,
not merely the version text. Live executable/process/modules audit AA64.
These small-fixture results are not approval of #268's full-index release gate.
The 2.3 source archive still defaults its version string to 2.2.1; the ARM
recipe explicitly sets the actual pinned source release, 2.3. A matching 2.3
full-genome comparison remains required. The golden's executable reports 2.2.1,
but inspecting `37668c53763709613267b96eb151a245aa9d1443:pixi.lock` and the
embedded Linux package recipe proves it uses the same 2.3 source archive.
The Windows recipe at that commit also pins the identical source SHA.
Do not reject or identify this oracle from the reported version alone.

The staged HTS 1.24 recipe reuses all three unchanged x64 CRAM/BAM optimization
patches, static pinned compression/TRE dependencies and the existing regressions.
It includes the native CRAM rANS NEON translation unit; compiler evidence is
required. Native compilation, packaging, PE audit and CRAM/BAM optimization
regressions pass; the generated whole-pipeline fixture also passes. Remote access
and plugins remain disabled, as in the x64 package.
Native Release builds retain upstream assertions so assertion-based regression
fixtures do not become silent no-ops through CMake's `NDEBUG` default.
Samblaster's staged native recipe reuses the existing POSIX/binary-I/O shim,
adds MSVC's missing `mempcpy`, and supplies a real SAM header before holding
stdin open for its live process/module audit. Native compilation, allocated-format
regression, four-record duplicate-pair fixture and live AA64 audit pass.
Samtools/bcftools 1.24 are staged against the same native HTS library build,
with all existing Windows optimization patches. Their packages install only
their own executables, avoiding duplicate HTS files in the installed prefix.
Source checksums and complete upstream C source lists have been checked.
BCFtools' native package, existing regressions and region-index regression pass;
samtools' native package and regressions now also pass. The generated whole-pipeline
fixture passes; full-genome qualification remains pending.
The ARM64 samtools linker reserves an 8 MiB stack. The default MSVC 1 MiB
reserve caused `0xC00000FD` while indexing a public-genome realigned CRAM:
indexing the identical file failed with the original executable and passed
after changing only its stack reserve. The package implements the correction
at link time; manually edited PE files are qualification experiments, not
distributable artifacts. This setting does not change the x64 Windows recipes.
BCFtools' private 32-bit region-index symbols are namespaced independently of
HTSlib's 64-bit region API; duplicate-symbol suppression would join incompatible
interfaces. The unchanged upstream region-index regression runs with seed 236.
An OpenJDK 17.0.21 runtime recipe is also staged from Microsoft's checksum-pinned
native ARM64 JDK archive. `jlink` retains Java SE, crypto, ZIP and locale modules;
the resulting runtime and a blocked-input Java process pass the AA64 audit.
This is a Java runtime package, not a compiler SDK and not GATK/GKL enablement.

Fastp 1.4.0 is staged with pinned Highway/libdeflate dependencies and ISA-L
2.32.1's generic C baseline. The upstream Windows ISA-L assembly is x86-only;
it is not an acceptable ARM64 implementation. Highway's NEON selection must
be proved by compiler evidence. The LLP64 widening matches the existing x64
recipe. Native compilation, gzip/counter correctness, parser regressions,
NEON include selection and live AA64 audits pass. Performance remains
unqualified; generic C ISA-L is not a claim of optimized ARM compression.
The command-line parser uses MSVC's already-readable `type_info::name()` for
diagnostics rather than depending on GCC's unavailable `cxxabi.h`.
Its private debug flag is renamed from reserved `_DEBUG` to prevent
include-order-dependent MSVC STL iterator-debug ABI mismatches.

Curl 8.21.0 is built from a checksum-pinned upstream archive with static zlib
and Windows Schannel, without an emulated OpenSSL dependency. Qualification
checks HTTP redirects/gzip byte preservation, HTTPS certificate verification,
stdin upload and live native module paths. These gates pass natively.

FastQC 0.13.0's staged restricted recipe compiles upstream sources with native
Java 17 and removes 16 foreign HDF5 JNI payloads from the unsigned JHDF5 jar,
retaining bytecode and license notices. FAST5 fails before starting Java;
FASTQ/BAM/SAM remain eligible for qualification. Both Python and any `--java`
override must have AA64 PE headers; ambient CLASSPATH cannot add unaudited jars.
Java 17 compilation, known-read statistics, archive and live native Windows
runtime audits and package qualification pass. Upstream's source uses a
Java-10+ desktop API despite its Java-8 build setting; this package targets 17.
Haplogrep 2.4.0 hides 13 foreign native objects/libraries inside `jbwa.zip`.
The restricted unsigned jar removes that resource; FASTA/JBWA and FASTA-MSA
requests fail before Java. Original/restricted VCF classification is byte-exact
locally and natively; native Windows version, application-class, live SDK
process/module and restricted-package qualification pass.
Archive qualification recursively examines nested jars and ZIP resources for
foreign PE/ELF/Mach-O payloads, not only entries with DLL suffixes.

The generated pipeline fixture checks actual sort/index/CRAM round-trip,
indexed coverage and a truth-known homozygous variant using the built packages.
Its 40 decoded records and complete VCF body now match the Linux fixture
exactly on native Windows ARM64. Live samtools/bcftools module audits and 11
selected existing small-genome CLI tests also pass with secure packaged Python.
These selected gates are not the complete small/full-genome suites.
The integration job also stages selected existing small-genome CLI tests for
realignment, sort/index/CRAM, calling, info/coverage and microarray export.
It installs and uses the secure source-built Python package, never overlaying
the older NuGet interpreter.

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
