# Windows

WGS Extract CLI runs natively on Windows the same way it runs on macOS and
Linux: Pixi installs Python, the CLI and the external tools into the project's
environment, and the CLI runs every tool from there. There is no WSL, MSYS2 or
other Unix layer to set up, and tools read and write your Windows files
directly.

## Installing

Run `install_windows.bat` from PowerShell or a command prompt, in the folder
you downloaded the CLI to (or alone, in an empty folder: it then downloads the
latest release there first). It installs Pixi for your user if it is missing,
installs the Pixi environment and checks the tools with `wgsextract deps check`.

If Pixi was installed for the first time, close and reopen PowerShell or the
command prompt before running `pixi`, so the new window picks up the updated
user PATH.

```bat
.\install_windows.bat
pixi run wgsextract --help
```

`uninstall_windows.bat` removes the environments and clears old runtime
settings from `config.toml`; `--remove-prerequisites` also removes Pixi.
Installers before this one set up MSYS2 (by default at `C:\msys64`) for their
tools. Nothing uses it now; `uninstall_windows.bat --remove-msys2` removes it if
you have no other use for it.

## Where the tools come from

Bioconda publishes no Windows packages, so WGS Extract builds its own:
samtools, bcftools and htslib's bgzip, tabix and htsfile, BWA, bwa-mem2,
minimap2, fastp and samblaster are compiled from their upstream releases with
MinGW-w64 (UCRT), linked statically so each `.exe` needs only Windows' own DLLs,
and packaged as conda packages that `pixi.toml` pins by URL and SHA-256. The
recipes, patches and build instructions are in
[packaging/windows/README.md](../packaging/windows/README.md).

Java tools (GATK, FastQC, HaploGrep) and Yleaf come from conda-forge,
Bioconda's platform-independent packages and PyPI as on the other platforms.
The GATK environment also includes the native Windows GKL sidecar described
below.
`deps check` lists what isn't available on Windows (for example VEP,
DeepVariant, Delly and FreeBayes).

Local paths passed to Java tools use native Windows separators, including
references, inputs, outputs, interval files and temporary directories. You can
still enter paths such as `R:/data/sample.bam`; the CLI converts them before
launching Java so GATK does not mistake `R:` for a URI provider. Explicit URIs
are preserved.

Commands needing pyBigWig (bigWig-backed `vcf phylop`, `vcf gerp` and their
reference downloads), VEP, DeepVariant, Delly, FreeBayes, PharmCAT, Cyrius,
T1K, ExpansionHunter or PacBio tools are not supported on Windows. They fail
before processing inputs or creating output folders, naming the unavailable
tool. PhyloP TSV/VCF resources remain supported. Parabricks `--gpu` modes
require Linux with an NVIDIA GPU.

## GATK on native Windows

Standard-read HaplotypeCaller uses native **OpenMP AVX PairHMM and AVX2
Smith-Waterman** on AVX2-capable x64 Windows CPUs. The URL/SHA-pinned GKL 0.8.11
sidecar supplies four static DLLs alongside unmodified GATK 4.6.2.0 and Java 17.
No WSL, MSYS2 installation, extra OpenMP runtime or manual Java classpath is
needed.

`install_windows.bat` prepares both the default and GATK environments.
For a manual setup, install the locked GATK environment explicitly before
calling; the CLI checks presence and does not install it for you:

```bat
pixi install --locked -e gatk
pixi run wgsextract vcf gatk --input "C:\genomes\sample.bam" --ref "C:\refs\reference.fa" --region chr20 --threads 4 --memory 512M --outdir "C:\results\gatk"
```

The log should include:

```text
Intel GKL Utils loaded
Using the OpenMP multi-threaded AVX-accelerated native PairHMM implementation
Using AVX accelerated SmithWaterman implementation
```

If the sidecar or pinned GATK jar is missing or obsolete, **re-run
`install_windows.bat` or `pixi install --locked -e gatk`**. A missing native-load
message, Java Smith-Waterman, or `LOGLESS_CACHING` fallback means the accelerated
path is not in use. Check CPU support and the preceding GKL load diagnostics;
fallback results can differ numerically from Linux.

The [matched-ISA public HG00096 chr20 validation](https://github.com/theontho/wgsextract-cli/pull/228)
produced **127,210 records byte-identical across all eight calls**, including
every numeric, depth, genotype and FILTER field. With one warmup and three
measured repetitions on matched 4-vCPU/16-GiB AMD EPYC 7763 VMs, medians were
1420.883 s on Windows and 1450.530 s on Linux (**0.97956×**).
This is a specific standard-read/AVX2 result, not a guarantee for every dataset
or CPU: AVX-512 is not enabled in the Windows sidecar, VCF headers and compressed
bytes are not parity targets, and the CLI retains its numerical/performance
warning. Compression and Ultima's flow-likelihood engine still use Java.

The [GATK native Windows smoke CI](../.github/workflows/gatk_windows.yml)
consumes the installed, locked sidecar without rebuilding it or downloading
genome data. It checks native loading, all fields of a tiny synthetic call
against the stock Linux AVX2 golden, and native PairHMM/Smith-Waterman unit
parity. Java fallback or changed VCF fields fail the gate; this does not replace
the full-chr20 release gate.

The CLI launches Java with the sidecar/GATK classpath. Launcher `--java-options`
remain JVM options, including scatter workers' heap and CPU limits; Linux/macOS
launching and default caller arguments are unchanged.

## Cancelling commands

Windows cleanup terminates the registered launcher and its descendants together,
including Java workers. It does not use Ctrl+Break, which Java can ignore.
Keyboard Ctrl+C delivery may still be delayed while a native tool is running;
process-tree termination happens when the CLI's cleanup handler runs.

## Short read alignment

The default `--aligner auto` uses native bwa-mem2 when the reference already
has its index and the index fits in memory. Otherwise it uses BWA. To prepare
the bwa-mem2 index for your reference, run:

```bat
pixi run wgsextract ref index --aligner bwa-mem2 --ref C:\path\to\reference.fa
```

**Temporary native indexing restriction (#264):** the current Windows bwa-mem2
build truncates large reference lengths. The CLI refuses native index
construction for references with **2,147,483,648 bases or more**, before
starting bwa-mem2; use BWA or a complete, verified Linux-built bwa-mem2 index.
Existing indexes and the CLI's verified published hs38DH index download remain
supported. `ref index` prepares the FASTA length metadata automatically.

The human bwa-mem2 index occupies about 17 GB of RAM, with additional memory
needed for reads, sorting and Windows. The CLI checks the reference's memory
estimate before choosing it. `--aligner bwa` selects the lower-memory option;
`--aligner bwa-mem2` selects bwa-mem2 explicitly.

## Upgrading from a WSL or MSYS2 setup

Older Windows installs used a `tool_runtime` setting (`windows`, `pacman`,
`wsl`, `cygwin` or `msys2`) and `pacman_ucrt64_bin`. The CLI now ignores these
settings, and drops them from `config.toml` the next time it saves it. Rerun
`install_windows.bat` to get the native tools.
