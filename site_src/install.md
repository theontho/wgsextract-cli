---
output: 'install.html'
title: 'Install | WGS Extract CLI'
description: 'Install WGS Extract CLI with the standalone macOS/Linux installer, native Windows installer, or developer Pixi workflow.'
eyebrow: 'Setup guide'
heading: 'Install once, then run repeatable genome workflows.'
lede: 'WGS Extract CLI uses Pixi to manage Python, the app itself, and many external bioinformatics tools. macOS and Linux use the standalone terminal installer; Windows uses `install_windows.bat` from PowerShell with the MSYS2 UCRT64 pacman runtime.'
toc: 'macOS/Linux|quickstart; Running the CLI|running; Windows|windows; Developer Pixi setup|developer; Platforms|platforms; References|references; Download checks|checksums; Configuration|config; Verify|verify'
footer_title: 'WGS Extract CLI'
footer_text: 'Install guide and environment reference.'
footer_link_text: 'Next: CLI guide'
footer_link_href: 'cli.html'
---

::: section id=quickstart
::: split
::: steps
::: step
### Open Terminal first
Open the Terminal app on your machine. On macOS, use **Applications > Utilities > Terminal**. On Linux, open your normal terminal window.
:::

::: step
### Paste the installer command
Copy the one-line installer below, paste it into Terminal, and press Enter. The installer prints each step before it runs it.
:::

::: step
### Use the standalone folder
The installer resolves the latest published GitHub release (not the `main` branch) and installs it into `wgsextract-cli/`. The app lives in `app/`, Pixi files live in `.pixi/`, installer temp files live in `app/tmp/`, and the CLI launcher sits beside `app/` at `wgsextract`. On macOS, the installer checks for the Xcode Command Line Tools first and opens this folder in Finder when it finishes.
:::

::: step
### Bootstrap references
Download the metadata and reference-library structure WGS Extract uses to find FASTA, VCF, ploidy, liftover, and annotation assets.
:::

::: step
### Run a small test
Use `info` and region-limited commands first. Whole-genome jobs can take hours and hundreds of gigabytes of working space.
:::
:::

::: code-panel title=macos-linux.sh subtitle="recommended installer"
```
# 1. Open Terminal.
# 2. Paste this line and press Enter.
# 3. The installer creates ./wgsextract-cli.
curl -fsSL https://raw.githubusercontent.com/theontho/wgsextract-cli/main/install.sh | sh

# Put the launcher on PATH for this shell (see "Running the CLI")
export PATH="$PWD/wgsextract-cli:$PATH"

# Initialize reference library data
wgsextract ref bootstrap
wgsextract ref library --list
wgsextract ref library --install hs38

# Verify the environment (exits 1 if a mandatory tool is missing)
wgsextract deps check

# Uninstall the app if needed. Interactive runs ask whether to remove Pixi too.
./wgsextract-cli/uninstall.sh
```
:::
:::
:::

::: section id=running
::: wrap
::: section-head
## Running the CLI
Commands on this site are written as plain `wgsextract`. Use the form that matches how you installed it.
:::

