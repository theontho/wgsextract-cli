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
`deps check` lists what isn't available on Windows (for example VEP,
DeepVariant, Delly and FreeBayes).

Commands needing pyBigWig (bigWig-backed `vcf phylop`, `vcf gerp` and their
reference downloads), VEP, DeepVariant, Delly, FreeBayes, PharmCAT, Cyrius,
T1K, ExpansionHunter or PacBio tools are not supported on Windows. They fail
before processing inputs or creating output folders, naming the unavailable
tool. PhyloP TSV/VCF resources remain supported. Parabricks `--gpu` modes
require Linux with an NVIDIA GPU.

## Short read alignment

The default `--aligner auto` uses native bwa-mem2 when the reference already
has its index and the index fits in memory. Otherwise it uses BWA. To prepare
the bwa-mem2 index for your reference, run:

```bat
pixi run wgsextract ref index --aligner bwa-mem2 --ref C:\path\to\reference.fa
```

The human bwa-mem2 index occupies about 17 GB of RAM, with additional memory
needed for reads, sorting and Windows. The CLI checks the reference's memory
estimate before choosing it. `--aligner bwa` selects the lower-memory option;
`--aligner bwa-mem2` selects bwa-mem2 explicitly.

## Upgrading from a WSL or MSYS2 setup

Older Windows installs used a `tool_runtime` setting (`windows`, `pacman`,
`wsl`, `cygwin` or `msys2`) and `pacman_ucrt64_bin`. The CLI now ignores these
settings, and drops them from `config.toml` the next time it saves it. Rerun
`install_windows.bat` to get the native tools.
