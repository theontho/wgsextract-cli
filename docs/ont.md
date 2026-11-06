# Oxford Nanopore

`align` and `realign` detect ONT from `@RG PL:ONT` or UUID read names in
BAM/CRAM/FASTQ (including gzipped FASTQ). Read groups take precedence; mixed
platforms are not assigned one platform. An explicit `--platform` overrides
detection. Unknown input still defaults to Illumina. SRA-renamed FASTQs without
platform metadata need `--platform ont`.
Only ONT gains new automatic alignment defaults. Revio/PacBio-, Ultima- and
MGI-named FASTQs, and `PL:PACBIO`/`PL:DNBSEQ` BAMs, retain the previous Illumina
alignment defaults unless `--platform` is explicit. The pre-existing
BAM/CRAM `PL:ULTIMA` automatic path is unchanged. Other detected platforms
may be reported, but reporting does not select their alignment workflows.

CPU ONT alignment uses minimap2 `map-ont`, carries `MM`, `ML` and `MN` through
`samtools fastq -T MM,ML,MN` and `minimap2 -y`, and soft-clips supplementary
alignments (`-Y`) so the modification coordinates still describe the full read.
Sorting retains these tags.
FASTQ input first passes through `samtools import -T MM,ML,MN`, so ordinary
ONT `runid`/channel comments do not become invalid SAM fields with `-y`.
Realignment streams single-end tagged FASTQ instead
of reusing potentially untagged paired FASTQs. `--keep-fastq` and GPU ONT
alignment are refused rather than silently losing modification data; for a
retained intermediate use `samtools fastq -T MM,ML,MN` separately.

For CRAM realignment, `--input-ref` supplies the original decoding FASTA for
both platform detection and extraction; `--ref` selects the alignment target.
Without `--input-ref`, a CRAM lacking platform read groups is decoded with
`--ref` when its sequence names, lengths and MD5 checksums match the CRAM's
(realigning to the build it was encoded with), and otherwise with the reference-library FASTA whose
signature matches, **before** its read names are probed. A failed CRAM read-name probe
is an error, never a silent fallback to paired short-read extraction.
The usual realignment disk-space preflight also applies to streamed ONT reads.

## Small variants

`vcf deepvariant` automatically selects `ONT_R104` for detected ONT input.
`--ont` is an alias for `--model-type ONT_R104`; explicit model selectors
override detection. Contradictory ONT selectors are rejected.

```sh
wgsextract vcf deepvariant -i hg002-ont.bam --ref chr20.fa \
  --region chr20:10000000-10010000 --threads 1 --outdir ont-variants
```

This model supports **R10.4.1 only**. R9.4.1/R9.4 data is unsupported; the CLI
warns when `@PG` or `@RG DS` basecaller metadata identifies it. UUID names
cannot identify chemistry, so absence of a warning does not establish support.
Use Google's installed `google/deepvariant:1.10.0` image (the default), or an
installed `run_deepvariant` that provides this model. Dependencies are checked,
never installed automatically.

`ONT_R104` requires x86-64. Linux ARM64 remains WGS-only, and ONT calling is
also refused on Apple Silicon rather than assuming emulation is qualified.
Other models retain their existing platform policy. The pinned Parabricks
4.8.0-1 supports `deepvariant --mode ont`, selected by `--gpu`; this command
mapping is tested, but GPU ONT accuracy has not been hardware-qualified here.
CPU container temporary files and HOME stay inside the owned output work
directory and are removed after the run.

### Clair3 (optional)

