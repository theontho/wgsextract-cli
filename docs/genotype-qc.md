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
| Autosomes, a woman's chrX | below 15x (`qc_min_diploid_depth`) | Both alleles of a heterozygous site need several reads each. In a [downsampled 1000 Genomes genome](microarray-benchmarks.md#accuracy-versus-coverage), `microarray` called 4% of heterozygous chip sites homozygous at 15x, 13% at 10x and 21% at 8x. |
| chrY, mtDNA, a man's chrX | below 3x (`qc_min_haploid_depth`) | One allele per site needs fewer reads, but below about 3x haplogroups often stop at an upstream branch. |

mtDNA has hundreds of copies per cell, so even a 1x genome usually has more
than enough mtDNA depth for `mt-haplogroup`; chrY in a low-pass genome usually
does not.

The check reads nothing new when it can avoid it, using in order:

1. the metrics cached by `wgsextract info --detailed` for this very file;
2. a CRAM's container headers, which record the bases in each container (about
   10 seconds for a 30x CRAM on an SSD; no decoding is needed). Contigs that
   htslib packs together into multi-reference containers are attributed from
   the `.crai` index, or left unknown without one. These totals also include
   unmapped mates placed beside their mapped partners (0.2% of the reads in
   a public HG002 NovaSeq genome), so the depth can be slightly high;
3. a BAM's index (`samtools idxstats`), with the read length from a 20,000-read
   sample.

Long reads (a mean read length above 410 bp) are measured instead: index and
container counts include secondary and supplementary records, and an ONT
supplementary record carries the whole read, so record counts times the read
length overstated HG002 ONT chrY 185x for 24x
([#427](https://github.com/WGSExtract/wgsextract-cli/issues/427)). The depth
is then `samtools depth` over about 1,000 evenly spaced 1 kb windows of an
indexed BAM or CRAM, a few seconds. Sampled windows show typical depth: they
mostly miss short collapsed-repeat pileups, such as HG002's 1 Mb of chrY
satellite at up to 8,000x, which holds half of chrY's aligned bases.

Depth counts every mapped base, including duplicates, over the reference's
non-N length, like `info`. When none of these sources is available (an
unindexed BAM, or an unindexed CRAM with multi-reference containers), the
check is skipped with an informational message.

That whole-input depth is spread over every contig that has reads, so a slice
(an `extract` output, a region-limited realign or a panel) looks shallow even
when its reads are deep. Before warning, a target found low is measured again
with `samtools bedcov` over about 200 evenly spaced 1 kb windows on the
contigs with reads, dividing by the bases those windows have reads on; when
none of them has any, every read on those contigs is counted. This measure
leaves out duplicates, secondary and QC-failed reads.

With `--region`, `microarray` and the `vcf` callers measure the region itself
this way instead, dividing by every base of the sampled windows, so a 54x
slice called over its own region is not warned about, a region half outside
the reads reports about half the depth, and one outside them reports 0x.
Region contigs match the input's names either way (`20` or `chr20`), and
`chr20:1,500,000-1,600,000` is one region. Whether chrX is haploid is still
decided from the whole input, since a region's chrY may have no reads. A CRAM
is measured only when its reference FASTA is at hand.

A genuinely low-pass genome still warns, though only bases with reads count,
so the depth shown is somewhat high: a 2% subsample (about 1x) of a 52x ONT
HG002 chr20 is reported as about 1.5x, and very sparse inputs approach 1x
however few reads they have.

## After: genotype rates

After `microarray` writes its files, the autosomal genotypes are checked:

- **Homozygosity of the CombinedKit's called genotypes.** Directly sequenced
  genomes give about 70–80%; one downsampled genome gave 78% at 30x, 88% at
  5x and 94% at 3x. Above 88% (`qc_max_autosomal_homozygosity`) usually means a low-pass or
  heavily imputed input; below 60%
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
qc_min_diploid_depth = 15.0        # 0 disables
qc_min_haploid_depth = 3.0         # 0 disables
qc_min_autosomal_homozygosity = 0.6
qc_max_autosomal_homozygosity = 0.88
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
