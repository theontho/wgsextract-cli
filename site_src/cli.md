---
output: 'cli.html'
title: 'CLI Guide | WGS Extract CLI'
description: 'WGS Extract CLI command guide with command groups, options, and common recipes.'
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
{{ lede: Most commands accept shared options before the subcommand, and many commands also accept them after the subcommand because WGS Extract re-applies explicit shared options across parsers. }}
:::

::: code-panel title=global-options.sh subtitle="common flags"
```
pixi run wgsextract \
  --input sample.cram \
  --outdir out/sample \
  --ref /refs/hs38.fa \
  --threads 8 \
  --memory 16G \
  info --detailed
```
:::
:::
:::

::: section id=groups
::: wrap
::: section-head
## Command groups
Run `pixi run wgsextract help` or `pixi run wgsextract --full-help` for the live tree from your installed version.
:::

::: grid three
::: card
### Info and QC
`info`, `info calculate-coverage`, `qc fastqc`, `qc fastp`, `qc vcf`, and `qc fake-data` help inspect inputs, test pipelines, and quality-check reads or variants.
:::

::: card
### BAM / CRAM
`bam sort`, `bam index`, `bam to-cram`, `bam to-bam`, `bam unalign`, `bam identify`, plus repair helpers for FTDNA files.
:::

::: card
### Extraction
`extract mito-fasta`, `extract mito-vcf`, `extract mt-bam`, `extract ydna-bam`, `extract ydna-vcf`, `extract y-mt-extract`, `extract unmapped`, and `extract custom`.
:::

::: card
### Variant workflows
`vcf snp`, `vcf indel`, `vcf sv`, `vcf cnv`, `vcf freebayes`, `vcf gatk`, `vcf deepvariant`, `vcf filter`, `vcf trio`, and annotation commands.
:::

::: card
### References and annotation
`ref bootstrap`, `ref library`, `ref index`, `ref verify`, `ref gene-map`, annotation downloads, and `vep download/run/verify`.
:::

::: card
### Analysis helpers
`microarray`, `lineage y-haplogroup`, `lineage mt-haplogroup`, `align`, `analyze comprehensive`, `example-genome`, `pet-align`, and `benchmark`.
:::
:::
:::
:::

::: section id=recipes
::: wrap
::: section-head
## Common CLI recipes
These examples are intentionally small and explicit. Replace paths and references with your own.
:::

::: code-panel title=recipes.sh subtitle="copy, edit, run"
```
# Identify a BAM/CRAM and inspect metadata
pixi run wgsextract --input sample.cram info --detailed

# Sort and index an alignment
pixi run wgsextract --input sample.bam --outdir out/sample bam sort
pixi run wgsextract --input out/sample/sample.sorted.bam bam index

# Convert BAM to CRAM for storage
pixi run wgsextract --input sample.bam --ref /refs/hs38.fa bam to-cram

# Generate consumer microarray-style outputs
pixi run wgsextract --input sample.bam microarray --formats 23andme_v5,ancestry_v2

# Extract mitochondrial variants and Y-DNA reads
pixi run wgsextract --input sample.bam extract mito-vcf --region chrM
pixi run wgsextract --input sample.bam extract ydna-bam

# Call and filter variants
pixi run wgsextract --input sample.bam --ref /refs/hs38.fa vcf snp
pixi run wgsextract vcf filter --vcf-input calls.vcf.gz --expr 'QUAL>30'

# Launch the desktop GUI
pixi run wgsextract gui --desktop
```
:::
:::
:::

::: section id=environments
::: wrap
::: section-head
## Pixi environments
Some specialized tools live in dedicated Pixi environments to keep dependency solving practical and platform-specific.
:::

::: grid three
::: card
### default
General CLI, GUI, samtools/bcftools, bwa/minimap2, fastp/fastqc, freebayes, GATK, and common workflows.

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
### deepvariant
DeepVariant workflows, including WGS/WES and PacBio HiFi model options where supported.

```
pixi run -e deepvariant wgsextract vcf deepvariant
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