`vcf clair3` runs [Clair3](https://github.com/HKU-BAL/Clair3) 2.x, an
ONT-trained small-variant caller. It is optional, and DeepVariant remains the
default ONT caller. Clair3 is qualified on **Linux x86-64 only**. It is not
available on Windows (use WSL2), and Bioconda's macOS and ARM builds are
refused because they have not been validated here. Install it into its own
Pixi environment with `pixi install -e clair3`. The CLI checks for it but
never installs it.

```sh
wgsextract vcf clair3 -i hg002-ont.bam --ref chr20.fa \
  --region chr20:10000000-11000000 --threads 2 --outdir ont-clair3 \
  --model r1041_e82_400bps_sup_v500
```

The model must match the basecaller that produced the reads. It is chosen in
this order:

1. `--model NAME|DIR`;
2. `clair3_model` in `config.toml`, for example
   `clair3_model = "/data/clair3-models/r1041_e82_400bps_sup_v420"`;
3. the Dorado/Guppy basecaller model named in the `@RG` (`DS:basecall_model=`,
   ID) or `@PG` header lines. For example, `dna_r10.4.1_e8.2_400bps_sup@v5.0.0`
   selects `r1041_e82_400bps_sup_v500`, R10.4 E8.1 selects `r104_e81_*_g5015`,
   and R9.4.1 selects `r941_prom_sup_g5014`.

If the header names no basecaller (common after realignment, or for older
public data), or names several, the command stops and asks for `--model`. A
model for another pore, kit or speed than the metadata states is refused. A
different accuracy (hac/sup) or basecaller version is allowed with a warning.
HiFi and Illumina models are refused, as are `*_with_mv` models, which need
Dorado move tables and `--enable_dwell_time`.

A `NAME` is one of the models Bioconda bundles in the environment
(`.pixi/envs/clair3/bin/models`; the error lists them). Download others
(for example Dorado v4.2/v4.3 models) from HKU's PyTorch catalogs, keeping
the directory name:

```sh
m=r1041_e82_400bps_sup_v420
mkdir -p clair3-models/$m
for f in pileup.pt full_alignment.pt; do
  curl -fL -o clair3-models/$m/$f \
    https://www.bio8.cs.hku.hk/clair3/clair3_models_rerio_pytorch/$m/$f
done
```

Then set `clair3_model` in `config.toml` to that directory, or pass it with
`--model`.

Bundled models are in `clair3_models_pytorch/`, Rerio-derived models in
`clair3_models_rerio_pytorch/`. Clair3 1.x TensorFlow models do not work with
Clair3 2.x. Before calling, the CLI checks both files against the SHA-256
digests pinned in `src/wgsextract_cli/assets/clair3/models.json`, which
covers 31 ONT models from both catalogs. A mismatch is an error. A model
without a pin, such as a custom one, runs with a warning.

On a 1 Mb slice (chr20:10-11 Mb) of public HG002 R10.4.1 sup reads (ONT's
GIAB 2023.05 release, which records `basecaller_cfg
dna_r10.4.1_e8.2_400bps_sup@v4.0.0`), realigned with `realign` and scored with
`rtg vcfeval` against GIAB v4.2.1 inside its confident regions (1,355 SNVs and
234 indels):

| Caller | SNV F1 | Indel F1 | Overall F1 |
| --- | --- | --- | --- |
| `vcf deepvariant` (`ONT_R104`) | 0.999 | 0.875 | 0.982 |
| `vcf clair3 --model r1041_e82_400bps_sup_v400` (matching model) | 0.997 | 0.862 | 0.979 |
| `vcf clair3 --model r1041_e82_400bps_sup_v410` (bundled) | 0.998 | 0.891 | 0.984 |
| `vcf clair3 --model r1041_e82_400bps_sup_v420` | 0.998 | 0.839 | 0.977 |

The callers are within a few indels of each other on this slice, so
DeepVariant stays the default. Results on all of chr20 are under
[Validation on GIAB HG002](#validation-on-giab-hg002).

## Structural variants

`vcf sv` runs Sniffles2 for detected ONT input; `--ont` requests the same
explicitly, and the platform-neutral `--long-read` selects Sniffles for any
long-read data. pbsv is PacBio-only, so it is refused for ONT (detected or
`--ont`), whether chosen by `--caller pbsv` or by `--pacbio`'s pbsv default. `--ont`/`--long-read` with `--caller delly`, and `--ont`
with `--pacbio`, are rejected; an explicit `--caller delly` on detected ONT runs
but warns that Delly is a short-read caller. PacBio behavior is unchanged:
`--pacbio` still prefers pbsv (falling back to Sniffles) with CCS thresholds,
and PacBio input without flags still uses Delly.

```sh
wgsextract vcf sv -i hg002-ont.bam --ref chr20.fa \
  --region chr20:10000000-11000000 --threads 1 --outdir ont-svs
```

Sniffles2 runs with its defaults, which are tuned for both ONT and HiFi.
`--tandem-repeats BED` is forwarded to Sniffles (as well as pbsv). On HG002
chr20, though, Sniffles' own GRCh38 TRF annotation *lowered* agreement with the
GIAB v5.0q benchmark (F1 0.861 without it, 0.797 with it; see
[validation](#validation-on-giab-hg002)), so it is not passed by default.
Mosaic mode (`--mosaic`) is not used: this command calls germline SVs. The VCF
sample is the reads' `@RG SM` when they name one sample (Sniffles itself would
write `SAMPLE`).

Without `--region`, Sniffles skips contigs shorter than 1 Mb (chrM, unplaced and
decoy contigs), so they get no SV calls. A `--region` on such a contig is
called. Install Sniffles (for example via the `pacbio` Pixi environment); it is
checked, not installed automatically.

## Methylation

`methylation pileup` turns the MM/ML base-modification tags that Dorado writes
(with a modified-base model such as `5mCG_5hmCG`) into a bedMethyl table with
ONT's [modkit](https://github.com/nanoporetech/modkit) `pileup`. modkit is
optional. Install it with `pixi install -e modkit`; the CLI checks for it but
never installs it. Bioconda builds modkit for Linux and macOS only, so the
command is refused on native Windows; use WSL2 there.

```sh
wgsextract methylation pileup -i hg002-ont.bam --ref chr20.fa \
  --region chr20:10000000-11000000 --threads 2 --outdir ont-methylation
```

It writes `<input>.cpg.bedmethyl.gz` (BGZF, tabix-indexed) and
`<input>.cpg.methylation.tsv`, and logs the summary. With `--format parquet`
(needs the optional `pyarrow`) the summary is `<input>.cpg.methylation.parquet`
and every site is also saved, typed, in `<input>.cpg.bedmethyl.parquet`; see
[Parquet output](parquet.md). Per modification, the
summary lists sites, sites with at least 5 valid calls, median coverage, the
weighted fraction modified (all calls pooled), the mean per-site level, and the
shares of sites at 80% or more and at 20% or less.

| Setting | CLI | `config.toml` | Default |
| --- | --- | --- | --- |
| Context | `--context cpg\|all` | `methylation_context` | `cpg` |
| 5hmC as well as 5mC | `--5hmc` / `--no-5hmc` | `methylation_5hmc` | off |
| Sum both strands of each CpG | `--combine-strands` / `--no-combine-strands` | `methylation_combine_strands` | on |

`--region` takes 1-based, inclusive regions (several separated by commas), and
`--threads` sets modkit's threads. Strands can only be summed at palindromic
motifs, so `--context all` turns combining off, and asking for both is an error.

Before modkit runs, the CLI samples 200 primary reads. If none carries MM/ML
tags it stops and explains why: the basecaller had no modified-base model, or
the reads were realigned through FASTQ without keeping the tags. `wgsextract
realign` keeps them. A partly tagged input only warns. Short-read input
(Illumina, Ultima, DNBSEQ) is refused, because bisulfite and EM-seq data need a
conversion-aware aligner. Input whose platform cannot be told runs with a
warning, as long as it carries the tags.

modkit 0.6 skips every read whose MM tag has an opposite-strand entry (such as
`G-h?` or `T-a.`), so the CLI counts them while sampling. When every tagged read
has one, it refuses with the cause. Otherwise it warns that coverage comes from
the remaining reads. For Nanopore these are Dorado duplex reads (`dx:i:1`):
use the simplex reads. PacBio HiFi 5mC with only `C+m?` entries, from primrose or
ccs-kinetics, is accepted. Current Jasmine also calls 5hmC (`C+h?`/`G-h?`) and
6mA (`A+a.`/`T-a.`) by default, so its reads are refused until Jasmine is re-run
with only its 5mC model. For the same reason, `--5hmc` warns on PacBio input.

On the same HG002 R10.4.1 chr20:10-11 Mb slice (ONT open data `PAO83395`,
about 54x; 8,108 strand-combined CpGs with at least 5 calls):

| Check | Result |
| --- | --- |
| All CpGs | 66.7% methylated |
| CpGs outside CpG islands and promoters | 72.6% |
| CpGs in the slice's 5 UCSC CpG islands | 1.4% (each island 0.1-8.5%) |
| CpGs within 1 kb of RefSeq TSSs | 14.2% |
| 5hmC (`--5hmc`) | 1.0% |
| Per-site agreement with ONT's published HG002 60x CpG calls | Pearson 0.98, Spearman 0.94, median difference 2.9 points |

The same reads realigned with `wgsextract realign` gave the same result, so the
tags survive realignment.

Platform detection, tagged CPU alignment/realignment, x86-64 DeepVariant
ONT_R104 calling, optional Linux x86-64 Clair3 calling, Sniffles SV calling,
and optional modkit CpG methylation are supported.

## Adaptive sampling targets

`panel bed` turns gene names and curated presets into a BED for MinKNOW's
adaptive sampling (or any targeted analysis), and `info calculate-coverage --by`
reports how well a run covered it.

```bash
wgsextract panel bed --list-presets
wgsextract panel bed --preset pharmacogenes --preset hla --ref hg38.fa
wgsextract panel bed --genes BRCA1,BRCA2 --gene-file my_genes.txt --pad-kb 50
wgsextract info calculate-coverage --input run.bam --by panel_pharmacogenes_hla_hg38_pad100kb.bed
```

- **Genes.** Names are matched without regard to case against the bundled
  NCBI RefSeq annotations (public domain) for hg38 (GRCh38.p14, release
  RS_2025_08), T2T (CHM13v2.0, RS_2025_08) and hg19 (GRCh37.p13, RS_2024_09).
  A name that is a unique alias of one gene resolves to it with a warning.
  Every unknown or ambiguous name is reported at once, with suggestions, and
  nothing is written.
- **Presets.** `pharmacogenes` (PharmCAT's genes), `hla` (the classical MHC,
  GABBR1 to KIFC1), `autoimmune` (Cotsapas 2011's shared autoimmune loci plus the
  MHC), `acmg-sf` (ACMG SF v3.2's 81 genes) and `cardiac` (its cardiovascular
  genes). `--list-presets` prints each source and gene list.
- **Regions.** Each gene's span is padded by `--pad-kb` (default 100 kb each
  side: adaptive sampling decides on a read's first few hundred bases, so
  reads that start in the flank reach the gene), clipped to the contig, and
  overlapping or touching regions are merged. The BED has four columns:
  contig, start, end and the genes in the region. No strand is given, so both
  strands are targeted.
- **Build and names.** `--ref` (or the configured reference) sets the build
  and the contig names, so `chr1`/`1` and `chrM`/`MT` match the FASTA MinKNOW
  aligns to. A reference of another build than `--build`, or with other contig
  lengths, is refused. A chrM that isn't rCRS (UCSC hg19's 16,571 bp) is
  targeted whole, with a warning. Without a reference the BED uses `chr` names
  of `--build` (default hg38).
- **Size.** ONT recommends targets of no more than 5% of the genome, best
  around 1%, for the best enrichment. Each run reports the fraction and warns
  above 5%. At 100 kb padding on hg38, `pharmacogenes` is 0.21%, `hla` 0.13%,
  `autoimmune` 0.43%, `acmg-sf` 0.73% and `cardiac` 0.39%.

`--by BED` replaces the per-chromosome report with one row per target: mean
and median depth, the fraction of bases at 10x, 20x, 30x and `--min-depth`
(default 30), and whether the mean is below `--min-depth`. A summary gives the
on-target and off-target mean depth and their ratio, the enrichment. Overlapping
targets are each measured in full, but counted once on target; the mitochondrial
contig counts as neither on nor off target unless the BED names it. Depth
follows `samtools depth`'s defaults, like `--bins`: deletions and duplicates
don't count, overlapping mates do. BED contig names may use `chr1` or `1`
style either way.

## Validation on GIAB HG002

ONT's public HG002 R10.4.1 run (`PAO83395`, giab_2023.05, sup v4.0.0 with
5mCG/5hmCG, about 51x by `samtools coverage`, CC BY 4.0) was put through the CLI. The steps were:

