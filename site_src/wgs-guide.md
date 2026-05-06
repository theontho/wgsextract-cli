---
output: wgs-guide.html
title: Whole Genome Guide | WGS Extract CLI
description: A friendly guide to whole genome sequencing concepts, file types, coverage, references, variants, SNP chips, and practical uses.
eyebrow: Genomics primer
heading: Whole genome sequencing, explained practically.
lede: WGS reads across nearly all of your DNA instead of checking only a small set of predefined markers. That makes it flexible: the same data can be revisited as reference genomes, callers, annotations, and research improve.
toc: Test types|types; File flow|files; Coverage|coverage; Variants|variants; References|references; Uses and limits|uses
footer_title: Whole Genome Guide
footer_text: Concepts for practical WGS work.
footer_link_text: Next: reference
footer_link_href: reference.html
---

::: section id=types
::: wrap
::: section-head
## WGS vs microarray / SNP chip vs exome
These tests answer different questions. WGS is broad, microarrays are often called SNP chips because they check selected marker sites, and exomes focus on protein-coding sequence.
:::

WGS is best thought of as a reusable data source rather than a single report. Once a genome is sequenced and aligned, you can revisit the same data for ancestry, mitochondrial and Y-lineage work, variant calling, annotation, storage conversion, or new research questions. The tradeoff is that the data is large, the workflows can be slow, and every analysis depends on the reference build and tools used.

A microarray, also called a SNP chip, is more like a fixed questionnaire. It checks a curated set of positions chosen by the chip designer. That is why consumer ancestry files are small and upload-friendly, but also why a SNP chip can miss rare variants, structural changes, or markers not included in that chip version.

::: grid three
::: card
### Whole genome sequencing
Attempts to sequence the full genome: autosomes, sex chromosomes, mitochondrial DNA, coding regions, non-coding regions, and many structural signals.

Short-read WGS is common and cost-effective, while long-read data can resolve some repetitive or structurally complex regions better. Either way, WGS Extract expects you to keep the raw files, aligned files, reference build notes, and exact commands together so future analyses remain understandable.

{{ tag: broadest reuse }}
:::

::: card
### Microarray / SNP chip genotyping
Checks selected markers chosen by a chip design. It is common for ancestry, but misses most variants that are not on the chip.

WGS Extract can simulate several consumer raw-data formats from WGS data by reading positions that overlap a target SNP chip. The simulated file can be useful for tools that only accept microarray-style input, but it is still derived from sequencing data and should be labeled as such when you share it.

{{ tag: cheap }}
{{ tag: fixed markers }}
{{ tag: upload-friendly }}
:::

::: card
### Exome sequencing
Targets protein-coding regions. Useful for many clinical questions, but it skips most non-coding and structural context.

An exome can be powerful when the question is mainly about protein-coding variants, but it is not a replacement for WGS. It often has uneven capture coverage, does not represent every exon equally, and usually provides much less information for ancestry, mitochondrial, Y-DNA, non-coding, and structural workflows.

{{ tag: coding focus }}
:::
:::
:::
:::

::: section id=files
::: wrap
::: section-head
## The usual file flow
Most WGS analysis moves through a small set of file types. WGS Extract helps at several points in this chain.
:::

Sequencing usually starts with raw reads. Those reads are aligned to a reference genome, sorted, indexed, and then reused for downstream steps. A variant caller compares aligned reads to the reference and writes records for positions where the sample appears to differ. Reports, extracted reads, microarray / SNP chip simulations, annotations, and filtered files are derived outputs.

Keep the primary file and its sidecars together. A BAM without its `.bai` index is slower or unusable for random-access operations. A CRAM without the matching reference FASTA may be unreadable. A compressed VCF without an index cannot be queried efficiently by genomic region.

::: timeline
::: node
{{ node-text: FASTQ|Raw reads and base quality scores directly from sequencing. }}
:::
::: node
{{ node-text: BAM / CRAM|Reads aligned to a reference genome, sorted and indexed for access. }}
:::
::: node
{{ node-text: VCF / BCF|Variant calls: positions where the sample differs from the reference. }}
:::
::: node
{{ node-text: Reports / kits|Microarray / SNP chip simulations, haplogroups, annotations, extracts, and filtered outputs. }}
:::
:::
:::
:::

::: section id=coverage
::: split
::: block
## Coverage and quality
{{ lede: Coverage is how many reads support a position. A "30x" genome means an average of about 30 reads cover each position, but real coverage varies across GC-rich regions, repeats, sex chromosomes, mitochondrial DNA, capture gaps, and low-mappability regions. }}

Coverage has both depth and breadth. Depth asks how many reads cover positions that were successfully sequenced. Breadth asks how much of a region has enough usable data at all. Two files can both be described as "30x" while one has much better coverage across difficult regions.

Quality also depends on mapping, duplicates, base quality, read length, platform, and reference build. Low-quality reads can create false positives; overly strict filtering can hide real variants. That is why region-limited tests and careful inspection matter before launching whole-genome jobs.
:::

