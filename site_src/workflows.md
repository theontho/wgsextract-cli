---
output: 'workflows.html'
title: 'Workflows | WGS Extract CLI'
description: 'Common WGS Extract workflows for ancestry, SNP chip simulation, variant calling, extraction, references, and storage.'
eyebrow: 'Goal-oriented recipes'
heading: 'Start with what you want to produce.'
lede: 'Whole genome workflows are easier when you choose the output first: an uploadable microarray / SNP chip file, a smaller Y/MT extract, a VCF, a storage-friendly CRAM, or a full FASTQ-to-BAM alignment. Examples write plain `wgsextract`; see [Running the CLI](install.html#running) for your launcher.'
toc: 'Microarray / SNP chip|microarray; Y/MT|ymt; Variants|variants; FASTQ|fastq; Storage|storage; Testing|testing'
footer_title: 'WGS Extract Workflows'
footer_text: 'Goal-oriented genome recipes.'
footer_link_text: 'Next: WGS guide'
footer_link_href: 'wgs-guide.html'
---

::: section id=microarray
::: split
::: block
{{ kicker-p: Ancestry uploads }}
## Simulate microarray / SNP chip files
{{ lede: Use this when you have WGS data but need a raw-data style file for services expecting 23andMe, AncestryDNA, FTDNA, MyHeritage, GEDmatch-style combined kits, or research panels. These files are also commonly called SNP chip files because they represent selected single-marker sites. }}

A microarray or SNP chip file is not a complete genome. It is a compact table of markers that a downstream service knows how to read. WGS Extract builds these files by looking up target positions in your aligned WGS data and writing the requested vendor-style format.

Start with one target format and a small region if you are checking plumbing. Use `--formats all` only when you are confident that the input, reference build, and output directory are correct. If a target site has too little coverage or ambiguous data, the simulated result may be missing or no-called depending on the format and command behavior.

Microarray formats use hg19 coordinates. hg38 input is lifted over with the chain file from `ref bootstrap`, and the command fails up front if that chain is missing; T2T input is not supported. Haploid calls (for example on chrY and chrM) are written as doubled letters such as `AA`, matching vendor files. `--parallel` calls variants per chromosome.
:::

::: code-panel title=microarray-snp-chip.sh subtitle="common formats"
```
wgsextract --input sample.bam microarray --formats all
wgsextract --input sample.bam microarray --formats 23andme_v5
wgsextract --input sample.bam microarray --formats 23andme_v5,ancestry_v2,ftdna_v3
```
:::
:::
:::

::: section id=ymt
::: wrap
::: section-head
## Extract mitochondrial and Y-DNA data
These outputs are much smaller than a whole genome and are useful for haplogroup tools, genealogy services, and quick validation.
:::

Mitochondrial DNA and Y-DNA workflows are common first checks because they are smaller than autosomal whole-genome jobs. They can confirm that the file is readable, that chromosome naming matches expectations, and that the output directory is configured correctly.

Chromosome naming matters here. Some files use `chrM` and `chrY`; others use `MT` and `Y`. If a command cannot find a region, inspect the alignment header or run an identify/info command before assuming the data is absent.

::: grid three
::: card
### mtDNA FASTA
Consensus sequence for mitochondrial analysis.

A mitochondrial FASTA is useful when a downstream tool wants a sequence rather than a variant table. It is a compact output, but the consensus still depends on coverage, read quality, and how heteroplasmy or ambiguous positions are handled.

```
wgsextract --input sample.bam extract mito-fasta
```
:::

::: card
### mtDNA VCF
Mitochondrial variants for Haplogrep-style workflows.

A mitochondrial VCF records differences from the reference. The command finds the mitochondrial contig itself and calls it as haploid.

```
wgsextract --input sample.bam extract mito-vcf
```
:::

::: card
### Y-DNA BAM / VCF
Paternal-lineage inputs for Y-focused tools and services.

Y-DNA extraction can produce smaller alignment or variant files for lineage tools; Y VCFs are called as haploid. Coverage can vary widely by sample, sex chromosome composition, sequencing method, and aligner behavior, so inspect outputs before treating a missing marker as meaningful.

