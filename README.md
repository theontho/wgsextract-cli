> [!WARNING]
> A bit rough around the edges but I use it locally myself.  An AI agent can probably patch over any rough spots for you.

# 🧬 WGS Extract CLI (`wgsextract-cli`)

![GUI Screenshot](docs/gui-screenshot.jpg)

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

A completely independent, modern, AI-optimized command-line recreation of the [WGS (Whole Genome Sequencing) Extract](https://github.com/WGSExtract/WGSExtract-Dev/) application. 

A goal of this reimplemenation was to make it cli driven first to make it more friendly to use with AIs, and to break the gordian knot of multi-platform end user dependency management by making dependency management a separate decoupled step from the application itself, and make the application partially runnable when dependencies are missing.  Pixi is looking like a potential winner for this 'second step'.

---

## ⚙️ Installation & Setup Guide

Getting `wgsextract-cli` running involves three main steps: installing the tool, ensuring external bioinformatics dependencies are present, and initializing your local reference library.

### 1. Install the CLI
The recommended way to install is using [**uv**](https://github.com/astral-sh/uv).

```bash
# Clone and enter the repository
git clone https://github.com/theontho/wgsextract-cli.git
cd wgsextract-cli

# Install as a tool (recommended)
uv tool install .
```

### 2. Install External Dependencies
`wgsextract-cli` relies on standard bioinformatics tools. We recommend using [**Pixi**](https://pixi.sh) to manage these automatically in a portable environment.

#### Automated (Recommended)
If you have [**Pixi**](https://pixi.sh) installed, it will automatically manage all dependencies.

- **macOS / Linux**: Full support. Pixi installs all bioinformatics tools automatically.
- **Windows**: Use **WSL2** (recommended) for full support. On native Windows, Pixi will manage Python and core utilities, but some bioinformatics tools (like `samtools`) must be installed manually or run via WSL2.

#### Windows (Recommended: WSL2)
To use the full bioinformatics suite on Windows, we recommend using WSL2. You can use our bootstrap script to set it up:

```powershell
# Run the WSL2 bootstrap script
powershell ./bootstrap_wsl.ps1
```

#### Verification
```bash
# Verify tools via Pixi (Mac/Linux/WSL2)
pixi run wgsextract info --detailed
```

### 3. Initialize Reference Library
Before running extraction tools, you must initialize the reference library (VCFs, liftover chains, metadata).

```bash
# Initialize library in the default 'reference/' folder
wgsextract ref bootstrap

# List available genomes
wgsextract ref library --list

# Install a genome (e.g., hs38)
wgsextract ref library --install hs38
```

### 4. Basic Usage Verification
```bash
# Run info to verify setup
wgsextract info --detailed

# If not installed globally, you can run via uv:
uv run wgsextract info --detailed
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

# System resources
threads = 8
memory = "16G"

# External tool paths
yleaf_path = "/usr/local/bin/yleaf"
haplogrep_path = "/usr/local/bin/haplogrep"
```

> [!TIP]
> Use `config.toml` (e.g., `~/.config/wgsextract/config.toml`) to set global paths and resource limits.

---

## 📖 Usage Guide

### Common Commands
```bash
# Identify BAM/CRAM file properties
uv run wgsextract bam identify

# Calculate mitochondrial coverage
uv run wgsextract extract mito-vcf --region chrM

# Generate a microarray simulation
uv run wgsextract microarray --kit 23andme_v5
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

1.  **Web GUI (Recommended)**: A modern, reactive interface built with `NiceGUI`.
    ```bash
    uv run wgsextract gui --web
    ```
2.  **Desktop GUI**: A classic desktop experience built with `CustomTkinter`.
    ```bash
    uv run wgsextract gui --desktop
    ```

---

## 🧪 Testing

We maintain high standards for code quality. You can run the test suite using `uv`:

```bash
# Smoke Tests (Fast, verifies CLI plumbing)
uv run python tests/test_smoke.py

# Robustness Tests (Ensures stability with bad inputs)
uv run python tests/test_robustness.py

# End-to-End Tests (Requires real data)
uv run python tests/test_e2e_fast_chrM.py
```

---

## 🛠️ Development

### Setup Environment
```bash
# Install development dependencies
uv sync --group dev
```

### Code Quality
Always run linting and formatting before submitting changes:
```bash
uv run ruff check --fix .
uv run ruff format .
uv run mypy src/wgsextract_cli
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