- `extract` a region from ONT's alignment;
- `realign` it to hs38 with the ONT preset;
- call variants with `vcf deepvariant`, `vcf clair3` and `vcf sv`;
- score the calls against GIAB.

Small variants were scored with hap.py (vcfeval engine) against v4.2.1 inside
its benchmark regions. SVs were scored with `truvari bench --pick ac --refine`
against v5.0q stvar inside its benchmark regions (the whole-genome run
was refined with `truvari refine -a mafft`, because POA alignment of 48 kb
regions exceeded 16 GB). The machine was a shared
8-core x86-64 Linux host, and each step ran with 4 threads.

| Region | Caller | SNV recall / precision / F1 | Indel recall / precision / F1 |
| --- | --- | --- | --- |
| chr20:10-15 Mb | `vcf deepvariant` | 0.9995 / 0.9996 / 0.9995 | 0.895 / 0.884 / 0.889 |
| chr20:10-15 Mb | `vcf clair3` (v410) | 0.9960 / 0.9991 / 0.9975 | 0.861 / 0.935 / 0.896 |
| chr20 (64 Mb) | `vcf deepvariant` | 0.9991 / 0.9994 / 0.9992 | 0.868 / 0.850 / 0.859 |
| chr20 (64 Mb) | `vcf clair3` (v410) | 0.9981 / 0.9991 / 0.9986 | 0.818 / 0.926 / 0.868 |