```
wgsextract --input sample.bam extract ydna-bam
wgsextract --input sample.bam extract ydna-vcf
wgsextract --input sample.bam lineage y-haplogroup
```

Yleaf results go to `<outdir>/yleaf/`. Use `lineage mt-haplogroup` for the maternal haplogroup with Haplogrep.
:::
:::
:::
:::

::: section id=variants
::: wrap
::: section-head
## Call, annotate, and filter variants
VCF workflows produce the file type most downstream variant tools understand. Choose callers based on variant type, sequencing technology, runtime, and accuracy needs.
:::

Variant calling is where small test regions are especially important. A command can be syntactically correct but still fail because the BAM is unsorted, the CRAM cannot find its reference, the contig names do not match, or the caller needs more memory than expected. A fast mitochondrial or small-gene test catches those problems before a whole-genome run consumes hours.

Annotation is a separate step from calling. A caller says what appears to differ from the reference. Annotation adds context such as gene names, transcript consequences, population frequencies, or clinical database matches. Filtering then narrows the result to records that match a quality threshold, region, gene, consequence, or expression.

::: code-panel title=variants.sh subtitle="small first, then full genome"
```
# Fast region test
wgsextract --input sample.bam --ref /refs/hs38.fa vcf snp --region chrM

# Whole-genome small variants
wgsextract --input sample.bam --ref /refs/hs38.fa vcf snp
wgsextract --input sample.bam --ref /refs/hs38.fa vcf indel

# Structural variants (Delly; --pacbio uses pbsv or Sniffles)
wgsextract --input sample.bam --ref /refs/hs38.fa vcf sv

# Annotation and filtering
wgsextract vep download --assembly GRCh38
wgsextract --input calls.vcf.gz vep run
wgsextract vcf chain-annotate --vcf-input calls.vcf.gz --annotations clinvar,gnomad,revel
wgsextract ref gene-map
wgsextract vcf filter --vcf-input calls.vcf.gz --gene BRCA1
wgsextract vcf filter --vcf-input calls.vcf.gz --expr 'QUAL>30'
```
:::

::: grid three
::: card
### SNP and InDel calls
SNP and InDel workflows target small sequence changes. They are often the first full-genome variant calls people run, but they still need a matching reference, sorted/indexed alignments, and enough coverage.
:::

::: card
### Structural and copy-number calls
SV and CNV workflows look for larger changes. These can be sensitive to sequencing technology, read length, depth, and caller assumptions, so compare outputs cautiously. `vcf cnv` needs the Delly mappability maps from `ref bootstrap --install-mappability-maps` and takes an integer `--ploidy` (default 2). For PacBio data, `vcf sv --pacbio` uses pbsv or Sniffles, and combining `--pacbio` with `--caller delly` is an error.
:::

::: card
### Annotation and filtering
Annotation tools such as VEP add biological context. Download annotation data with the `ref` commands first; SpliceAI scores need a manual download because they sit behind an Illumina BaseSpace login. Gene filters need gene maps from `ref gene-map` (hg19/hg38 only). Filtering is useful for exploration, but a filtered list is not a diagnosis and should not be used as medical advice.
:::
:::
:::
:::

::: section id=fastq
::: split
::: block
{{ kicker-p: Raw reads }}
## From FASTQ to an aligned file
{{ lede: FASTQ files are raw reads. Quality-check and trim them before alignment, then align to the reference build you intend to use for downstream VCF, extraction, and SNP chip simulation work. }}

Raw reads are closest to the sequencer output. They preserve information before alignment choices, but they are large and require more processing. A typical workflow checks read quality, optionally trims adapters or low-quality ends, aligns reads to a reference, then sorts and indexes the resulting alignment.

If you plan to compare outputs with third-party services, choose the reference build intentionally. Aligning to hg38 and then trying to use hg19-specific annotations or SNP chip positions can produce confusing results unless you lift over or regenerate the appropriate files.

