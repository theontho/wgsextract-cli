# Native Linux ARM64 (phase 1)

Use a native 64-bit ARM Linux host, such as Ubuntu 24.04 on Cobalt 100 or
Ampere, and the [documented installer](../README.md#install). Pixi selects the
locked `linux-aarch64` packages automatically; no x86 emulator or container
is needed. Dependencies are installed during setup, never by running a feature.
For a source checkout:

```sh
pixi install --locked
pixi run wgsextract deps check
pixi run wgsextract --help
```

For unreleased code, choose the branch explicitly with the existing
`WGSEXTRACT_REF` installer option. The default installer installs the latest
published release, which may predate ARM64 support.

## Supported phase-1 commands

The default environment includes native BWA, bwa-mem2, minimap2, samtools,
HTSlib, bcftools, fastp, samblaster, Yleaf and Haplogrep. This covers `align`,
`realign`, BAM/CRAM sorting/indexing/conversion, bcftools calling, microarray
export, `info`/coverage and Y/mtDNA lineage commands. BWA uses NEON KSW;
bwa-mem2's native ARM build uses sse2neon (an intrinsics translation library,
not an x86 emulator); minimap2 uses its NEON backend.

VEP is available separately: run `pixi install --locked -e vep` before
annotation, just as on Linux x86_64. The CLI only validates that the environment
already exists. Reference data/cache requirements are unchanged.

Choose `--aligner bwa` or `--aligner bwa-mem2` explicitly for reproducible
comparisons. Both retain the existing fixed batch-size policy, `-K 100000000`;
no ARM-specific scientific defaults are introduced. MEM2's full-human index
construction can require about 90 GB; reuse the verified public index where
available or use a sufficiently large native machine. Alignment needs much
less RAM, and the usual preflight still applies.

The upstream MEM2 v2.3 source archive still reports `2.2.1` from its executable.
Use the locked package version and upstream source checksum as well as
`--version` when matching a scientific golden; the reported version alone is
not evidence that two builds use the same source.

## Deliberately unavailable

GATK commands fail fast on Linux ARM64, including `--gpu`: the pinned GKL
PairHMM and Smith-Waterman kernels require x86 AVX. No slow pure-Java fallback
is silently enabled. DeepVariant also fails before checking input, launching
Docker or running a local binary, including `--gpu` and custom images.
The phase-2 explanation is shown for gated commands, version probes, selected
`deps check --tool` requests and missing optional tools in `deps check`.
Unavailable tools are marked missing, without an unusable installation hint.
Use bcftools for phase-1 variant calling.

Ultima alignment/realignment is outside phase 1 because flow-based duplicate
marking uses GATK. Explicit `--platform ultima` and automatically detected
Ultima read groups fail before alignment/reference/output preparation, even
when GATK is installed. GATK-free Ultima data transformations are unaffected.

GPU alignment/realignment also fails before preparing inputs or inspecting
Docker: the Parabricks image is not qualified natively on ARM64. Use the
native CPU aligners; x86 containers are not an ARM64 fallback.

PharmCAT, Cyrius, T1K, ExpansionHunter and PacBio-specific binaries are not
qualified as part of phase 1. Their existing platform restrictions are not a
promise of ARM64 support. Native GKL and DeepVariant are separate #236 phase-2
work; a Java JIT or an ARM TensorFlow package alone does not satisfy that gate.

CI runs the generated small-genome end-to-end suite on GitHub's native
`ubuntu-24.04-arm` runner. Full 30x exactness and performance acceptance require
native, appropriately sized machines and matched pinned inputs, references,
versions, fixed batch size, CPU/memory limits and local storage. Compare decoded
records, not compressed CRAM bytes or command-line provenance headers.

## Qualification status

Fresh Ubuntu 24.04 ARM installation, native unit tests and generated-genome
tests pass. Full public HG00096 MEM2 output (779,820,884 decoded records) and
bcftools calling (4,408,885 records, including all genotypes) exactly match the
same-source x64 control. Full BWA 30x output also exactly matches the x64
#189 golden: 779,820,884 ordered records and 370,846,991,252 decoded bytes,
including matching portable headers, flagstat and idxstats. Native small-genome
and complete-pair public pilots pass. Stock ARM MEM2 index
construction is 39.1% slower in the measured comparison. This one-time
reference-preparation cost is not an acceptance blocker. A bounded native
50 Mb indexing experiment found only a 1.1% median improvement from
Neoverse-N2 compiler flags, smaller than run variation; all five index
members remained byte-identical. Native profiling identified suffix-array
sorting/FM-index construction rather than NEON alignment or compression as
the hot path. No CPU-specific rebuild or unqualified sorting replacement is
enabled by default.

BWA took 20,980.295 seconds on ARM versus the historical x64 control's
26,380.628 seconds (20.47% faster). Both use 16 vCPU, 128 GiB and local NVMe;
ARM has 16 physical cores, x64 eight cores with SMT. This is explicitly a
historical full x64 comparison, not a fresh same-branch full benchmark.
Full MEM2 is 19.84% slower (within 20%); clean full bcftools calling is
30.61% faster. Regional pricing and core topology affect performance/USD.

See [PR #273](https://github.com/theontho/wgsextract-cli/pull/273) and
[issue #236](https://github.com/theontho/wgsextract-cli/issues/236) for measured
hardware, controls and qualification limits. Support here does not imply
that an untested ARM tool or x86-emulated executable is qualified.