The 5 Mb slice has 6,037 truth SNVs and 1,018 truth indels; all of chr20 has
71,333 SNVs and 11,256 indels. Indel accuracy is limited by ONT homopolymer
errors, as is expected for R10.4.1 sup v4.0.0 at this depth.

| Region | `vcf sv` (Sniffles 2.8.1) | Truth SVs | Recall | Precision | F1 |
| --- | --- | --- | --- | --- | --- |
| chr20:10-15 Mb | defaults | 27 | 0.704 | 1.000 | 0.826 |
| chr20 | defaults | 802 | 0.783 | 0.956 | 0.861 |
| chr20 | `--tandem-repeats` (Sniffles TRF BED) | 802 | 0.708 | 0.912 | 0.797 |
| chr1-22, X, Y | defaults | 28,123 | 0.758 | 0.939 | 0.839 |

Before refinement, 193 of the 202 SVs missed on chr20 lay in tandem repeats.
190 were heterozygous, and 143 were insertions. The v5.0q benchmark includes
such repeat SVs; older benchmarks largely excluded them.

Time and peak memory on chr20 (278,216 reads), 4 threads:

| Step | Wall time | Peak RSS |
| --- | --- | --- |
| `extract --region chr20`, streamed over HTTPS from ONT's 100 GB CRAM | 3.7 min | 1.7 GB |
| `realign` to full hs38 (`--memory 512M`) | 40 min | 14.8 GB |
| `vcf deepvariant` (CPU container) | 31 min | 9.8 GB |
| `vcf clair3` | 10.5 min | 4.7 GB |
| `vcf sv` | 70 s | 0.7 GB |

