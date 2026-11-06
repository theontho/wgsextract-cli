# Confidence labels on rare and clinical variants

A ClinVar "pathogenic" match or an allele missing from gnomAD is only as good
as the variant call under it. A 30x genome still has stretches at 5x, collapsed
duplications where reads map ambiguously, and strand-specific artifacts, and
the rarest variants in any genome are disproportionately calling errors. So
`vcf clinvar`, `vcf gnomad --max-af`, `vcf pharmcat` and the `analyze`
SNP/indel discovery report label every call they report. (Structural-variant
and CNV calls are not labelled: these rules read short-variant evidence.)

| Label | Meaning |
| --- | --- |
| `high` | The reads support the call well on every available measure. |
| `moderate` | Supported, but some evidence is thin (depth or genotype quality between the low and high thresholds, a skewed allele balance) or missing from the VCF. |
| `low` | At least one measure says the call may be wrong: too few reads, low genotype quality, reads far off the expected allele balance, ambiguous mapping, strand bias, or a caller FILTER. |

A label is about the **technical support for the call in this sequencing run**,
never about whether the variant causes disease. Even a `high` finding is a
research result: before acting on it, confirm it with an accredited clinical
laboratory (in the US a CLIA/CAP-certified test, ideally on a new sample). A
first run through these commands is a technical check, not a clinical result.

## Where the labels appear

- **VCF**: `INFO/WGSE_CONF` (`Number=A`: `high`, `moderate`, `low`, or `.` for an
  ALT the sample doesn't carry) and `INFO/WGSE_CONF_REASONS` (the reason codes
  for each ALT, joined by `|`). A `##wgsextract_confidence` header line records
  the scheme, caller and thresholds. Labelling an already-labelled file
  replaces the old labels.
- **TSV** beside the VCF (`clinvar_pathogenic.confidence.tsv`,
  `gnomad_af_lt_<AF>.confidence.tsv`, `significant_<type>_<name>.confidence.tsv`,
  `<name>.confidence.tsv` for PharmCAT): one row per carried call with
  `confidence`, `reasons`, `depth`, `genotype_quality`, `alt_fraction`, the
  zygosity and the relevant annotations (`CLNSIG`, `CLNDN`, `GENEINFO` and the split record's
  `OLD_MULTIALLELIC`, `GNOMAD_AF`, the
  PharmCAT `gene`, ...). Its `#` header lines state the thresholds and the
  disclaimer above.
- **Result manifest** ([result-manifest.md](result-manifest.md)): metrics
  `confidence_high`, `confidence_moderate`, `confidence_low` and
  `confidence_not_carried`; warnings `low_confidence_findings` and, for ClinVar
  pathogenic/likely pathogenic calls, `unconfirmed_clinical_findings`; and
  `details.confidence`, a list with one entry per labelled output: `scheme`
  (`wgsextract.confidence/1`), `role`, `output`, `sample`, `caller`,
  `thresholds`, `counts`, `not_carried`, `reasons` (counts per code),
  `findings` (the first 1000 calls with their labels and annotations;
  `findings_truncated` says whether there were more) and `disclaimer`.
- **Personal report** ([personal-report.md](personal-report.md)): `wgsextract
  report` lists ClinVar pathogenic/likely pathogenic calls in the ACMG SF genes
  with these labels, and labels PharmCAT genes by their weakest labelled call.

Only calls the sample carries are labelled. A ClinVar site where the sample is
homozygous reference (DeepVariant writes these as `RefCall` records with
genotype `0/0`) is counted as `not_carried` and is not a finding. Multi-sample
VCFs are judged by their first sample, with a `confidence_first_sample_only`
warning.

## Rules

Each carried ALT allele is judged on the evidence its caller wrote. The worst
rule wins: any `low` reason makes the call `low`; otherwise any `moderate`
reason makes it `moderate`; otherwise it is `high`.

