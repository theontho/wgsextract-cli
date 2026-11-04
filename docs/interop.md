# Working with professional pipelines

WGS Extract is a desktop toolkit, not a pipeline framework. For production
cohort work, the community-standard Nextflow pipelines are
[nf-core/sarek](https://nf-co.re/sarek) for short reads and EPI2ME's
[wf-human-variation](https://github.com/epi2me-labs/wf-human-variation) for
Oxford Nanopore. This page covers three things:

- handing our BAM/CRAM files to those pipelines (or to GATK directly);
- bringing their VCFs back for the consumer-facing features, such as
  microarray kits, haplogroups and reports;
- putting our metrics into the same MultiQC report as everything else.

## Our files

| Output | Format and conventions |
| :--- | :--- |
| `align`, `realign` | Coordinate-sorted, indexed BAM or CRAM (`--format`). There is one read group: `ID` is the reads' file stem, `SM`/`LB` come from `--sample`, and `PL` comes from `--platform`. Duplicates are marked with samblaster on short reads. There is no base quality recalibration. |
| CRAM | Decodes only with the exact FASTA it was written against, which is the one in your reference library (`ref library-list`). Pass the same file to every downstream tool. A different "GRCh38" (for example GATK's `Homo_sapiens_assembly38.fasta` against our `hs38DH`) has different extra contigs and cannot decode it. When in doubt, convert with `bam to-bam`. |
| `vcf snp`/`indel`/`gatk`/`deepvariant`/... | bgzipped, tabix-indexed, single-sample VCF named after the input, with chromosome names as in the reference. |
| `*.manifest.json` | The [result manifest](result-manifest.md) of each run: inputs, outputs, metrics, warnings and provenance. |

Run `wgsextract info --detailed` on a BAM/CRAM before handing it off. It
reports the reference build and model, read length, insert size, duplicate
rate and inferred sex, which are what the pipelines below ask for.

## nf-core/sarek

Sarek takes a CSV samplesheet. Our CRAM is already aligned and duplicate-marked,
so start at the step after duplicate marking:

```bash
wgsextract info --detailed --input sample.cram     # build, sex (XX/XY)
printf 'patient,sex,status,sample,cram,crai\n' > samplesheet.csv
printf 'P1,XY,0,P1_N,%s,%s\n' "$PWD/sample.cram" "$PWD/sample.cram.crai" >> samplesheet.csv

nextflow run nf-core/sarek -r <VERSION> -profile docker \
  --input samplesheet.csv --outdir sarek \
  --step variant_calling --tools haplotypecaller,deepvariant \
  --igenomes_ignore --fasta /path/to/reference/genomes/hs38DH.fa
```

- **Which step.**
  - `--step variant_calling` skips base quality recalibration. That is right
    for DeepVariant, and acceptable for HaplotypeCaller on modern data.
  - To recalibrate, use `--step prepare_recalibration` and give sarek its
    known-sites resources (`--dbsnp`, `--known_indels`).
  - Starting from FASTQ instead (`--step mapping`, with columns
    `patient,sample,lane,fastq_1,fastq_2`) re-aligns everything with sarek's
    own reference.
- **Reference.** Pass the FASTA your CRAM was written against, decompressed
  with its `.fai`, plus `--igenomes_ignore`. Sarek builds the rest of the
  indexes it needs.
  - `--genome GATK.GRCh38` only works for CRAMs aligned to that exact FASTA.
  - Re-align to it first with `wgsextract realign` if you want sarek's
    bundled resources.
- **Several samples.** Add a row per sample and `--joint_germline` to get a
  joint-called VCF.

Sarek's germline outputs, and what to do with them here:

| Sarek output | Bring back with |
| :--- | :--- |
| `variant_calling/<tool>/<sample>/<sample>.<tool>.vcf.gz` | `microarray --input ...`, `lineage ...`, `vcf annotate`/`vcf clinvar`, `vcf to-fasta` |
| `variant_calling/deepvariant/<sample>/<sample>.deepvariant.g.vcf.gz`, `.../haplotypecaller/<sample>/<sample>.haplotypecaller.g.vcf.gz` (gVCF) | `microarray --input ...` (see [Bringing VCFs back](#bringing-vcfs-back)) |
| `variant_calling/haplotypecaller/joint_variant_calling/joint_germline.vcf.gz` (multi-sample) | `microarray --input ... --sample P1_N`, `vcf to-fasta --sample P1_N` |
| `preprocessing/recalibrated/<sample>/<sample>.recal.cram` | Any BAM/CRAM command, such as `lineage`, `microarray`, `extract`, `typing` and `info`. Use sarek's reference (`--ref`). |
| `multiqc/multiqc_data/` and the report | Combine with ours; see [MultiQC](#multiqc) below. |

## wf-human-variation (Oxford Nanopore)

```bash
wgsextract bam to-bam --input sample.cram --outdir handoff    # if you have a CRAM
nextflow run epi2me-labs/wf-human-variation -profile standard \
  --bam handoff/sample.bam --ref /path/to/reference/genomes/hs38DH.fa \
  --sample_name SAMPLE --snp --sv --out_dir wf-hv
```

- **Reads and methylation.** Our `align`/`realign` keep the MM/ML
  methylation tags on ONT reads, so `--mod` works on our BAMs.
- **Reference.** Pass the same reference the BAM was aligned to.
- **gVCF.** Add `--GVCF` for a gVCF alongside the VCF.
- **Outputs to bring back.**
  - `SAMPLE.wf_snp.vcf.gz` (short variants) works with `microarray`,
    `lineage` and the `vcf` annotation commands.
  - `SAMPLE.wf_sv.vcf.gz` holds structural variants.
  - Unaligned input comes back aligned as `SAMPLE.cram`.

## GATK directly

Our BAM/CRAM already carries what GATK requires:

- a read group with `SM`, `LB` and `PL`;
- coordinate sorting and an index;
- duplicate marking.

For example:

```bash
gatk HaplotypeCaller -R hs38DH.fa -I sample.cram -O sample.g.vcf.gz -ERC GVCF
```

`wgsextract vcf gatk` runs HaplotypeCaller for you. `bam to-ubam` writes the unaligned BAM
that GATK's Best Practices preprocessing starts from.

## Bringing VCFs back

- **Multi-sample VCFs**, such as a joint-called cohort or a trio:
  - `microarray` refuses them unless you choose a sample with `--sample`. The
    error lists the samples.
  - The kit and its manifest are named `<vcf>_<sample>_...`, and the manifest
    records the sample. `vcf to-fasta` takes `--sample` the same way.
- **gVCF input.**
  - `microarray` recognises gVCFs by their reference blocks (`<NON_REF>`,
    `<*>` or `.` with `END=`, as GATK, DeepVariant and DRAGEN write them;
    Sequencing.com's "genome" VCFs are DRAGEN gVCFs) and reads each target
    SNP from the block or variant covering it.
  - A target inside a `0/0` block is a reference call. A target inside a
    `./.` block is a no-call, and so is a target that no record covers. Where
    a block overlaps a variant record, the variant wins.
  - This applies to GRCh37, GRCh38 and T2T/CHM13 gVCFs alike.
  - In a plain VCF, by contrast, an absent site is assumed to be the reference
    (`--assume-hom-ref`). Pass `--assume-hom-ref` (or set `microarray_assume_hom_ref` in
    `config.toml`) to force that behaviour on
    a gVCF as well.
- **Builds.**
  - Microarray kits need the VCF's build in your reference library; pass it
    with `--ref`. Reference-block bases come from that FASTA, whose `chr1`
    or `1` naming needn't match the VCF's.
  - GRCh38 VCFs are lifted to the kits' hg19 coordinates exactly as our own
    calls are.

## MultiQC

[MultiQC](https://multiqc.info) is how professional pipelines (including both
of the above) summarise a run. Our metrics can join that report in two ways.

**Without MultiQC installed**, write MultiQC custom-content files from the
manifests and hand them to any MultiQC run, such as sarek's own:

```bash
wgsextract info --detailed --json --input sample.cram > runs/sample.info.json
wgsextract lineage mt-haplogroup --input sample.cram --outdir runs
wgsextract qc multiqc-metrics --input runs --outdir runs/multiqc
multiqc sarek/ runs/multiqc            # our sections join sarek's report
```

`qc multiqc-metrics` reads every `*.manifest.json`, plus saved `--json` output,
under `--input`. `info` writes no manifest, so save its `--json`. It writes:

| File | Content |
| :--- | :--- |
| `wgsextract_alignment_mqc.json` | `info`: sex, depth, reads mapped, read length, insert size, duplicates, build/model, sequencer |
| `wgsextract_lineage_mqc.json` | `lineage`: mtDNA and Y haplogroups (with quality), Y-STR panel coverage |
| `wgsextract_microarray_mqc.json` | `microarray`: autosomal calls, homozygosity, kits, worst no-call rate |
| `wgsextract_variants_mqc.json` | Record counts of the VCFs our callers and extractors wrote |
| `wgsextract_warnings_mqc.json` | Count and codes of the structured warnings each sample raised |
| `wgsextract_general_stats_mqc.json` | Sex, depth, % mapped, duplicates, haplogroups and warnings in MultiQC's General Statistics table |

How samples, runs and stale files are handled:

- **Sample names.** Rows are keyed by sample: the input's file name without
  its type extension, or the `--sample` chosen from a multi-sample VCF.
  Name your inputs as the pipeline names its samples (sarek's `sample`
  column), and the rows line up.
- **Repeated runs.** When a command ran more than once for a sample, the
  latest successful run wins.
- **Stale files.** Rerunning replaces the files and removes ones whose
  section no longer has data.
- **MultiQC options.** These are standard
  [custom-content](https://docs.seqera.io/multiqc/custom_content) files, so
  MultiQC options such as `--sample-names` and `--ignore` apply.

**With MultiQC installed** (`pixi install -e multiqc`, version 1.32 or newer,
below 2), `qc multiqc --input DIR` converts the manifests itself and writes one
offline `multiqc_report.html`. The report includes:

- FastQC reports;
- `bcftools stats` output, as written by `qc vcf`;
- our sections.