`realign`'s peak is mostly minimap2's hs38 `map-ont` index. With the default
sort memory (about 1 GB per thread on a 30 GB machine) it exceeded a 14 GB
limit and was killed, so plan for at least 16 GB of RAM for ONT alignment.
`align` and `realign` now warn before minimap2 starts when its estimate does
not fit, and suggest `--threads` and `--memory` that do; see
[resource estimates](resource_estimates.md#minimap2-long-reads).
Scaled from chr20 (2.1% of the genome), a 51x whole genome on 4 threads would
take about 32 h to realign, 25 h for DeepVariant and 8 h for Clair3. Whole-genome
small-variant accuracy was therefore measured on chr20 only.
The read-only features were run on ONT's whole-genome CRAM (PAO83395, 104 GB,
about 51x, GRCh38 analysis set), without realigning it:

| Command | Result on HG002 | Wall time | Peak RSS |
| --- | --- | --- | --- |
| `info --detailed` | Oxford Nanopore, male, hg38, 18,778 bp mean read length | 95 s | 0.5 GB |
| `lineage y-haplogroup` | J-ZS2721, inside J1-M267 and two levels below YFull's J-ZS2729 for HG002 | 3.5 min | 0.8 GB |
| `lineage mt-haplogroup` | H5a7 (Haplogrep quality 0.95) | 12 s | 0.2 GB |
| `microarray --formats 23andme_v5`, 2 threads | 638,466 markers, 0.17% no-calls; 99.82% genotype concordance with v4.2.1 at 2.26 M benchmark sites (99.53% where the truth has a variant) | 31 min | 1.7 GB |
| `vcf sv`, 4 threads | 25,672 SVs; recall 0.758, precision 0.939 against 28,123 v5.0q SVs (table above) | 24 min | 5.6 GB |
| `methylation --region chr20` | 65.5% of 746,117 CpGs methylated (>= 5 calls) | 43 s | 1.7 GB |

