---
output: 'cli.html'
title: 'CLI Guide | WGS Extract CLI'
description: 'WGS Extract CLI command guide with command groups, global options, common recipes, and Pixi environments.'
eyebrow: 'Command reference'
heading: 'A map of the WGS Extract command line.'
lede: 'The CLI is the best interface for repeatable work: scripts, batch jobs, remote machines, AI-agent workflows, and long-running analyses where exact commands matter.'
toc: 'Global options|global; Command groups|groups; Recipes|recipes; Pixi environments|environments; Best practices|patterns'
footer_title: 'WGS Extract CLI'
footer_text: 'Command guide and recipes.'
footer_link_text: 'Next: workflows'
footer_link_href: 'workflows.html'
---

::: section id=global
::: split
::: block
## Global options
{{ lede: Shared options can go before or after the subcommand. `--threads` and `--memory` accept `auto` (the default). Every command exits non-zero on failure, so commands can be chained safely in scripts. Examples write plain `wgsextract`; see [Running the CLI](install.html#running){.inline-link} for the launcher that matches your install. }}
:::

::: code-panel title=global-options.sh subtitle="common flags"
```
wgsextract \
  --input /data/sample.cram \
  --outdir /data/out/sample \
  --ref /refs/hs38.fa \
  --threads auto \
  --memory 2G \
  info --detailed
```
:::
:::
:::

::: section id=groups
::: wrap
::: section-head
## Command groups
Run `wgsextract help` for the live tree from your installed version, `wgsextract --full-help` for every option, or `wgsextract <command> --help` for one command.
:::

::: grid three
::: card
### Info and QC
`info` (build, integrity, read metrics; `--csv` for machine-readable output), `info calculate-coverage` (exact breadth via samtools coverage, written to `<input>_coverage.tsv`), `info coverage-sample` (fast estimate), plus `qc fastqc`, `qc fastp`, `qc vcf`, and `qc fake-data`.
:::

::: card
### BAM / CRAM
`bam identify`, `bam sort`, `bam index`, `bam unindex`, `bam unsort`, `bam to-cram`, `bam to-bam`, `bam unalign`, `bam merge`, `bam to-ubam` (unaligned BAM for GATK), plus `repair ftdna-bam` and `repair ftdna-vcf` for FTDNA files.
:::

::: card
### Extraction
`extract mito-fasta`, `extract mito-vcf`, `extract mt-bam`, `extract ydna-bam`, `extract ydna-vcf`, `extract y-mt-extract`, `extract unmapped`, `extract custom`, and `extract bam-subset`. Mito and Y VCFs are called as haploid.
:::

::: card
### Variant workflows
Calling: `vcf snp`, `vcf indel`, `vcf freebayes`, `vcf gatk`, `vcf deepvariant`, `vcf sv`, `vcf cnv`. Annotation: `vcf annotate`, `vcf chain-annotate`, `vcf clinvar`, `vcf gnomad`, `vcf revel`, `vcf phylop`, `vcf cadd`, `vcf gerp`, `vcf spliceai`, `vcf alphamissense`, `vcf pharmgkb`, `vcf pharmcat`. Also `vcf filter`, `vcf trio`, and `vcf to-fasta` (personal consensus genome).
:::

::: card
### References and annotation
`ref bootstrap`, `ref library`, `ref library-list`, `ref status`, `ref download`, `ref index`, `ref verify`, `ref count-ns`, `ref gene-map`, the annotation downloads (`ref clinvar`, `gnomad`, `revel`, `phylop`, `cadd`, `gerp`, `spliceai`, `alphamissense`, `pharmgkb`), and `vep download`, `vep run`, `vep verify`.
:::

::: card
### Analysis and alignment
`microarray`, `lineage y-haplogroup`, `lineage mt-haplogroup`, `typing cyp2d6`, `typing hla`, `typing str`, `sign create`, `sign verify`, `align`, `realign`, `pet-align`, `analyze comprehensive`, `analyze batch-gen`, `example-genome list/download`, `benchmark`, `deps check`, and `config`.
:::
:::
:::
:::

::: section id=recipes
::: wrap
::: section-head
## Common CLI recipes
These examples are intentionally small and explicit. Replace paths and references with your own. Output names keep the full input stem, so `HG002.chr20.bam` produces `HG002.chr20_sorted.bam`.
:::

::: code-panel title=recipes.sh subtitle="copy, edit, run"
```
# Identify a BAM/CRAM and inspect metadata
wgsextract --input sample.cram info --detailed

# Sort and index an alignment (sample.bam -> out/sample/sample_sorted.bam)
wgsextract --input sample.bam --outdir out/sample bam sort
wgsextract --input out/sample/sample_sorted.bam bam index

# Convert BAM to CRAM for storage
wgsextract --input sample.bam --ref /refs/hs38.fa bam to-cram

# Generate consumer microarray-style outputs
wgsextract --input sample.bam microarray --formats 23andme_v5,ancestry_v2

# Extract mitochondrial variants and Y-DNA reads
wgsextract --input sample.bam extract mito-vcf
wgsextract --input sample.bam extract ydna-bam

# Call and filter variants
wgsextract --input sample.bam --ref /refs/hs38.fa vcf snp
wgsextract vcf filter --vcf-input calls.vcf.gz --expr 'QUAL>30'

# Show the full command tree
wgsextract help
```
:::
:::
:::

::: section id=environments
::: wrap
::: section-head
## Pixi environments
Some specialized tools live in dedicated Pixi environments to keep dependency solving practical and platform-specific. When a tool such as `vep` or `pbsv` is not on `PATH`, WGS Extract runs it through `pixi run -e <environment>` automatically. You can also run a whole command inside an environment, from the installed `app/` folder or a source checkout, as shown below.
:::

::: grid three
::: card
### default
General CLI, samtools/bcftools, bwa/minimap2, fastp/fastqc, freebayes, delly, GATK, Yleaf, Haplogrep, and common workflows.

```
pixi run wgsextract deps check
```
:::

::: card
### pacbio
PacBio-aware structural variant workflows using pbmm2, pbsv, and sniffles where available.

```
pixi run -e pacbio wgsextract vcf sv --pacbio
```
:::

::: card
### vep
Ensembl VEP for `vep run`, `vep download`, and `vep verify`. Not available on native Windows.

```
pixi run -e vep wgsextract vep run
```
:::

::: card
### gatk and freebayes
GATK (Python 3.10, Java 17) and FreeBayes live in their own environments for dependency reasons. `vcf gatk` and `vcf freebayes` use them automatically; the first run downloads the environment.
:::

::: card
### cyrius, t1k, and expansionhunter
Gene and repeat typing: `typing cyp2d6` (Cyrius), `typing hla` (T1K), and `typing str` (ExpansionHunter). Linux and macOS. These aren't downloaded on first use: install them once with `pixi install -e <environment>`; Cyrius is licensed for noncommercial use only.
:::

::: card
### dev
Contributor tooling: pytest, ruff, mypy, pre-commit, actionlint. Development tasks select it automatically.

```
pixi run test
pixi run lint
```
:::
:::
:::
:::

::: section id=patterns
::: wrap
::: section-head
## CLI best practices
Genome jobs are expensive. These habits prevent wasted hours and corrupted assumptions.
:::

::: grid four
::: card
### Use regions first
Try `chrM`, `chrY`, or a small gene region before whole-genome commands.
:::

::: card
### Keep outputs isolated
Use `--outdir` per sample or `--genome` folders to avoid mixing files.
:::

::: card
### Match builds
Keep BAM/CRAM, FASTA, VCF, gene maps, and annotations on the same reference build.
:::

::: card
### Log the command
Save exact commands, Pixi environment, reference build, and tool versions for repeatability.
:::
:::
:::
:::