::: table-wrap
| Install type | How to run | Notes |
| --- | --- | --- |
| Standalone installer (macOS/Linux) | `./wgsextract-cli/wgsextract`, or `wgsextract` after adding `wgsextract-cli/` to `PATH` | Relative paths resolve against your current directory. |
| Windows installer or source checkout | `pixi run wgsextract`, from the project folder | |
| Specialized Pixi environments | `pixi run -e pacbio wgsextract ...` (also `deepvariant`, `vep`), from the app or project folder | See [Pixi environments](cli.html#environments){.inline-link}. |
:::

To keep the standalone launcher on `PATH`, add `export PATH="/path/to/wgsextract-cli:$PATH"` to your shell profile.
:::
:::

::: section id=windows
::: split
::: block
::: section-head
## Windows native installer
Use the Windows installer from a normal Windows PowerShell window when you want native Windows paths and the MSYS2 UCRT64 pacman runtime.
:::

::: grid two style="margin-top: 22px"
::: card
### Run the BAT installer
Open PowerShell on Windows, then move into the WGS Extract CLI folder you cloned or downloaded and run `install_windows.bat`. If Pixi or MSYS2 are missing, the installer bootstraps them first.
:::

::: card
### What it does
`install_windows.bat` installs or validates Pixi and MSYS2, installs the project Pixi environment, prepares the MSYS2 UCRT64 pacman runtime tools, and saves pacman as the default runtime.
:::
:::
:::

::: code-panel title=install_windows.bat subtitle="recommended on Windows"
```
# Open PowerShell in the downloaded wgsextract-cli folder.
# If you have not downloaded the project yet:
git clone https://github.com/theontho/wgsextract-cli.git
cd wgsextract-cli

# Recommended Windows install path: run the BAT installer.
.\install_windows.bat

# Verify the native Windows pacman runtime.
pixi run wgsextract deps pacman check
pixi run wgsextract deps check

# Uninstall the app-local environment and config defaults.
.\uninstall_windows.bat

# Also remove bootstrapper-installed Pixi and MSYS2 when desired.
.\uninstall_windows.bat --remove-prerequisites
```
:::
:::

::: wrap
::: callout
{{ text: **Windows prerequisites:** A normal PowerShell session with internet access is enough for a default install. The installer downloads Pixi and MSYS2 when missing. The default MSYS2 path is `C:\msys64`; if yours is somewhere else, run `.\install_windows.bat --msys2-root D:\tools\msys64`. See the [Windows pacman runtime guide](https://github.com/theontho/wgsextract-cli/blob/main/docs/windows_pacman_runtime.md){.inline-link} for details. }}
:::
:::
:::

::: section id=developer
::: split
::: block
::: section-head
## Developer Pixi setup
Technical users who want a normal source checkout, editable code, Git branches, or direct Pixi environment control should use Pixi directly.
:::

::: grid two style="margin-top: 22px"
::: card
### Best for contributors
Use this path when you plan to edit code, run tests, inspect package metadata, or switch branches frequently.
:::

::: card
### Same toolchain
The standalone installer still uses Pixi underneath. The developer setup just leaves Pixi and the source checkout exposed.
:::
:::
:::

::: code-panel title=developer-pixi.sh subtitle="technical users"
```
# Install Pixi first
curl -fsSL https://pixi.sh/install.sh | bash

# Clone and install from source
git clone https://github.com/theontho/wgsextract-cli.git
cd wgsextract-cli
pixi install

# Run through Pixi
pixi run wgsextract help
pixi run wgsextract deps check
pixi run test
```
:::

::: callout
{{ text: **Contributor tooling** (pytest, ruff, mypy) lives in the `dev` Pixi environment. Tasks such as `pixi run test`, `pixi run lint`, and `pixi run typecheck` select it automatically. }}
:::
:::
:::

::: section id=platforms
::: wrap
::: section-head
## Platform notes
Use the standalone installer on macOS/Linux and `install_windows.bat` on native Windows. For Windows, prefer the native MSYS2 UCRT64 pacman runtime; WSL2 is not recommended for normal use because setup is more invasive and file access can be slower.
:::

::: grid three
::: card
### macOS
Use the standalone installer. It opens the install folder in Finder and creates the `wgsextract` CLI launcher.

{{ tag: osx-arm64 }}
{{ tag: osx-64 }}
:::

::: card
### Linux
Use the standalone installer by default on Linux. Pixi resolves Python and native command-line tools through conda-forge and bioconda.

{{ tag: linux-64 }}
:::

::: card
### Native Windows (recommended)
Use `install_windows.bat` from PowerShell. It bootstraps Pixi and MSYS2 when needed and configures the app to use the MSYS2 UCRT64 pacman runtime by default.

{{ link: Windows pacman runtime|https://github.com/theontho/wgsextract-cli/blob/main/docs/windows_pacman_runtime.md|inline-link }}
:::
:::

::: code-panel title=windows-installer.bat subtitle="native app setup" style="margin-top: 22px"
```
# Open PowerShell, clone or download the project, then:
cd wgsextract-cli
.\install_windows.bat
pixi run wgsextract deps pacman check
```
:::
:::
:::

::: section id=references
::: wrap
::: section-head
## Reference library setup
Most genome operations need a reference genome and companion files. The reference library lets WGS Extract resolve those assets instead of asking you for every path on every command.
:::

::: grid two
::: card
### Bootstrap data
`ref bootstrap` initializes the library scaffolding and common metadata. Then use the library commands to list and install supported builds such as hs38.

```
wgsextract ref bootstrap
wgsextract ref library --list
wgsextract ref library --install hs38
```

Annotation data comes from `ref clinvar`, `ref gnomad`, `ref revel`, `ref phylop`, `ref alphamissense`, `ref pharmgkb`, and `ref gene-map`. SpliceAI scores require an Illumina BaseSpace login, so `ref spliceai` explains where to put a manually downloaded file.
:::

::: card
### Index and verify
FASTA references need indexes and dictionaries before alignment and variant callers can use them efficiently.

```
wgsextract ref index --ref /path/to/hs38.fa
wgsextract ref verify --ref /path/to/hs38.fa
wgsextract ref status
```

`ref verify` checks gzip integrity, the FASTA index, and, for catalog genomes, the sequence-dictionary signature; it fails on a mismatch.
:::
:::

::: callout
{{ text: **Reference builds matter.** hg19/GRCh37, hg38/GRCh38, and T2T coordinates differ. Do not mix BAMs, VCFs, annotation files, and gene coordinates from different builds unless you intentionally lift over or re-run the analysis. }}
:::
:::
:::

::: section id=checksums
::: wrap
::: section-head
## Download verification
Reference bundles and Windows native tool ZIPs are GitHub release assets, versioned separately from app releases so routine releases do not re-upload multi-gigabyte files.
:::

When `ref bootstrap`, `ref library --install`, or `ref download` fetches a GitHub release asset, WGS Extract asks the GitHub Releases API for that asset's `sha256:<digest>` metadata and verifies the file before extracting or processing it. No neighboring `.sha256` files are needed. The Windows installer's `scripts/setup_pacman_runtime.ps1` uses the same check for the BWA, minimap2, samblaster, and fastp ZIPs.

- If the digest lookup is unavailable (for example, the API is unreachable or rate-limited), WGS Extract logs a warning and continues. Set `GITHUB_TOKEN` to authenticate the lookup in automation.
- If GitHub metadata is fetched but has no valid SHA-256 digest, or the downloaded file does not match it, the download fails.
- Reference genomes are checked again after installation: gzip integrity, `samtools faidx`, and the sequence-dictionary signature.
- For local files or non-GitHub URLs on Windows, pass the expected digest explicitly (for example `install_windows.bat --bwa-binary-sha256 <hex>`); see the [Windows pacman runtime guide](https://github.com/theontho/wgsextract-cli/blob/main/docs/windows_pacman_runtime.md){.inline-link}.
:::
:::

::: section id=config
::: split
::: block
## Configuration
{{ lede: Use `wgsextract config` to view the active config path and every setting. Set input, output, reference FASTA, reference library, genome library, threads, memory, and external tool paths once instead of repeating them. Command-line options override config values for a single run. }}

::: grid two style="margin-top: 22px"
::: card
### Config locations
macOS: `~/.config/wgsextract/config.toml` if `~/.config/` exists, otherwise `~/Library/Application Support/wgsextract/`. Linux: `~/.config/wgsextract/config.toml`. Windows: `%LocalAppData%\theontho\wgsextract\config.toml`.
:::

::: card
### Genome library
Set `genome_library` to a folder with one subfolder per person or sample. Then `--genome sample-id` can resolve inputs and outputs from that folder.
:::
:::
:::

::: code-panel title=config.toml subtitle=example
```toml
input = "/data/genomes/joe/joe.cram"
outdir = "/data/genomes/joe/out"
# Reference genome FASTA (same as --ref)
ref = "/data/reference/genomes/hs38.fa.gz"
# Reference library directory
reflib = "/data/reference"
genome_library = "/data/genome-library"
# A number, or "auto"
threads = "auto"
memory = "auto"
haplogrep_path = "/usr/local/bin/haplogrep"
```
:::
:::
:::

::: section id=verify
::: wrap
::: section-head
## Verify before running full genomes
Start with environment checks and tiny regions. It is much cheaper to catch missing references, unsorted BAMs, or command syntax errors on chrM than halfway through a full-genome job.
:::

::: code-panel title=verification-recipes.sh subtitle="safe first commands"
```
wgsextract deps check
wgsextract --input /data/sample.cram info --detailed
wgsextract --input /data/sample.cram extract mito-vcf
wgsextract --input /data/sample.cram vcf snp --region chrM
wgsextract --input /data/sample.cram microarray --formats 23andme_v5 --region chrM
```
:::
:::
:::
