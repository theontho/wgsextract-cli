---
output: 'workflows.html'
title: 'Workflows | WGS Extract CLI'
description: 'Common WGS Extract workflows for ancestry, SNP chip simulation, variant calling, extraction, references, and storage.'
eyebrow: 'Goal-oriented recipes'
heading: 'Start with what you want to produce.'
lede: 'Whole genome workflows are easier when you choose the output first: an uploadable microarray / SNP chip file, a smaller Y/MT extract, a VCF, a storage-friendly CRAM, or a full FASTQ-to-BAM alignment. Examples write plain `wgsextract`; see [Running the CLI](install.html#running) for your launcher.'
toc: 'Microarray / SNP chip|microarray; Y/MT|ymt; Variants|variants; FASTQ|fastq; Storage|storage; Testing|testing; Benchmark|benchmark'
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

Start with one target format and a small region if you are checking plumbing. Every run writes the combined kit (all chips' markers); `--formats all` writes only that, and a comma-separated list adds vendor files. If a target site has too little coverage or ambiguous data, the simulated result may be missing or no-called depending on the format and command behavior.

Consumer exports use hg19 coordinates. hg38 input needs the liftover chain from `ref bootstrap`; the command fails early if it is missing. Native CHM13v2 input can also project to GRCh37/rCRS exports, but only with the exact **original chrM, masked-Y** analysis reference and required installed assets. Other CHM13 variants are rejected; ambiguous mappings stay no-calls. See [CHM13 requirements](https://github.com/WGSExtract/wgsextract-cli/blob/main/docs/microarray-chm13.md){.inline-link}.

Haploid calls follow vendor conventions: 23andMe writes chrY, chrM, and male non-PAR chrX as one letter (`A`); the other formats double them (`AA`). Heterozygous calls are alphabetical. Updated MyHeritage GSA and Ancestry layouts are separately selectable without removing older IDs. Optional quality thresholds, `--strand-ambiguous no-call`, `--resolve-rsids` and `--call-reasons` add conservative filtering and diagnostics without changing defaults. Calls are written on the GRCh37 forward strand; `--quirks vendor` instead writes the markers a template's vendor revision is publicly known to have reported on the reverse strand that way.

AADR 1240K, HO and 1240K+HO research panels write both PLINK and EIGENSTRAT files. The opt-in `aadr_2m` panel needs an explicit `ref aadr --install --ref LIBRARY` first; it is not included in `all`, requires an installed forward chain for hg38, and does not support CHM13/T2T. See [AADR coordinates and allele conventions](https://github.com/WGSExtract/wgsextract-cli/blob/main/docs/aadr.md){.inline-link}.

Insertion/deletion probes are opt-in with `--indel-manifest FORMAT=PATH`, using an applicable, sequence-defined manufacturer manifest. Unsupported probes or missing evidence stay no-calls; this is not unrestricted indel discovery. See [manifest requirements](https://github.com/WGSExtract/wgsextract-cli/blob/main/docs/microarray-indels.md){.inline-link}.
:::

::: code-panel title=microarray-snp-chip.sh subtitle="common formats"
```
wgsextract --input sample.bam microarray --formats all
wgsextract --input sample.bam microarray --formats 23andme_v5
wgsextract --input sample.bam microarray --formats 23andme_v5_2025,ancestry_v2_2024,ftdna_v3
wgsextract --input sample.bam microarray --formats aadr_1240k,aadr_ho
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
wgsextract --input sample.bam lineage y-str --panel Y-37
```

Native Yleaf-derived results go to `<outdir>/yleaf/`: prediction TSV, marker tables and `run.json` record the upstream/data tag, build, tree and QC settings. No external Yleaf installation is needed. Tables download on demand, with pinned SHA-256 verification, into the configured reference library. CRAM and variant-only VCF inference require the matching `--ref`; targeted VCF panels should use `--no-ref-inference`. Choose `--tree yfull|yfull_v10|ftdna|isogg`; the default `yfull` is a pinned YFull YTree release (14.06.0), whose tree (CC-BY-4.0) and YBrowse-derived marker tables (CC-BY-SA) are separate [data modules](reference.html#licences){.inline-link}, and results and `run.json` record the tree's name, version, date and license. Register your own trees in config.toml `[y_trees.<id>]` tables (local files, or URLs pinned by SHA-256), or pass a one-off `--tree-file` with `--pos-file`; the `y_tree` setting picks the default tree (see the native Yleaf README). A missing or `NA` haplogroup is an error, not a successful prediction. Credits: Yleaf authors at Erasmus MC; Ralf et al., *Mol Biol Evol* (2018), [doi:10.1093/molbev/msy032](https://doi.org/10.1093/molbev/msy032). Use `lineage mt-haplogroup` for the maternal haplogroup with Haplogrep 3 (version 3 or later is required).

`lineage mt-haplogroup` classifies against `--tree phylotree-fu-rcrs` (PhyloTree 17 Forensic Update 1.3, the default), `phylotree-rcrs` (PhyloTree 17.3) or `mitotree-rcrs` (Mitotree 2026.05.06); set the default with the `haplogrep_tree` setting. Trees download on first use from their upstream GitHub releases, with pinned SHA-256 verification of the archive and every file, into `<reference library>/haplogrep3/trees/`. `haplogrep_results.txt` and the result manifest record the tree and the Haplogrep version. `--contamination` also runs haplocheck (Weissensteiner et al., *Genome Research* 2021): from a BAM/CRAM it calls heteroplasmies with mutserve first; a VCF must carry heteroplasmy levels (FORMAT/AF). Its report, `haplocheck_contamination.txt`, gives the contamination status and level, and a `YES` adds an `mt_contamination` warning. Haplogrep 3 and haplocheck are Java tools from Bioconda; native Windows ARM64 runs restricted builds of them that accept VCF and BAM/CRAM input but not FASTA.

`lineage y-str` writes FamilyTreeDNA Y-12 to Y-111, PowerPlex Y23 or Yfiler Plus repeat counts to `<outdir>/ystr/`, including an FTDNA-ordered CSV. Markers that short reads cannot span are reported as no-calls rather than guessed. 99.9% of calls on 30x 1000 Genomes samples match their published PowerPlex Y23 typings.
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

SNP/indel calling remains serial by default. `--parallel` opts into per-contig workers, whose bcftools subsampling can change some calls or likelihoods on full genomes; it is not guaranteed byte-identical to serial output.

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

# Pharmacogenomics: CYP2D6 and HLA from the reads, the rest from the VCF
wgsextract --input sample.cram --outdir out typing cyp2d6
wgsextract --input sample.cram --outdir out typing hla
wgsextract vcf pharmcat --vcf-input calls.vcf.gz \
  --outside-call out/sample.cyp2d6.pharmcat.tsv --outside-call out/sample.hla.pharmcat.tsv
```
:::

::: grid three
::: card
### SNP and InDel calls
SNP and InDel workflows target small sequence changes. They are often the first full-genome variant calls people run, but they still need a matching reference, sorted/indexed alignments, and enough coverage.

DeepVariant uses an already-installed local runner or Docker image (standard x86 image `google/deepvariant:1.10.0`); Docker/images are never installed by calling. On Apple Silicon the x86 image runs under slow emulation; native Windows DeepVariant is unsupported. Since v0.6.6, native Linux ARM64 CPU calling supports **WGS only**, using `localhost/wgsextract-deepvariant:1.10.0-arm64`, built locally with `bash packaging/deepvariant-arm64/build.sh out/deepvariant-arm64-build`. Other models and `--checkpoint` are refused; GATK and Parabricks remain gated on ARM64. See [native ARM64 DeepVariant setup and qualification](https://github.com/WGSExtract/wgsextract-cli/blob/main/docs/deepvariant-arm64.md){.inline-link}.
:::

::: card
### Structural and copy-number calls
SV and CNV workflows look for larger changes. These can be sensitive to sequencing technology, read length, depth, and caller assumptions, so compare outputs cautiously. `vcf cnv` runs Delly 2, which estimates mappability from the reads itself (no map file needed), and takes an integer `--ploidy` (default 2). For PacBio data, `vcf sv --pacbio` uses pbsv or Sniffles, and combining `--pacbio` with `--caller delly` is an error.
:::

::: card
### Annotation and filtering
Annotation tools such as VEP add biological context. Download annotation data with the `ref` commands first; `ref spliceai` fetches Ensembl's open hg38 scores, while hg19 needs a manual BaseSpace download. Gene filters need gene maps from `ref gene-map` (hg19/hg38 only). A filtered list is not a diagnosis. Some genes need reads rather than a VCF: `typing` calls CYP2D6, HLA and repeat expansions from a whole-genome BAM/CRAM.
:::
:::

::: callout
{{ text: **DeepVariant and CRAM 3.1:** the DeepVariant container's older htslib cannot decode CRAM 3.1. Since v0.6.6, [#294](https://github.com/WGSExtract/wgsextract-cli/pull/294){.inline-link} automatically stages CPU DeepVariant CRAM 3.1+ input as indexed CRAM 3.0, honors requested regions, checks disk space and cleans up afterward. The original stays unchanged; allow extra disk and transcode time. BAM, CRAM 3.0 and Parabricks inputs are unchanged. }}
:::

::: callout
{{ text: **NVIDIA GPU option:** `align`, `realign`, `vcf gatk` and `vcf deepvariant` accept `--gpu` for Parabricks on Linux x86_64. Prepare Docker, NVIDIA Container Toolkit, a suitable GPU (16 GB+), and the Parabricks image yourself; the CLI only validates them. Native Windows/macOS and Linux ARM64 GPU paths are unsupported; WSL2 is untested. Ultima flow workflows are not supported by this path. See [GPU setup](https://github.com/WGSExtract/wgsextract-cli#nvidia-gpus-parabricks){.inline-link} and [hardware and acceleration](https://github.com/WGSExtract/wgsextract-cli/blob/main/docs/hardware.md){.inline-link}. }}
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

An explicit `--aligner` is always used as given, and combinations that make no sense (for example `--aligner bwa` with a PacBio platform) are rejected rather than silently changed. minimap2 accepts the PacBio preset names `ccs` and `subread`, mapped to `map-hifi` and `map-pb`. `realign` checks free disk space before starting and reuses previously extracted FASTQs when they exist. Ultima Genomics BAM/CRAMs (`PL:ULTIMA`) are recognized and aligned with BWA-MEM, preserving available flow tags (`tp`, `t0`) in the correct orientation. GATK supplies flow-mode duplicate marking; without it, samtools marking is used with a warning. bcftools uses `-X ultima`; GATK flow calling and DeepVariant's `ULTIMA` model additionally require flow metadata and tags.

Ultima data includes MyHeritage and Nebula/DNA360 WGS. Automatic DeepVariant `ULTIMA` selection requires an Ultima read group, its flow order (`FO`), and detected flow tags; prepare the experimental `google/deepvariant:gh1100-ultima` image for that path, which is not enabled on ARM64. MyHeritage CRAMs without flow tags are called as ordinary short reads instead: by default DeepVariant selects `WGS`, using `google/deepvariant:1.10.0` on Linux x86_64/macOS or the locally built ARM64 image on Linux ARM64. The selected image must already be installed for the Docker path. Low-pass MyHeritage WGS (about 2x) is too shallow for reliable genotypes without imputation, which WGS Extract does not provide. `info` recognizes MyHeritage's Ultima read names (v0.6.6 and later).

Oxford Nanopore reads are detected from `PL:ONT` read groups or UUID read names (or set `--platform ont`): alignment uses minimap2's `map-ont` preset and keeps MM/ML methylation tags, DeepVariant selects `ONT_R104` on x86-64, `vcf sv` uses Sniffles, and `vcf clair3` is an optional Linux x86-64 caller. Whole-genome ONT calling is still **unvalidated**, not a supported end-to-end promise; see the [Nanopore guide](https://github.com/WGSExtract/wgsextract-cli/blob/main/docs/ont.md){.inline-link}. Track real-data validation and caller/model work in [#301](https://github.com/WGSExtract/wgsextract-cli/issues/301){.inline-link}.

Paired short-read realignment streams into BWA/BWA-MEM2 without FASTQ intermediates whenever one collate bucket fits in RAM beside alignment, including 30x genomes; otherwise it uses the file-based path. Fixed BWA batches and memory/disk preflight help reproducibility and planning, but do not guarantee runtime.
:::

::: code-panel title=fastq-to-bam.sh subtitle="raw data path"
```
wgsextract --input sample_R1.fastq.gz qc fastqc
wgsextract qc fastp --r1 sample_R1.fastq.gz --r2 sample_R2.fastq.gz
wgsextract align --r1 sample_fp_1.fastq.gz --r2 sample_fp_2.fastq.gz --ref /refs/hs38.fa

# Long reads: pick the platform; the aligner follows (pbmm2 or minimap2)
wgsextract align --r1 sample.hifi.fastq.gz --platform hifi --ref /refs/hs38.fa

# Ultima Genomics reads (single-end FASTQ): flow-aware alignment and duplicates
wgsextract align --r1 sample.ultima.fastq.gz --platform ultima --ref /refs/hs38.fa

# Re-align an existing BAM/CRAM to a different reference (Ultima detected from its read group)
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

::: section id=benchmark
::: split
::: block
## Benchmark a reproducible workflow
Start with the smoke profile, then choose a larger profile or release-backed dataset with `benchmark --help`. The default is synthetic; `--dataset real` uses the mini-genome, while `real-1x` and `real-30x` use public WGS data. Reports record tool versions, machine details, timings and per-step logs. Compare matched inputs, references and resources; a tiny smoke result is not a full-genome speed claim.

The [microarray benchmarks](https://github.com/WGSExtract/wgsextract-cli/blob/main/docs/microarray-benchmarks.md){.inline-link} document vendor concordance and parallel measurements.
:::

::: code-panel title=benchmark.sh subtitle="small first"
```
wgsextract benchmark --profile smoke --suite core --outdir out/benchmark-smoke
wgsextract benchmark --help
```
:::
:::
:::