`info --detailed` estimates short-read depth as mapped records x mean read
length. For long reads it instead samples 1 kb windows with `samtools depth`
([#427](https://github.com/WGSExtract/wgsextract-cli/issues/427)). Index
record counts include secondary and supplementary alignments, and an ONT
supplementary record carries the whole read. So on a chr20+chrY slice of that
CRAM the record estimate gave 85x for chr20 and 185x for chrY, while sampling
gives 56x and 28x. `samtools coverage` gives 53x over chr20's non-N bases.
chrY's typical depth is about 26x outside 1 Mb of satellite at up to 8,000x.
Unplaced, alt and decoy contigs under 1 Mb share one pooled sample, which
sets the Other row. Sampling needs the BAM/CRAM index: without one, `info`
warns, falls back to the record estimate and caches it until an index appears.
Unaligned reads have no depth to sample, so it is cached as zero.
`qc vcf` ran on the chr20 DeepVariant VCF (106,665 PASS SNVs, Ts/Tv 1.74).

Small-variant calling on the whole genome was not run: 32 h of realignment
plus 25 h of DeepVariant on 4 shared cores would not show anything that chr20
does not, since the callers work per region. The whole-genome run instead
covers what chr20 cannot: sex and haplogroup inference, the genome-wide
microarray export and SV calling across all chromosomes; a whole-genome
small-variant run is tracked in
[#431](https://github.com/WGSExtract/wgsextract-cli/issues/431).

### Data and CI fixture

- `tests/fixtures/ont_hg002/` (about 270 KB) holds 44 HG002 reads with their
  MM/ML tags, covering chr20:14,542,001-14,554,000 at about 27x, with that
  reference window and the truth calls in it: 18 small variants and one
  313 bp deletion. `tests/test_ont_hg002.py` aligns the reads with `align`,
  then checks:
  - platform detection, read groups and tag retention;
  - Sniffles finding the deletion;
  - DeepVariant choosing `ONT_R104`;
  - Clair3 calling exactly the truth variants, when Clair3 is installed.

  CI runs it in the `ont-hg002` job. `provenance.json` records the sources;
  `python scripts/ont_hg002_data.py fixture` rebuilds the fixture.
- The 5 Mb slice and whole chr20 (in two parts under GitHub's 2 GiB limit;
  `samtools cat` joins them) are assets of the
  [`ont-hg002-data-v1`](https://github.com/WGSExtract/wgsextract-cli/releases/tag/ont-hg002-data-v1)
  release. [`docs/data/ont_hg002_manifest.json`](data/ont_hg002_manifest.json)
  lists them, and the GIAB truth files, with SHA-256 sums.
  `python scripts/ont_hg002_data.py fetch [NAME ...]` downloads and verifies
  them.

## CLI / GUI contract

No new option is needed for detection. Leave `--platform` unset for automatic
selection; explicitly sending `--platform illumina` disables it. The CLI logs
the detected platform. `info` recognizes `PL:ONT` even when read names were
renamed. BAM/CRAM variant callers share the same cached, file-revision-aware
platform resolver; detection alone does not qualify short-read callers for ONT.
GUI integrations should allow automatic platform selection and surface the
tag-preservation limitations above.
Expose `ONT_R104` and `--ont` for DeepVariant, preserve an unset model for
automatic selection, and show its chemistry and architecture restrictions.
For `vcf clair3`, expose `--model` (leave it unset for config or metadata
selection) and hide the command on Windows. For `vcf sv`, expose `--ont`/`--long-read`, leave `--caller` unset for
automatic Sniffles selection, and do not offer pbsv for ONT input.
For `methylation pileup`, leave `--context`, `--5hmc` and `--combine-strands`
unset so `config.toml` applies, hide the command on Windows, and show its
bedMethyl and summary outputs.
The GUI repository is separate; these CLI changes do not update it.