| Evidence | Low | Moderate | Reason codes |
| --- | --- | --- | --- |
| FILTER | anything but `PASS` or `.` | | `filtered` |
| Read depth (FORMAT/DP, else the sum of AD, else INFO/DP) | below 10 | 10–19 | `low_depth`, `moderate_depth` |
| Genotype quality (FORMAT/GQ, else from PL) | below 20 | 20–29 | `low_gq`, `moderate_gq` |
| Alt read fraction, heterozygous call (AD, else DeepVariant VAF, Clair3 AF or DP4) | below 0.2 or above 0.8 | 0.2–0.3 or 0.7–0.8 | `allele_imbalance`, `skewed_allele_balance` |
| Alt read fraction, homozygous or haploid call | below 0.8 | 0.8–0.9 | `allele_imbalance`, `skewed_allele_balance` |
| Mapping quality (INFO/MQ) | below 40 for an RMS MQ (GATK, DRAGEN); below 20 for a mean MQ (bcftools) | | `low_mapq` |
| Strand bias (INFO/FS or SOR; else SOR from FORMAT/SB or INFO/DP4) | FS above 60 or SOR above 3 (indels: 200 and 10) | | `strand_bias` |
| Quality by depth (INFO/QD, GATK calls only) | below 2 | | `low_qd` |
| A measure missing from the VCF | no depth, GQ or allele counts at all (`no_call_metrics`) | any one missing | `depth_unknown`, `gq_unknown`, `allele_balance_unknown` |

