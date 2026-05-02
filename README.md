# 🧬 WGS Extract CLI (`wgsextract-cli`)

[![GUI Screenshot](docs/gui-screenshot.jpg)]

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

A completely independent, modern, AI-optimized command-line recreation of the [WGS (Whole Genome Sequencing) Extract](https://github.com/WGSExtract/WGSExtract-Dev/) application. 

A goal of this reimplemenation was to make it cli driven first to make it more friendly to use with AIs, and to break the gordian knot of multi-platform end user dependency management by making dependency management a separate decoupled step from the application itself, and make the application partially runnable when dependencies are missing.  Pixi is looking like a potential winner for this 'second step'.

---

## 🚀 Quick Start

The fastest way to get started is using [**uv**](https://github.com/astral-sh/uv).

### 1. Installation
```bash
# Clone and enter the repository
git clone https://github.com/WGS-Extract/wgsextract-cli.git
cd wgsextract-cli

# Install as a global tool (Recommended)
uv tool install .
```

### 2. Basic Usage
```bash
# Run directly if installed as a tool
wgsextract info --detailed

# Or run without installing using uv
uv run wgsextract info --detailed
```

---

## ✨ Key Features

- **🎯 Zero-Config Startup**: Use `.env.local` to set global defaults for your reference library (`WGSE_REF`) and input files (`WGSE_INPUT`).
- **📂 Smart Resource Resolution**: The `ReferenceLibrary` engine automatically locates genomes, ploidy files, and SNP tables.
- **⚡ Performance Optimized**: Native support for `--region` (e.g., `chrM`) allows rapid processing of specific chromosomal areas.
- **🛡️ Robust Testing**: A comprehensive four-tier test suite (130+ tests) ensures reliability and behavioral correctness.
- **🤖 AI-Ready**: Designed with a clean CLI interface that is easy for LLMs and automated scripts to interact with.

---

## 🛠️ Installation & Dependencies

`wgsextract-cli` orchestrates several industry-standard bioinformatics tools.

### Required External Tools
*   **Core**: `samtools`, `bcftools`, `htslib`
*   **Callers**: `delly`, `freebayes`, `ensembl-vep`
*   **Aligners**: `bwa`, `minimap2`
*   **QC**: `fastp`, `fastqc`

### 📦 Dependency Management
We recommend using **Homebrew** (macOS) or **Conda/Pixi** (Linux/WSL2) to manage these tools.

#### macOS (Homebrew)
```bash
# Run the verified installation script
bash dep_scripts/install_macos.sh
```

#### Linux / WSL2 (Conda/Mamba)
```bash
# Recommended for easy Ensembl VEP installation
bash dep_scripts/install_linux_conda.sh
```

---

## ⚙️ Configuration

Copy the example environment file and customize it:
```bash
cp .env.example .env.local
```

**Key variables in `.env.local`:**
- `WGSE_REF`: Path to your reference genome directory.
- `WGSE_INPUT`: Default BAM/CRAM file path.

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

## 📊 Project Stats

Visualize the codebase complexity:
```bash
# Via Pixi (if installed)
pixi run stats

# Or directly
./scripts/project_stats.sh
```

---

## 📄 License

Distributed under the **GPL-3.0 License**. See `LICENSE` for more information.
