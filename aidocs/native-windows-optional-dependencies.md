# Native Windows optional dependency plan

## Goal

Improve native Windows optional dependency coverage without making WSL the
normal path. WSL remains useful for tools that are effectively Linux-only, but
large WGS inputs stored on the Windows filesystem pay a major `/mnt/c` I/O
penalty, so the preferred order is:

1. MSYS2 UCRT64 pacman packages.
2. Native Windows Pixi packages that solve and run without WSL.
3. WGS Extract release assets built for MSYS2 UCRT64, like the existing BWA
   ZIP.
4. WSL or container-only fallback, deferred until native options are exhausted.

## Current optional tool inventory

`wgsextract_cli.core.dependencies.OPTIONAL_TOOLS` currently checks:

| Tool | Native Windows source | Status / notes |
| --- | --- | --- |
| `curl` | MSYS2 pacman or Pixi | Available. Prefer pacman in the Windows runtime so the reported source is consistent with other UCRT64 tools. |
| `htsfile` | MSYS2 pacman via `mingw-w64-ucrt-x86_64-htslib` | Available. This package is already installed for mandatory `tabix`/`bgzip`. |
| `java` | Pixi `openjdk` | Available and already part of the default Pixi environment through the GATK feature. |
| `fastqc` | Pixi `fastqc` | Bioconda noarch package solves on `win-64`; needs Java. |
| `gatk` | Pixi `gatk4` | Bioconda noarch package solves on `win-64`; large but native enough for Windows. |
| `haplogrep` | Pixi `haplogrep` | Bioconda noarch package solves on `win-64`; Java-oriented. |
| `yleaf` | Pixi `yleaf` | Bioconda noarch Python package solves on `win-64`; smoke-test before treating as supported. |
| `pbsv` | Pixi `pbsv`, experimental | Noarch package solves on `win-64`, but it is old and may wrap non-Windows binaries; validate before enabling by default. |
| `minimap2` | Build/package native | Implemented in this branch as an MSYS2 UCRT64 build/install path and release asset workflow output. |
| `samblaster` | Build/package native | Not available from MSYS2 UCRT64 or native Pixi. Good candidate for a release asset. |
| `fastp` | Build/package native | Not available from MSYS2 UCRT64 or native Pixi. Good candidate for a release asset. |
| `freebayes` | Build/package native | Not available from MSYS2 UCRT64 or native Pixi. More complex dependency stack. |
| `delly` | Build/package native | Not available from MSYS2 UCRT64 or native Pixi. More complex dependency stack. |
| `sambamba` | Build/package native or defer | D/LDC-based; likely harder than C/C++ tools. |
| `pbmm2` | Build/package native or defer | PacBio C++ stack; likely harder than first native asset candidates. |
| `sniffles` | Defer / investigate | Native Pixi solve currently fails despite noarch metadata, likely dependency constraints. |
| `vep` | Defer / WSL later | Perl/cache stack is high-friction on native Windows. |
| `run_deepvariant` | Defer / WSL or container later | DeepVariant remains Linux/container-oriented. |
| `dv_call_variants.py` | Defer / WSL or container later | Same DeepVariant constraint. |

## Phase 1: pacman-native setup

The first implementation phase should make the Windows setup/install path
explicitly install and report every optional dependency that MSYS2 UCRT64 can
provide directly.

Current MSYS2 UCRT64 coverage:

- `curl` from `mingw-w64-ucrt-x86_64-curl`
- `htsfile` from `mingw-w64-ucrt-x86_64-htslib`

`htslib` is already installed because mandatory tools need `tabix`, `bgzip`,
and `htsfile`. The useful change is to make optional pacman packages explicit
in `scripts/setup_pacman_runtime.ps1`, so setup output tells users which
optional tools are intentionally covered by pacman.

## Phase 2: native release assets

After pacman coverage is explicit and tested, add release-asset builds for
missing native tools. Follow the BWA pattern:

- build in GitHub Actions on `windows-latest` with MSYS2 UCRT64;
- package only the runtime executable and required adjacent files;
- upload ZIP assets to WGS Extract CLI releases;
- have `scripts/setup_pacman_runtime.ps1` download, verify GitHub release asset
  SHA-256 metadata, and install into the UCRT64 bin tree;
- keep `-Skip...Download`, `-Skip...Build`, and `-Force...Build` style switches
  for installer/debug control.

Recommended order:

1. `minimap2` - implemented first because it is a high-value aligner and
   builds cleanly under MSYS2 UCRT64.
2. `samblaster`
3. `fastp`
4. `freebayes`
5. `delly`
6. `sambamba`
7. `pbmm2`

## Runtime mode implication

The Windows installer currently persists `tool_runtime = "pacman"`. That strict
mode is correct for mandatory pacman validation, but it prevents host Pixi
optional tools such as Java, FastQC, GATK, HaploGrep, and Yleaf from being
discovered during a general `deps check`.

Native Windows needs a no-WSL hybrid runtime mode that searches:

1. normal host `PATH`;
2. MSYS2 UCRT64 pacman paths;
3. host Pixi environments;

and never falls back to WSL. This should be implemented before promising Pixi
optional coverage in the GUI setup experience.