An explicit `--aligner` is always used as given, and combinations that make no sense (for example `--aligner bwa` with a PacBio platform) are rejected rather than silently changed. minimap2 accepts the PacBio preset names `ccs` and `subread`, mapped to `map-hifi` and `map-pb`. `realign` checks free disk space before starting and reuses previously extracted FASTQs when they exist.
:::

::: code-panel title=fastq-to-bam.sh subtitle="raw data path"
```
wgsextract --input sample_R1.fastq.gz qc fastqc
wgsextract qc fastp --r1 sample_R1.fastq.gz --r2 sample_R2.fastq.gz
wgsextract align --r1 sample_R1_fp_1.fastq.gz --r2 sample_R1_fp_2.fastq.gz --ref /refs/hs38.fa

# Long reads: pick the platform; the aligner follows (pbmm2 or minimap2)
wgsextract align --r1 sample.hifi.fastq.gz --platform hifi --ref /refs/hs38.fa

# Re-align an existing BAM/CRAM to a different reference
wgsextract --input sample.hg19.bam --ref /refs/hs38.fa realign
```
:::
:::
:::

::: section id=storage
::: wrap
::: section-head
## Clean up and store alignment files
Coordinate-sorted, indexed CRAM is usually a better long-term storage format than BAM when you have the matching reference genome.
:::

Storage workflows are not just housekeeping. Sorting makes records usable by coordinate-based tools. Indexing enables fast region access. CRAM conversion can save substantial disk space, but it makes the matching reference FASTA part of the file's long-term readability story.

Before deleting a larger original file, make sure the converted file opens, indexes, and reports expected metadata. Preserve checksums and notes about the reference build, because future failures often come from not knowing which reference a compressed alignment expects.

::: grid three
::: card
### Sort
```
wgsextract --input sample.bam bam sort
```
Required by most variant callers and random-access tools. Writes `sample_sorted.bam` (or `_sorted.cram` for CRAM input). Coordinate sorting is different from name sorting; use the one expected by your downstream command.
:::

::: card
### Index
```
wgsextract --input sample_sorted.bam bam index
```
Creates `.bai` or `.crai` random-access indexes. Keep indexes beside the primary alignment file so tools can find them automatically.
:::

::: card
### Compress to CRAM
```
wgsextract --input sample_sorted.bam --ref /refs/hs38.fa bam to-cram
```
Smaller storage if the reference is preserved. Record the reference path, build, and checksum when possible.
:::
:::
:::
:::

::: section id=testing
::: split
::: block
{{ kicker-p: Safe experimentation }}
## Generate fake data before touching real genomes
{{ lede: Fake data is useful for learning command shapes, testing pipeline plumbing, benchmarking file IO, and creating reproducible bug reports without exposing real genetic data. }}

Fake data lets you practice workflows without risking privacy. It is especially useful for checking output directories, command syntax, reference resolution, logging, and cleanup behavior. It will not reproduce every biological edge case, but it can prove that the pipeline wiring works before you use real data.

Use a small fake dataset first, then scale up only when the command does what you expect. Full-size test data can still consume significant disk and time, so treat it like a real run from an operational perspective.

Fake BAMs come from a fast streaming generator by default, for both scaled and `--full-size` data. It writes coordinate-sorted paired-end reads straight into `samtools view`, uses real reference sequence when a FASTA is resolved, and applies deterministic SNPs with `NM` tags. The older generator, `--legacy-bam`, is slower and scaled-only, but adds randomized placement and indel CIGARs; use it when you specifically need indel-heavy synthetic reads.
:::

::: code-panel title=fake-data.sh subtitle="test artifacts"
```
wgsextract qc fake-data --type bam --coverage 1 --outdir out/fake-fast
wgsextract qc fake-data --type bam,vcf,fastq --coverage 0.1 --outdir out/fake-all
wgsextract qc fake-data --type bam --coverage 0.1 --full-size --outdir out/fake-full
wgsextract qc fake-data --type bam --coverage 1 --legacy-bam --outdir out/fake-legacy
```
:::
:::
:::
