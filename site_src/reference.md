---
output: 'reference.html'
title: 'Reference | WGS Extract CLI'
description: 'WGS Extract glossary, file extension reference, tool reference, privacy notes, and troubleshooting guide.'
eyebrow: 'Lookup pages'
heading: 'Glossary, files, tools, privacy, and troubleshooting.'
lede: 'Use this as a quick reference when a command mentions an unfamiliar acronym, file extension, external tool, reference-build concept, or common failure mode.'
toc: 'Glossary|glossary; Files|files; Tools|tools; Privacy|privacy; Troubleshooting|troubleshooting'
footer_title: 'WGS Extract Reference'
footer_text: 'Glossary, files, tools, and troubleshooting.'
footer_link_text: 'Back home'
footer_link_href: 'index.html'
---

::: section id=glossary
::: wrap
::: section-head
## Core glossary
Short definitions for terms you will see across WGS Extract, samtools/bcftools, variant annotation tools, and consumer SNP chip workflows.
:::

This glossary is intentionally practical. The same words can have precise meanings in genomics papers, tool manuals, and consumer genealogy sites, but WGS Extract users usually need to know what a term implies for files, commands, references, and interpretation.

::: grid three
::: card
### Alignment
Mapping sequencing reads to a reference genome, producing a BAM or CRAM. Alignment decides where each read most likely came from, so reference choice and aligner behavior affect every downstream result.
:::

::: card
### Allele
A version of a sequence at a genomic position. Humans usually have two alleles for autosomal positions, one inherited from each parent, but sex chromosomes, mitochondrial DNA, deletions, duplications, and mosaic or mixed signals can complicate that simple model.
:::

::: card
### Annotation
Metadata added to variants, such as gene name, population frequency, predicted effect, transcript consequence, or known clinical significance. Annotation helps prioritize records, but it depends on databases, versions, transcripts, and reference builds.
:::

::: card
### Build
The reference genome version used for coordinates, such as hg19, hg38, or T2T. Build mismatches are one of the easiest ways to produce plausible-looking but wrong results.
:::

::: card
### Coverage
How many reads support a base or region. Depth describes how many reads cover a position; breadth describes how much of a region has usable coverage. Averages can hide gaps.
:::

::: card
### Haplogroup
A lineage classification based on Y-DNA or mitochondrial DNA markers. Haplogroups are useful for genealogy and population history, but they are not the same as a complete ancestry estimate.
:::

::: card
### Liftover
Coordinate conversion between reference builds, usually with chain files. Liftover can be useful for moving known positions between hg19 and hg38, but complex regions may fail or map ambiguously.
:::

::: card
### Microarray / SNP chip
A fixed marker test that checks selected positions chosen by the chip design. WGS Extract can simulate microarray / SNP chip raw-data files from sequencing data for tools that expect those formats.
:::

::: card
### Ploidy
How many copies of a chromosome or region are expected, such as two autosomal copies or one Y chromosome in many male samples. Incorrect ploidy assumptions can affect variant calling and filtering.
:::

::: card
### Reference
The genome sequence used as the coordinate system for alignment and comparison. A CRAM may also need the exact reference to decode reads later.
:::

::: card
### Variant
An observed difference from the reference. Variants include SNPs, insertions/deletions, structural variants, and copy-number changes.
:::

::: card
### Variant caller
A tool that reads alignments and emits VCF records for likely variants. Different callers optimize for different data types, variant classes, speed, and accuracy tradeoffs.
:::
:::
:::
:::

::: section id=files
::: wrap
::: section-head
## File extension reference
Genome projects create many sidecar files. Keep them with the primary file unless you know they can be regenerated.
:::

Most genomics tools discover sidecar files by filename convention. Moving `sample.bam` without `sample.bam.bai`, or moving `calls.vcf.gz` without its index, can turn a previously working command into a slow or failing one. When archiving, move the primary file, index, reference metadata, and notes together.

::: table-wrap
| Extension | What it is | Common sidecars |
| --- | --- | --- |
| `.fastq`, `.fq`, `.fastq.gz` | Raw sequencing reads with base quality scores. Often paired as read 1 and read 2 for paired-end sequencing. | R1/R2 mate files, quality-control reports. |
| `.sam` | SAM, a text alignment format. It is large and rarely kept long-term. | Usually converted to BAM or CRAM. |
| `.bam` | BAM, binary aligned reads. Common input for WGS Extract. | `.bai` index. |
| `.cram` | CRAM, reference-based compressed aligned reads. Good for storage. | `.crai` index plus matching reference FASTA. |
| `.vcf`, `.vcf.gz` | VCF, variant calls and annotations. | `.tbi` or `.csi` index for compressed VCF. |
| `.bcf` | BCF, binary VCF-like format used by bcftools. | `.csi` index. |
| `.fa`, `.fasta`, `.fna` | FASTA reference or sequence file. | `.fai`, sequence dictionary, bwa/minimap2 indexes. |
| `.bed` | BED, genomic intervals. | Build-specific coordinate notes. |
| `.chain` | Liftover mapping between reference builds. | Source and target build documentation. |
| `.dict` | Sequence dictionary describing reference contigs. | Usually paired with a reference FASTA. |
| `.txt`, `.csv`, `.tsv` | Common text outputs, including microarray / SNP chip-style raw-data files. | Format documentation and reference build notes. |
:::
:::
:::

::: section id=tools
::: wrap
::: section-head
## External tool reference
WGS Extract orchestrates established tools instead of reimplementing every algorithm.
:::

