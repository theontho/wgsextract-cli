# Low-coverage and imputed inputs

Genotype outputs are only as good as the reads behind them. A consumer
"low-pass" genome (often 0.5–5x) cannot see both alleles at most heterozygous
sites, and the provider usually fills the gaps by imputing genotypes from a
population panel. Such files convert without errors, but their microarray kits
and haplogroups can be wrong, and some analysis services reject them. WGS
Extract warns about this; it never changes, blocks or deletes outputs.

## Before: mean depth

Before `microarray`, the `vcf` callers (`snp`, `indel`, `freebayes`, `gatk`,
`deepvariant`, `sv`, `cnv`) and the haplogroup commands (`lineage
y-haplogroup`, `lineage mt-haplogroup`), a BAM/CRAM input's mean depth is
checked for the targets the command uses: the autosomes for whole-genome runs,
or chrX, chrY or mtDNA when `--region` names them, chrY for `y-haplogroup` and
mtDNA for `mt-haplogroup`.

| Target | Default warning threshold | Why |
| --- | --- | --- |
| Autosomes, a woman's chrX | below 8x (`qc_min_diploid_depth`) | Both alleles of a heterozygous site need several reads each; below about 8x many are called homozygous or missed. |
| chrY, mtDNA, a man's chrX | below 3x (`qc_min_haploid_depth`) | One allele per site needs fewer reads, but below about 3x haplogroups often stop at an upstream branch. |

mtDNA has hundreds of copies per cell, so even a 1x genome usually has more
than enough mtDNA depth for `mt-haplogroup`; chrY in a low-pass genome usually
does not.

The check reads nothing new when it can avoid it, using in order:

1. the metrics cached by `wgsextract info --detailed` for this very file;
2. a CRAM's container headers, which record the bases in each container (about
   10 seconds for a 30x CRAM on an SSD; no decoding or index is needed);
3. a BAM's index (`samtools idxstats`), with the read length from a 20,000-read
   sample.

Depth counts every mapped base, including duplicates, over the reference's
non-N length, like `info`. When none of these sources is available (an
unindexed BAM, or a CRAM whose header doesn't say which contigs hold its
reads), the check is skipped with an informational message.

## After: genotype rates

After `microarray` writes its files, the autosomal genotypes are checked:

- **Homozygosity of the CombinedKit's called genotypes.** Directly sequenced
  genomes give about 70–80%. Above 90% (`qc_max_autosomal_homozygosity`)
  usually means a low-pass or heavily imputed input; below 60%
  (`qc_min_autosomal_homozygosity`) suggests a contaminated or mixed sample.
  The heterozygous/homozygous ratio is logged with it.
- **No-call (`--`) rate of each vendor file.** Above 10%
  (`qc_max_no_call_rate`) means the input lacks reads or calls at many chip
  positions. This is skipped for `--region` runs, which leave most chip
  positions uncalled by design.

Both need at least 1,000 sites to say anything.

## Settings

Thresholds are `config.toml` settings:

```toml
qc_min_diploid_depth = 8.0         # 0 disables
qc_min_haploid_depth = 3.0         # 0 disables
qc_min_autosomal_homozygosity = 0.6
qc_max_autosomal_homozygosity = 0.9
qc_max_no_call_rate = 0.1
```

`--qc-min-depth DEPTH` overrides both depth thresholds for one run of a
command above; `--qc-min-depth 0` turns the depth check off.

## What to do about a warning

- Run `wgsextract info --detailed` for per-chromosome depth and breadth.
- For a low-pass file, use the provider's imputed VCF knowingly: imputed
  genotypes are population guesses, so rare variants and close-relative
  matching are unreliable, and uploads may be rejected.
- Results on chrY or mtDNA at low depth may still be correct to an upstream
  branch; treat terminal subclades as unconfirmed.