::: guide-panel
### Coverage questions to ask
- Is this average depth, median depth, or breadth of coverage?
- Are mitochondrial DNA and Y-chromosome reads present?
- Was the sample aligned to hg19, hg38, T2T, or another build?
- Is the alignment coordinate sorted and indexed?
- Are low-quality reads, duplicates, or platform-specific artifacts affecting calls?
- Is the region repetitive, duplicated, or known to be hard to map?
:::
:::
:::

::: section id=variants
::: wrap
::: section-head
## Variant types
Different callers and workflows target different kinds of changes. No single command is best for every variant class and sequencing technology.
:::

A variant is any observed difference from the reference sequence, but different classes behave very differently. Small variants such as SNPs and short insertions/deletions are usually easier to represent in a VCF. Larger rearrangements, repeat expansions, and copy-number changes often need specialized callers, better depth signals, or long-read data.

Microarray / SNP chip files mostly represent known single-position markers. They are useful and widely accepted by genealogy tools, but they should not be confused with a complete variant call set from WGS.

::: table-wrap
| Variant type | What it means | Why it matters |
| --- | --- | --- |
| SNP | One base differs from the reference. | Common in ancestry, trait, and many disease-risk annotations. This is the main marker type used by many SNP chips. |
| InDel | A small insertion or deletion. | Can disrupt genes and is harder to call than many SNPs, especially near repeats. |
| Structural variant / SV | A larger deletion, duplication, inversion, insertion, or rearrangement. | Often missed by microarrays and short-read SNP-only workflows. |
| CNV | A region is deleted or duplicated. | Can affect gene dosage and medically relevant regions. |
| mtDNA and Y-DNA markers | Lineage-informative variants in mitochondrial DNA or the Y chromosome. | Used for maternal and paternal haplogroups and genealogy. |
| Annotation | Added context such as gene, consequence, frequency, or known clinical database matches. | Helps prioritize variants, but does not make the result medical advice. |
:::
:::
:::

::: section id=references
::: wrap
::: section-head
## Reference builds and coordinates
A genomic coordinate only makes sense relative to a reference build. chr1:100000 in hg19 may not refer to the same biological position in hg38 or T2T.
:::

The reference genome is the coordinate system for the analysis. Alignment, variant calling, annotation, liftover, microarray / SNP chip simulation, and gene filtering all depend on it. Mixing builds can silently produce wrong answers because a position can move, disappear, change chromosome naming, or overlap different annotations.

If you receive a BAM, CRAM, or VCF from a lab, save any metadata that identifies the build. If the build is unknown, treat the file cautiously until you can infer or verify it from headers, contig names, known marker positions, or the provider's documentation.

::: grid three
::: card
### hg19 / GRCh37
Older but still common in genealogy tools, legacy annotations, and many older consumer datasets.

Some third-party tools and older SNP chip coordinates still expect hg19/GRCh37. That does not mean it is always better; it means you must match the expected coordinate system.
:::

::: card
### hg38 / GRCh38
Modern mainstream reference for many current short-read WGS pipelines and annotation resources.

Many current callers, annotation datasets, and public resources support hg38 well. If you are starting a new short-read workflow, this is often the practical default unless a downstream service requires another build.
:::

::: card
### T2T
T2T references cover many regions older references represented poorly, but tool and annotation support varies.

T2T can be valuable for difficult regions, but it may not be accepted by consumer tools, older annotation resources, or workflows that assume hg19/hg38 chromosome structures.
:::
:::

::: callout
{{ text: **Do not mix builds casually.** If your BAM was aligned to hg38, call variants and annotate with hg38-compatible resources unless you intentionally lift over coordinates or re-align. Liftover is useful, but it is not perfect and can fail around complex or ambiguous regions. }}
:::
:::
:::

::: section id=uses
::: wrap
::: section-head
## What WGS can be used for
WGS is powerful, but it is not magic. Results depend on data quality, tool choice, reference build, ancestry representation in databases, and careful interpretation.
:::

The practical value of WGS is that one dataset can serve many goals. You can create microarray / SNP chip-style files for services that require them, extract smaller mitochondrial or Y-DNA datasets, call variants, annotate candidate genes, or convert storage formats. The same flexibility also creates responsibility: each derived output should keep enough metadata to explain where it came from.

::: grid three
::: card
### Ancestry and genealogy
Microarray / SNP chip simulations, Y/MT extracts, haplogroups, triangulation, and upload-compatible files for third-party tools.

Ancestry use cases often care about compatibility. The "right" output is sometimes not the most complete output; it is the one accepted by the target service with the expected build, marker set, and file format.
:::

::: card
### Variant exploration
SNP, InDel, SV, CNV, VEP, ClinVar, gnomAD, conservation, splicing, and pharmacogenomic annotation workflows.

Exploration is not diagnosis. Use region tests, check coverage, review caller assumptions, and confirm important findings with qualified professionals and validated labs.
:::

::: card
### Data stewardship
Convert BAM to CRAM, index files, verify references, preserve metadata, and keep data private and backed up.

The raw and aligned files are sensitive and expensive to replace. Store them with checksums, reference-build notes, command logs, and enough organization that future you can understand what each file means.
:::
:::

::: callout
{{ text: **Privacy and medical caution:** Whole genome files can reveal sensitive information about you and biological relatives. This site is educational and does not provide medical advice. Uploading data to third-party services can create privacy and consent issues that are hard to undo. }}
:::
:::
:::