Tool names often become shorthand for whole classes of operations. When troubleshooting, keep track of both the WGS Extract command and the external tool it wraps. The same input can behave differently if a tool version, reference build, thread count, or memory setting changes.

::: grid three
::: card
### samtools / htslib
Alignment viewing, sorting, indexing, conversion, and many low-level operations around BAM, CRAM, and VCF files. If a file cannot be indexed, viewed, or queried, samtools-level checks are often the first place to look.
:::

::: card
### bcftools
Variant calling, filtering, annotation, indexing, and VCF/BCF manipulation. It is useful for both generating calls and inspecting whether a compressed variant file is properly indexed.
:::

::: card
### bwa / minimap2
Read alignment for short reads and long reads respectively. The aligner influences mapping quality, duplicate behavior, and how reads in repetitive or structurally complex regions are represented.
:::

::: card
### fastqc / fastp
Raw read QC reports, adapter trimming, filtering, and preprocessing. These are most relevant when starting from FASTQ rather than an already aligned BAM or CRAM.
:::

::: card
### GATK / DeepVariant
GATK and DeepVariant are high-accuracy variant calling workflows with different runtime and model tradeoffs. They can be powerful, but they are also sensitive to references, resources, and platform assumptions.
:::

::: card
### VEP
VEP annotations add consequences, genes, transcripts, impact, and plugin/cache-driven metadata. A VEP result should be interpreted with the cache version and reference build in mind.
:::
:::
:::
:::

::: section id=privacy
::: wrap
::: section-head
## Privacy and data handling
Whole genome data is uniquely identifying. Treat it as sensitive personal and family information.
:::

Genetic data can reveal information about biological relatives who did not personally choose to share data. Even a small derived file, such as a microarray / SNP chip simulation or a filtered VCF, can contain identifying information. Logs can also leak filenames, sample IDs, paths, or variant names.

Local-first processing reduces exposure, but it does not remove the need for careful backups, disk encryption, access control, and deliberate sharing. Before uploading to a third-party service, consider whether the service allows deletion, whether relatives could be implicated, and whether a smaller or fake dataset would answer the question.

::: grid three
::: card
### Prefer local processing
WGS Extract is designed for local workflows. Be intentional before uploading BAM, CRAM, VCF, FASTQ, or microarray / SNP chip files to third-party sites.
:::

::: card
### Keep raw data backed up
Preserve original FASTQ/BAM/CRAM files, reference build notes, checksums, and tool commands. Derived files can often be regenerated, but raw data may be expensive or impossible to replace.
:::

::: card
### Share minimized outputs
When debugging, use fake data, region-limited extracts, or redacted logs instead of whole-genome files whenever possible. If real data is necessary, share the smallest region and least identifying output that can reproduce the problem.
:::
:::

::: callout
{{ text: **Not medical advice:** Variant interpretation can be difficult and clinically consequential. Confirm medically relevant findings through qualified professionals and validated labs. Do not treat a consumer annotation, SNP chip report, or exploratory filter result as a diagnosis. }}
:::
:::
:::

::: section id=troubleshooting
::: wrap
::: section-head
## Troubleshooting checklist
Most failures come from missing tools, mismatched references, absent indexes, unsorted files, permissions, or jobs that need more disk/RAM/time.
:::

Troubleshooting is faster when you reduce the problem. Run a command on a tiny region, confirm the file opens with the expected tool, check indexes, and verify that chromosome names match. Save the exact command and the first meaningful error message; later lines often describe follow-on failures rather than the root cause.

::: grid two
::: card
### Command not found
Run through Pixi: `pixi run wgsextract ...`. Check `pixi install` completed and `pixi run wgsextract deps check` passes. If a wrapped external tool is missing, verify that you are using the expected Pixi environment.
:::

::: card
### Reference errors
Verify the FASTA exists, is indexed, and matches the build used by the input BAM, CRAM, or VCF. A CRAM may fail even when the path looks correct if the reference content does not match.
:::

::: card
### Region not found
Check chromosome naming. Some files use `chrM`, `chrY`, `chr1`; others use `MT`, `Y`, `1`. Use file headers and index stats to confirm the available contig names.
:::

::: card
### Variant caller fails
Confirm the alignment is coordinate sorted, indexed, readable, and aligned to the same reference passed with `--ref`. Also check memory, temporary disk space, and whether the selected caller supports the sequencing technology.
:::

::: card
### Job is too slow
Run a small `--region` first, then increase threads and output disk. Whole-genome callers can take hours. If performance changes unexpectedly, compare input size, compression format, storage location, and thread count.
:::

::: card
### SNP chip output looks sparse
Microarray / SNP chip simulations only include requested target markers. Missing calls can come from low coverage, build mismatch, chromosome naming differences, or target markers that are not represented in the input data.
:::

::: card
### Windows tool issues
Use the native MSYS2 UCRT64 pacman runtime on Windows; it is the recommended path for normal Windows installs. WSL2 is not recommended as the default runtime because it can require Windows feature changes, reboots, Linux user setup, and path translation, and it is often slower when working with files stored on the Windows side. If you are troubleshooting an older WSL setup, keep native Windows paths, MSYS2 paths, and WSL paths separate because they should not be mixed accidentally.
:::

::: card
### Output files are confusing
Use a separate `--outdir` per sample or workflow. Keep command logs and reference-build notes beside generated files so you can distinguish raw inputs, intermediate files, final outputs, and exploratory experiments.
:::
:::
:::
:::
