# Hardware and acceleration

Alignment (`align`, `realign`) is the slowest step for a whole genome. This
page says which hardware speeds it up and by how much. Each figure is labelled:

- **measured**: timed by this project;
- **published**: from the cited upstream source, on their hardware;
- **estimate**: a model or expectation, not a timing.

For memory and disk planning, see [resource estimates](resource_estimates.md).

## CPU aligners

`--aligner auto` (the default) aligns short reads with bwa-mem2 when all of
these hold, and with bwa otherwise:

- bwa-mem2 is installed (it is in the default environment on Linux, macOS and
  Windows);
- the reference already has a bwa-mem2 index;
- the estimated alignment pipeline fits in the RAM available now.

Auto never downloads or builds the index. When the index is missing but would
fit, the CLI logs a hint; when the index is there but doesn't fit, it logs the
estimated need and uses bwa. `--aligner bwa` or `--aligner bwa-mem2` chooses
explicitly; `--aligner bwa-mem2` fails when it can't run in this computer's
memory rather than falling back.

| | bwa | bwa-mem2 |
| :--- | :--- | :--- |
| Index in memory, human genome | ~5 GB | ~17 GB |
| Building the index | far less | ~28 bytes per base, ~90 GB for GRCh38 |
| Alignment pipeline, 30x at 6 threads with `--memory 256M` (**estimate**, calibrated on a 680-million-read genome) | ~18 GiB | ~29 GiB |
| The same with default `--memory auto` on a 32 GiB computer (1 GiB per thread) | ~24 GiB | ~29 GiB (sort capped at 256 MiB) |
| Time | 1× | ~0.74× (**measured**: 192 s against 259 s, 4.2M read pairs, 12 threads) |

Few computers can build the bwa-mem2 index, so
`wgsextract ref index --aligner bwa-mem2 --ref REF` downloads the published
one (17 GB) for hs38DH and Broad's GRCh38, whose sequences are the same. For
other references it builds the index only when the computer has the memory.
The alignments are identical to bwa's, so the choice affects time and memory
only.

The ~1.3× speedup is what we measured. bwa-mem2's authors **published** up to
3.5× on one thread and 2.4× on one socket of an Intel Xeon Skylake with
AVX-512 ([Vasimuddin et al., IPDPS 2019](https://arxiv.org/abs/1907.12931)).
Bioconda's x86-64 bwa-mem2 (Linux, Intel Macs) picks its SSE4.1, SSE4.2, AVX, AVX2 or AVX-512 kernels at
run time, so newer CPUs gain more.

**ARM (Apple Silicon, Linux ARM64).** The ARM bwa-mem2 builds translate those
x86 SIMD kernels to NEON with [sse2neon](https://github.com/DLTcollab/sse2neon),
so expect a smaller gain than on AVX-512 x86 (**estimate**). On a 16-core Linux
ARM64 server, full 30x bwa-mem2 was 19.8% slower than on the x64 control
(**measured**; see [Linux ARM64](linux-arm64.md#qualification-status)).

There is no fixed memory cutoff, on a Mac or anywhere else: auto compares the
estimate for this input, reference and settings with the RAM available at the
time. As guidance only, a whole human genome needs a computer with 32 GB or
more for bwa-mem2; since the 30x estimate at 6 threads is ~29 GiB, auto picks
bwa whenever less than that is free. Smaller inputs need less.

### Threads and memory

- `--threads` (or `threads` in `config.toml`) defaults to `auto`: all cores, or
  the performance cores on Apple Silicon.
- `--memory` is the sort memory per thread. `auto` uses 25% of RAM (at least
  2 GB) split across the threads, with at least 1 GiB per thread; for example
  16 GiB and 12 threads gives 1 GiB each, 12 GiB in total. With bwa-mem2,
  sorting is capped at 256 MiB per thread to leave room for its index; bwa
  keeps the full `--memory`.
- Before a native BWA run, the CLI prints a memory and disk preflight. More
  threads use more memory; when it reports `LOW`, lower `--threads` or
  `--memory`, or use `--aligner bwa`. Threads are never lowered automatically.

## NVIDIA GPUs (`--gpu`, Parabricks)

`align --gpu` and `realign --gpu` run [NVIDIA Parabricks](https://docs.nvidia.com/clara/parabricks/):
`fq2bam` (BWA-MEM, sorting and duplicate marking) for short reads, and its
minimap2 for PacBio HiFi. The [README](../README.md#nvidia-gpus-parabricks)
lists exactly what each command runs.

Requirements, which the CLI checks but doesn't install:

- Linux on x86-64 (WSL2 should work but is untested; native Windows, macOS and
  Linux ARM64 are refused);
- an NVIDIA GPU with 16 GB or more and its driver;
- Docker with the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/);
- the Parabricks image (`parabricks_image` in `config.toml`).

How much faster:

| Hardware | 30x genome, FASTQ to sorted, duplicate-marked BAM | Source |
| :--- | :--- | :--- |
| RTX 3090 (24 GB) | 40–50 minutes, against 3.5–4 hours for bwa on 12 CPU threads | **measured** |
| NVIDIA DGX system | as little as 6 minutes, against 4–9 hours on a 96-vCPU m5.24xlarge | **published**, [Parabricks fq2bam](https://docs.nvidia.com/clara/parabricks/tool-reference/tools/fq2bam) |

What `--gpu` refuses:

- **ONT**: the GPU path isn't qualified to keep the MM/ML methylation tags;
  align ONT reads without `--gpu`.
- **Ultima**: Parabricks drops the flow tags (`tp`, `t0`) and can't mark
  flow-based duplicates.
- **PacBio CLR/subreads**: there is no GPU preset.
- **`--aligner bwa-mem2`**: Parabricks runs its own BWA-MEM; use `auto` or `bwa`.

## Other GPUs

WGS Extract has no GPU alignment for AMD (ROCm), Intel (oneAPI) or Apple
(Metal) GPUs: we know of no maintained aligner for them whose output matches
BWA-MEM. Integrated GPUs give no meaningful benefit. A Metal-accelerated
aligner for Apple Silicon is being researched in
[#364](https://github.com/theontho/wgsextract-cli/issues/364).

## When a GPU is worth it

- **One or a few genomes:** a CPU is fine. A 30x genome aligns overnight on 8–12
  threads; set up bwa-mem2 first if you have the memory.
- **Many genomes, or quick turnaround:** an NVIDIA GPU with 16 GB+ on Linux
  turns hours into tens of minutes. A rented cloud GPU works too.
- **Variant calling:** `vcf deepvariant --gpu` and `vcf gatk --gpu` run
  Parabricks' DeepVariant and HaplotypeCaller, about a minute for chr20 on the
  RTX 3090 (**measured**). CPU DeepVariant is much slower, especially on Apple
  Silicon, which runs the x86-64 image under emulation. Linux ARM64 has a
  [native CPU build](deepvariant-arm64.md); native Windows has no DeepVariant
  ([Windows](windows.md)).
