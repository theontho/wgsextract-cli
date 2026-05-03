> [!WARNING]
> A bit rough around the edges but I use it locally myself.  An AI agent can probably patch over any rough spots for you.

# 🧬 WGS Extract CLI (`wgsextract-cli`)

![GUI Screenshot](docs/gui-screenshot.jpg)

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

A completely independent, modern, AI-optimized command-line recreation of the [WGS (Whole Genome Sequencing) Extract](https://github.com/WGSExtract/WGSExtract-Dev/) application. 

Designed to be CLI-first for AI-friendliness, `wgsextract-cli` leverages [**Pixi**](https://pixi.sh) to provide a consistent, cross-platform environment for both Python and external bioinformatics tools (like samtools and bcftools).



## ⚙️ Installation & Setup Guide

`wgsextract-cli` uses [**Pixi**](https://pixi.sh) to manage its entire environment, including Python, standard bioinformatics tools (samtools, bcftools, etc.), and the application itself. This ensures a consistent, reproducible setup across all platforms.

### 1. Install Pixi
If you don't have Pixi installed, run:
```bash
# macOS / Linux / WSL2
curl -fsSL https://pixi.sh/install.sh | bash

# Windows (PowerShell)
iwr -useb https://pixi.sh/install.ps1 | iex
```
*Restart your terminal after installation.*

### 2. Clone and Setup
```bash
git clone https://github.com/theontho/wgsextract-cli.git
cd wgsextract-cli

# Install all dependencies and the CLI tool
pixi install
```

### 3. Global Install (Optional)
To make the `wgsextract` command available everywhere on your system without needing to prefix it with `pixi run`:
```bash
# From within the cloned directory
pixi global install --path .
```
*Note: This will add `wgsextract` to your Pixi global binary path.*

### 4. Platform Support
- **macOS (Intel/Apple Silicon)**: Fully supported. Pixi installs all bioinformatics tools automatically.
- **Linux**: Fully supported. Pixi installs all bioinformatics tools automatically.
- **Windows**:
    - **WSL2 (Recommended)**: Follow the Linux instructions within a WSL2 terminal for full support.
    - **Native Windows**: Pixi will manage Python and core utilities, but many bioinformatics tools (like `samtools`) are not natively available via Conda on Windows. For a full experience, use WSL2.

### 5. Initialize Reference Library
Before running extraction tools, you must initialize the reference library (VCFs, liftover chains, metadata).

```bash
# Initialize library in the default 'reference/' folder
pixi run wgsextract ref bootstrap

# List available genomes
pixi run wgsextract ref library --list

# Install a genome (e.g., hs38)
pixi run wgsextract ref library --install hs38
```

### 6. Verification
```bash
# Verify tools and environment
pixi run wgsextract info --detailed
```

---

## ✨ Key Features

- **🎯 Persistent Configuration**: Use a standard `config.toml` in your user directory to set global defaults for your reference library (`ref`) and input files (`input`).
- **📂 Smart Resource Resolution**: The `ReferenceLibrary` engine automatically locates genomes, ploidy files, and SNP tables.
- **⚡ Performance Optimized**: Native support for `--region` (e.g., `chrM`) allows rapid processing of specific chromosomal areas.
- **🛡️ Robust Testing**: A comprehensive four-tier test suite (130+ tests) ensures reliability and behavioral correctness.
- **🤖 AI-Ready**: Designed with a clean CLI interface that is easy for LLMs and automated scripts to interact with.

---



## ⚙️ Configuration

`wgsextract-cli` uses a cross-platform configuration system. Settings are stored in a `config.toml` file in your standard user configuration directory.

### Config Locations:
- **macOS**: `~/.config/wgsextract/config.toml` (Used if `~/.config/` exists) or `~/Library/Application Support/wgsextract/config.toml`
- **Linux**: `~/.config/wgsextract/config.toml`
- **Windows**: `%AppData%\wgsextract\wgsextract\config.toml`

### View Your Config:
Run the following command to see your active configuration path and settings:
```bash
wgsextract config
```

### Example `config.toml`:
```toml
# Default input and output paths
input = "/path/to/my/genome.bam"
outdir = "/path/to/output"

# Reference library location
ref = "/path/to/reference/genomes"

# Per-person/sample genome folders
genome_library = "/path/to/genome-library"

# System resources
threads = 8
memory = "16G"

# External tool paths
yleaf_path = "/usr/local/bin/yleaf"
haplogrep_path = "/usr/local/bin/haplogrep"
```

> [!TIP]
> Use `config.toml` (e.g., `~/.config/wgsextract/config.toml`) to set global paths and resource limits.

### Genome Library
Set `genome_library` to a directory containing one subfolder per person or sample. The subfolder name is the `--genome` ID.

```text
/path/to/genome-library/
  joe/
    genome-config.toml
    joe.cram
    joe.vcf.gz
    raw-fastqs/
      joe_R1.fastq.gz
      joe_R2.fastq.gz
  ken mcdonald/
    bam files/
      sample.bam
```

When `--genome <genome_id>` is supplied, the CLI recursively resolves common inputs from that folder and writes outputs there unless `--outdir` is explicitly provided. A `genome-config.toml` file is created in the genome folder during discovery, even when there is no ambiguity.

If multiple BAM/CRAM files, VCF files, or FASTQ sets are found, the command fails instead of guessing. Edit that genome's `genome-config.toml` to choose the intended files:

```toml
alignment = "bam files/sample.bam"
vcf = "variants/sample.vcf.gz"
fastq_r1 = "raw-fastqs/sample_R1.fastq.gz"
fastq_r2 = "raw-fastqs/sample_R2.fastq.gz"
```

```bash
pixi run wgsextract --genome joe info
pixi run wgsextract --genome "ken mcdonald" microarray --formats 23andme_v5
pixi run wgsextract --genome joe vcf filter --expr 'QUAL>30'
```

---

## 📖 Usage Guide

### Common Commands
```bash
# Identify BAM/CRAM file properties
pixi run wgsextract bam identify

# Calculate mitochondrial coverage
pixi run wgsextract extract mito-vcf --region chrM

# Generate a microarray simulation
pixi run wgsextract microarray --kit 23andme_v5
```

### Available Subcommand Groups
| Category | Commands |
| :--- | :--- |
| **BAM/CRAM** | `sort`, `index`, `to-cram`, `to-bam`, `unalign`, `identify` |
| **Extraction** | `mito-vcf`, `ydna-vcf`, `y-mt-extract`, `bam-subset` |
| **VCF/Variant** | `snp`, `indel`, `annotate`, `filter`, `freebayes`, `vep-run` |
| **Analysis** | `microarray`, `lineage`, `qc`, `pet-align` |
| **System** | `info`, `ref download`, `ref index` |

---

## 🎨 UI Interfaces

While primarily a CLI tool, `wgsextract-cli` includes modern GUI options:

1.  **Desktop GUI**: A classic desktop experience built with `CustomTkinter`.
    ```bash
    pixi run wgsextract gui --desktop
    ```
2.  **Web GUI (NOT Recommended)**: This GUI is incomplete and broken, it's a WIP.
    ```bash
    pixi run wgsextract gui --web
    ```
---

## 🧪 Testing

We maintain high standards for code quality. You can run the test suite using `pixi`:

```bash
# Smoke Tests (Fast, verifies CLI plumbing)
pixi run python tests/test_smoke.py

# Robustness Tests (Ensures stability with bad inputs)
pixi run python tests/test_robustness.py

# End-to-End Tests (Requires real data)
pixi run python tests/test_e2e_fast_chrM.py
```

---

## 🛠️ Development

### Setup Environment
```bash
# Install all dependencies
pixi install
```

### Code Quality
Always run linting and formatting before submitting changes:
```bash
pixi run ruff check --fix .
pixi run ruff format .
pixi run mypy src/wgsextract_cli
```

---

## 📊 Project Code Stats

Visualize the codebase complexity:
```bash
# Via Pixi (if installed)
pixi run stats

# Or directly
./scripts/project_stats.sh
```

Last stats run:
```
========================================================
  WGS Extract CLI: Project Statistics
========================================================

--- Full Project (Excluding generated data and external deps) ---
     185 text files.
     179 unique files.                                          
      11 files ignored.

github.com/AlDanial/cloc v 2.06  T=0.42 s (430.3 files/s, 78153.9 lines/s)
-------------------------------------------------------------------------------
Language                     files          blank        comment           code
-------------------------------------------------------------------------------
Python                          82           3478           1771          20029
Bourne Shell                    87            929            798           4916
Markdown                         3             73              0            196
TOML                             2             24             11            192
PowerShell                       2             10              3             28
YAML                             1              0              0             25
SVG                              1              3              4             17
INI                              1              0              0              5
-------------------------------------------------------------------------------
SUM:                           179           4517           2587          25408
-------------------------------------------------------------------------------

--- Production Code (src/wgsextract_cli) ---
      53 text files.
      53 unique files.                              
       3 files ignored.

github.com/AlDanial/cloc v 2.06  T=0.29 s (184.1 files/s, 70679.4 lines/s)
-------------------------------------------------------------------------------
Language                     files          blank        comment           code
-------------------------------------------------------------------------------
Python                          52           2699           1379          16248
SVG                              1              3              4             17
-------------------------------------------------------------------------------
SUM:                            53           2702           1383          16265
-------------------------------------------------------------------------------

--- Test Code (tests/ and smoke_test_scripts/) ---
      96 text files.
      96 unique files.                              
       1 file ignored.

github.com/AlDanial/cloc v 2.06  T=0.38 s (254.5 files/s, 29160.1 lines/s)
-------------------------------------------------------------------------------
Language                     files          blank        comment           code
-------------------------------------------------------------------------------
Bourne Shell                    69            836            765           4543
Python                          27            762            383           3710
-------------------------------------------------------------------------------
SUM:                            96           1598           1148           8253
-------------------------------------------------------------------------------

========================================================
```

---

## 📄 License

Distributed under the **GPL-3.0 License**. See `LICENSE` for more information.