Haploid calls (a man's chrX and chrY, mtDNA, `GT=1`) use half the depth
thresholds: there is no second allele to find.

Annotation splits a multiallelic call (such as `1/2`) into one record per
ALT, marked `INFO/OLD_MULTIALLELIC`, so each ALT meets its own ClinVar or
gnomAD entry. The split trims each record's PL to REF and that ALT, dropping
the called genotype, and copies site-wide DP4 and SB, whose alt reads belong to
every ALT. A split record's genotype quality therefore comes only from
FORMAT/GQ, and its alt read fraction only from AD with FORMAT/DP (or a per-ALT
VAF/AF); otherwise they are `gq_unknown` and `allele_balance_unknown`, which
cap it at `moderate` rather than wrongly making it `low`. On HG002 chr20, the
641 multiallelic bcftools calls were 98.4% correct; judged on the split
evidence, 523 of them would have been `low`. `wgsextract vcf snp` and
`vcf indel` write FORMAT/AD and FORMAT/DP, so their split calls keep their
allele balance.

Change the depth and genotype-quality thresholds for every run in
`config.toml`, or for one run on the command line:

```toml
confidence_min_depth = 10.0   # below: low (halved for haploid calls)
confidence_high_depth = 20.0  # below: moderate
confidence_min_gq = 20.0
confidence_high_gq = 30.0
```

```sh
wgsextract vcf clinvar --input s.vcf.gz --confidence-depth 8 15 --confidence-gq 20 30
```

## Why these thresholds

- **Depth 10 / 20.** A true heterozygous site shows each allele in about half
  its reads, binomially. The chance that a true heterozygote shows an alt
  fraction outside 0.2–0.8 is 22% with 6 reads, 2.1% with 10 and 0.26% with
  20, so below 10 reads a heterozygote is easily called homozygous or missed,
  and by 20 depth alone rarely misleads. Clinical laboratories set their own
  minimum depths during validation (Rehm et al. 2013; Roy et al. 2018); 20x per
  site is a common choice, and depth is one of the standard filters reviewed
  by Koboldt (2020). These thresholds are per site: a genome's mean depth says
  nothing about a given position.
- **GQ 20 / 30.** GQ is the Phred-scaled probability that the genotype is
  wrong: 20 means 1%, 30 means 0.1%. DeepVariant's GQ is reported to be well
  calibrated against GIAB truth (Poplin et al. 2018), so it can be taken at
  face value; bcftools and GATK GQs are model-based and usually optimistic,
  which the other rules partly compensate for. When GQ is absent it is derived
  from PL (the difference between the two most likely genotypes).
- **Allele balance.** Germline heterozygous calls should be near 0.5. An alt
  fraction outside 0.3–0.7 is worth a second look; outside 0.2–0.8 (where only
  0.26% of true heterozygotes at 20 reads fall) it suggests a mapping
  artifact, a paralogous region, contamination or a mosaic/somatic event
  rather than a clean germline call. Homozygous calls with many reference
  reads are likewise suspect.
- **Mapping quality, strand bias and QD** use the GATK germline hard-filter
  thresholds (MQ < 40, FS > 60, SOR > 3, QD < 2; for indels FS > 200,
  SOR > 10), which Broad derived from truth-set distributions. GATK's MQ is
  the root mean square of the reads' mapping qualities. bcftools instead
  writes their plain mean ("Average mapping quality" in its header), which
  sits far lower for the same reads, so 40 would flag ordinary calls: on
  HG002 chr20 the bcftools calls with MQ 35–39 were as accurate as the
  genome average. A mean MQ is therefore held to MAPQ 20, a 1% chance that a
  read is misplaced, below which calls were wrong 10% of the time. QD
  (QUAL divided by depth) is only judged on GATK calls: its threshold assumes
  GATK's QUAL. DRAGEN's machine-learning-recalibrated QUAL is on another
  scale (a clean 34x heterozygote scores QUAL 50, QD 1.5), and in one DRAGEN
  genome 52% of PASS calls had QD below 2. DRAGEN applies its own hard
  filters, which arrive through FILTER. When a caller
  writes no FS or SOR (bcftools), SOR is computed from its strand counts with
  GATK's formula. DeepVariant writes no strand or MQ annotations; its model
  sees them directly, so its labels rest on depth, GQ and allele balance.
- **FILTER.** A call the caller itself filtered (GATK `LowQual`, DeepVariant
  `RefCall`/`NoCall`, a VQSR tranche) is low confidence by definition.
- **Missing evidence is not good evidence.** A VCF without depth or genotype
  quality (for example one converted from a microarray or imputed) cannot earn
  `high`. Clinical guidance is that each laboratory must validate its own
  pipeline and quality thresholds and confirm reportable variants it can't
  vouch for (Rehm et al. 2013; Roy et al. 2018); calls with poor quality
  metrics are where false positives concentrate (Lincoln et al. 2019).

These rules cannot see systematic errors that look good in the reads (a
reference-genome error, a segmental duplication with confident but wrong
mapping) or variants a short-read genome misses altogether. GIAB's stratified
benchmarks show such errors are concentrated in low-complexity and segmental
duplication regions (Wagner et al. 2022).

## Validation

A good label should put few errors in `high` and many in `low`. Two call
sets were labelled with the default thresholds and every carried call scored
against an independent answer:

- **HG002 chr20, Illumina 35x, bcftools calls**, scored by `rtg vcfeval`
  against the GIAB v4.2.1 benchmark inside its confident regions (82,655 calls).
- **HG008 whole genome, Ultima, DeepVariant calls**, compared (allele and
  genotype class, after normalisation) with an orthogonal Illumina 169x call
  set (QUAL ≥ 30) inside GIAB's "not difficult" regions where both platforms
  have identical coverage (3.1 million calls). This truth set has its own
  errors, so its rates are disagreements, not proven errors.

| Call set | Label | Calls | Share of calls | Wrong / discordant | Share of all errors |
| --- | --- | ---: | ---: | ---: | ---: |
| HG002 chr20 (GIAB) | high | 76,231 | 92.2% | 0.29% | 17% |
| | moderate | 4,171 | 5.0% | 7.9% | 25% |
| | low | 2,253 | 2.7% | 34% | 58% |
| HG008 genome (orthogonal) | high | 2,570,100 | 82.8% | 0.12% | 9% |
| | moderate | 485,463 | 15.6% | 3.0% | 39% |
| | low | 49,629 | 1.6% | 39% | 53% |

At the 33,855 HG008 calls at ClinVar-listed sites, 0.06% of `high`, 1.9% of
`moderate` and 28% of `low` calls disagreed with the Illumina calls; on HG002
chr20 the 1,002 calls at ClinVar sites had 0.1%, 8% and 17%. A `low` call was
over 100 times as likely to be wrong as a `high` one, while `high` kept over
80% of the correct calls. Errors in `high` remain, as
the limits above say: a `high` label is not a confirmation.

## References

- Rehm HL, et al. ACMG clinical laboratory standards for next-generation
  sequencing. *Genet Med.* 2013;15(9):733–747. doi:10.1038/gim.2013.92
- Roy S, et al. Standards and guidelines for validating next-generation
  sequencing bioinformatics pipelines: a joint recommendation of the AMP and
  CAP. *J Mol Diagn.* 2018;20(1):4–27. doi:10.1016/j.jmoldx.2017.11.003
- Lincoln SE, et al. A rigorous interlaboratory examination of the need to
  confirm next-generation sequencing-detected variants with an orthogonal
  method in clinical genetic testing. *J Mol Diagn.* 2019;21(2):318–329.
  doi:10.1016/j.jmoldx.2018.10.009
- Koboldt DC. Best practices for variant calling in clinical sequencing.
  *Genome Med.* 2020;12:91. doi:10.1186/s13073-020-00791-w
- Poplin R, et al. A universal SNP and small-indel variant caller using deep
  neural networks. *Nat Biotechnol.* 2018;36:983–987. doi:10.1038/nbt.4235
- Wagner J, et al. Benchmarking challenging small variants with linked and
  long reads. *Cell Genom.* 2022;2(5):100128. doi:10.1016/j.xgen.2022.100128
- Broad Institute. Hard-filtering germline short variants (GATK
  documentation).
  <https://gatk.broadinstitute.org/hc/en-us/articles/360035890471>
