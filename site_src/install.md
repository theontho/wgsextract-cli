---
output: 'install.html'
title: 'Install | WGS Extract CLI'
description: 'Install WGS Extract CLI with the standalone macOS/Linux installer, native Windows installer, or developer Pixi workflow.'
eyebrow: 'Setup guide'
heading: 'Install once, then run repeatable genome workflows.'
lede: 'WGS Extract CLI uses Pixi to manage Python, the app itself, and many external bioinformatics tools. macOS and Linux use the standalone terminal installer; Windows uses `install_windows.bat` from PowerShell with the MSYS2 UCRT64 pacman runtime.'
toc: 'macOS/Linux|quickstart; Windows|windows; Developer Pixi setup|developer; Platforms|platforms; References|references; Configuration|config; Verify|verify'
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
The app lives in `app/`, Pixi files live in `.pixi/`, installer temp files live in `app/tmp/`, and the CLI launcher sits beside `app/` at `wgsextract`. On macOS, Finder opens this folder when installation finishes.
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

# The bootstrap script resolves and installs the latest GitHub release,
# not the latest main branch source.

# Initialize reference library data
./wgsextract-cli/wgsextract ref bootstrap
./wgsextract-cli/wgsextract ref library --list
./wgsextract-cli/wgsextract ref library --install hs38

# Verify the app and environment
./wgsextract-cli/wgsextract info --detailed
./wgsextract-cli/wgsextract deps check

# Launch the desktop GUI on macOS:
# double-click "WGS Extract GUI.command" in the folder Finder opened.
#
# Launch the desktop GUI on Linux:
./wgsextract-cli/start-wgsextract-gui.sh
```
:::
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
pixi run wgsextract --help

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
pixi run wgsextract --help
pixi run wgsextract deps check
pixi run wgsextract gui --desktop
```
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
Use the standalone installer by default. It opens the install folder in Finder and creates a Finder-friendly `WGS Extract GUI.command` desktop GUI launcher.

{{ tag: osx-arm64 }}
{{ tag: osx-64 }}
:::

::: card
### Linux
Use the standalone installer by default on Linux. It creates `start-wgsextract-gui.sh` for the desktop GUI. Pixi resolves Python and native command-line tools through conda-forge and bioconda.

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
./wgsextract-cli/wgsextract ref bootstrap
./wgsextract-cli/wgsextract ref library --list
./wgsextract-cli/wgsextract ref library --install hs38
```

GitHub-hosted reference downloads are checked against GitHub Releases SHA-256 asset metadata before WGS Extract extracts or processes them. Set `GITHUB_TOKEN` for authenticated API lookups. If the digest lookup is unavailable, WGS Extract warns and continues; if GitHub metadata is fetched but lacks valid SHA-256 asset metadata, or if the download does not match the digest, the download fails.
:::

::: card
### Index and verify
FASTA references need indexes and dictionaries before alignment and variant callers can use them efficiently.

```
./wgsextract-cli/wgsextract ref index --ref /path/to/hs38.fa
./wgsextract-cli/wgsextract ref verify --ref /path/to/hs38.fa
```
:::
:::

::: callout
{{ text: **Reference builds matter.** hg19/GRCh37, hg38/GRCh38, and T2T coordinates differ. Do not mix BAMs, VCFs, annotation files, and gene coordinates from different builds unless you intentionally lift over or re-run the analysis. }}
:::
:::
:::

::: section id=config
::: split
::: block
## Configuration
{{ lede: Use `wgsextract config` to view the active config path and defaults. You can set input, output, reference, genome library, thread, memory, and external tool paths once instead of repeating them. }}

::: grid two style="margin-top: 22px"
::: card
### Config locations
macOS: `~/.config/wgsextract/config.toml` or Application Support. Linux: `~/.config/wgsextract/config.toml`. Windows: under AppData.
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
ref = "/data/reference/hs38.fa"
genome_library = "/data/genome-library"
threads = 8
memory = "16G"
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
./wgsextract-cli/wgsextract deps check
./wgsextract-cli/wgsextract --input sample.cram info --detailed
./wgsextract-cli/wgsextract --input sample.bam extract mito-vcf --region chrM
./wgsextract-cli/wgsextract --input sample.bam microarray --formats 23andme_v5 --region chrM
```
:::
:::
:::
