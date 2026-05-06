---
output: gui.html
title: GUI Guide | WGS Extract CLI
description: Desktop GUI guide for WGS Extract CLI.
eyebrow: Guided interface
heading: The GUI is a command launcher with context.
lede: The desktop GUI gives WGS Extract a tabbed interface for common jobs: selecting genome files, checking dependencies, browsing references, launching extraction and microarray tasks, running VCF workflows, and watching logs.
actions: GUI tabs|#tabs|primary; How it maps to CLI|#cli-map
footer_title: WGS Extract GUI
footer_text: Desktop interface guide.
footer_link_text: CLI guide
footer_link_href: cli.html
---

::: section
::: showcase
::: screenshot
{{ screenshot: gui-screenshot.jpg|Screenshot of the WGS Extract desktop GUI }}
:::

::: block
{{ kicker-p: Launch commands }}
## Use the desktop GUI for normal workflows.
{{ lede: The CustomTkinter desktop GUI is the main graphical interface. The installer creates only a desktop GUI launcher: `WGS Extract GUI.command` on macOS and `start-wgsextract-gui.sh` on Linux. }}

::: code-panel title=launch.sh subtitle="desktop GUI" style="margin-top: 22px"
```
# macOS installer:
# double-click "WGS Extract GUI.command" in Finder.

# Linux installer:
./wgsextract-cli/start-wgsextract-gui.sh

# Developer checkout:
pixi run wgsextract gui --desktop
```
:::
:::
:::
:::

::: section id=tabs
::: wrap
::: section-head
## Desktop GUI tabs
The GUI organizes WGS Extract around the way people think about genome tasks, not around every CLI subparser.
:::

::: grid three
::: card
### Workflow
A high-level visual map from raw reads to aligned files, extracted regions, variants, annotation, and reports.
:::

::: card
### Info / BAM
Identify BAM/CRAM properties, check file stats, calculate coverage, sort, index, convert BAM/CRAM, and repair known FTDNA BAM issues.
:::

::: card
### Extract
Create mtDNA, Y-DNA, combined Y/MT, unmapped, subset, and custom-region files without typing every command.
:::

::: card
### Microarray
Select consumer microarray target formats and generate upload-friendly files from WGS data.
:::

::: card
### Ancestry
Run Yleaf and Haplogrep workflows for paternal and maternal haplogroup analysis when supporting tools and inputs are present.
:::

::: card
### VCF
Run SNP, InDel, SV, CNV, FreeBayes, GATK, DeepVariant, annotation, filtering, trio, VEP, and gene-focused workflows.
:::

::: card
### FASTQ
Quality-check raw reads with FastQC, trim/filter with fastp, and align reads into BAM/CRAM-oriented workflows.
:::

::: card
### Library
Manage reference genomes, gene maps, VEP caches, and supporting resources needed by advanced commands.
:::

::: card
### Settings
Set shared paths for inputs, references, output directories, external tools, and app configuration.
:::
:::
:::
:::

::: section id=cli-map
::: wrap
::: section-head
## How GUI actions map to CLI commands
The GUI is not a separate science pipeline. It launches the same core workflows exposed by the command line.
:::

::: table-wrap
| GUI area | CLI family | Typical use |
| --- | --- | --- |
| Info / BAM | `info`, `bam`, `repair` | Inspect, sort, index, convert, and fix alignment files. |
| Extract | `extract` | Build smaller BAM/VCF/FASTA files for chrM, chrY, unmapped reads, or custom regions. |
| Microarray | `microarray` | Generate 23andMe, AncestryDNA, FamilyTreeDNA, MyHeritage, GEDmatch-style, and research-panel outputs. |
| Ancestry | `lineage` | Run haplogroup tools on Y-DNA or mitochondrial data. |
| VCF | `vcf`, `vep` | Call, annotate, filter, and interpret variant files. |
| FASTQ | `qc`, `align` | Inspect, trim, and align raw sequencing reads. |
:::

::: callout
{{ text: **Tip:** If a GUI command behaves unexpectedly, copy the equivalent CLI command pattern from [the CLI guide](cli.html){.inline-link} and run it in a terminal with `--debug` for clearer logs. }}
:::
:::
:::
