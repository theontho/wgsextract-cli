# Microarray benchmarks

The weekly remote-resource test generates all **21 consumer layouts** and a
CombinedKit from public HG00096 GRCh38 and native CHM13 WGS, then compares them
with that sample's independently measured Illumina Omni 2.5 chip. It also scores
against HG00097 as a different-sample negative control. This is not a comparison
of two generated kits, nor a validation of every marker or chromosome.
Both regional CLI runs explicitly use two threads.

- WGS: [1000 Genomes NYGC HG00096 30x GRCh38 CRAM](https://ftp.sra.ebi.ac.uk/vol1/run/ERR324/ERR3240114/HG00096.final.cram),
  `chr20:10000000-12000000`.
- Reference: [GRCh38 full analysis set](https://ftp.1000genomes.ebi.ac.uk/vol1/ftp/technical/reference/GRCh38_reference_genome/GRCh38_full_analysis_set_plus_decoy_hla.fa).
- Chip: [1000 Genomes Omni release 20140818](https://ftp.1000genomes.ebi.ac.uk/vol1/ftp/release/20130502/supporting/hd_genotype_chip/ALL.chip.omni_broad_sanger_combined.20140818.snps.genotypes.vcf.gz),
  GRCh37 `20:9900000-12100000`.
- Native T2T WGS: [public HG00096 CHM13 CRAM](https://anvilproject.s3.amazonaws.com/file/71d42a36eb86dbf3b0bcca7fe02eb864.md5),
  native `chr20:10000000-12000000`, not a rebased alignment.
- T2T reference: [CHM13v2.0 masked-Y/original-MT FASTA](https://s3-us-west-2.amazonaws.com/human-pangenomics/T2T/CHM13/assemblies/analysis_set/chm13v2.0_maskedY.fa.gz),
  SHA256 `36b83c984efe3db0ad05065e875e72fdcc56ade32b5c61cbffc14698453125a3`.
  The test downloads the complete reference (~980 MB), verifies this checksum,
  builds indexes locally, and preserves every primary SQ signature when slicing
  the remotely indexed CRAM. It does not claim to checksum the complete CRAM.
  The existing reference downloader also provisions the standard hg18-to-hg19
  chain needed to include NCBI36 vendor probes in the pinned marker union.

For regional WGS, chip loci must independently lift uniquely and forward into
the actual extracted GRCh38 interval before scoring. The wider GRCh37 query
margin is not part of the call-rate denominator; eligibility is never selected
from which genotypes the generated kit happened to call.
For native T2T, eligibility instead requires a mapped GRCh37 source position
in the pinned reciprocal site map, inside the actual native chr20 interval.
Neither alleles nor generated calls select eligible truth. This validates chip
genotypes, not independently the coordinate map itself.

The allele-exact scorer matches marker identity and chromosome, **not position
across different builds**. It sorts alleles but never guesses strand,
complements alleles, remaps rsIDs, or interprets I/D probes. Conflicting duplicate
identities are permanently quarantined. Single-letter and doubled homozygotes
are equivalent only on X/Y/MT. TSV, quoted/unquoted CSV and Ancestry's two allele
columns are supported, including gzip input.
Real unplaced/position-zero probes are retained as no-calls.

Reports include per-chromosome overlap, truth no-calls, chromosome mismatches,
eligible truth calls, called/concordant/discordant/no-call counts, call rate and
called-only concordance. Call rate is conditional on the truth/kit marker-ID
overlap with matching chromosomes; no-call rows stay in that denominator.
Markers absent from the kit cannot be assigned to its panel without an expected
panel manifest. This is not a complete panel-coverage metric; truth and kit marker
totals and chromosome mismatches are reported separately. The regional
gate requires at least 20 called overlapping markers per layout (200 CombinedKit,
100 23andMe V5), call rate >=90%, concordance >=98%, and different-sample
concordance <90%, with no chromosome mismatches. This is a SNP benchmark,
not evidence for I/D accuracy.

CI uploads **only aggregate `chip-concordance.json`**, never genomic records.

## Score an approved real chip/export

Run from the repository root in the existing development environment:

```sh
pixi run python scripts/benchmark_microarray_chip.py \
  --truth out/approved-chip.txt --truth-format Ancestry_V2_2024 \
  --kit 23andMe_V5=out/generated/sample_23andMe_V5.txt \
  --kit MyHeritage_GSA_2025=out/generated/sample_MyHeritage_GSA_2025.csv \
  --output out/chip-concordance.json
```

Use the registered canonical format name, or `CombinedKit` for four-column
`rsID chromosome position genotype` TSV, including an independent chip VCF query:
`bcftools query -s SAMPLE -f '%ID\t%CHROM\t%POS[\t%TGT]\n' chip.vcf.gz`.
Publish aggregate counts only. Never commit donated exports, genotypes, personal
names, kit IDs or identifying source paths. An actual paired chip and WGS is
necessary to claim individual concordance.

## Serial versus parallel

With `--threads` above 1, chromosomes are called in parallel workers by default;
`--no-parallel` calls them in one stream. Parallel calls are exactly those of one
stream: bcftools subsamples a site with more than 255 bases using a random
stream seeded once per process, and only such deep sites draw from it. Each
worker's output records its last site with INFO/DP above 255; when more than one
chromosome has one, those chromosomes are recalled up to that site in one
bcftools process, in alignment order, so the stream is consumed exactly as one
stream consumes it, and the recalled records replace the workers'. All other
sites never depend on the stream. `--region` and VCF input always use one
stream, as does an unindexed alignment. Use a matching BAM/CRAM, FASTA
and indexed target table spanning at least two chromosomes, with no region flag.

```sh
agent-coord run --cores 8 --mem 8G --exclusive \
  --label "microarray serial-parallel benchmark" -- \
  pixi run python scripts/benchmark_microarray_parallel.py \
    --input out/public/full.cram --ref out/reference/genome.fa.gz \
    --config out/benchmark-config.toml \
    --ref-vcf-tab out/reference/All_SNPs_hg38_ref.tab.gz \
    --threads 8 --repeats 2 --outdir out/parallel-benchmark
```

The Linux harness uses `wait4` for total user/system CPU (including descendants
reaped by the CLI), wall time, and maximum **individual-process** RSS. It also
samples aggregate live process-tree RSS every 100 ms; shared pages can be
counted repeatedly, so that value is not unique physical memory. No extra timing
utility is required. Repeated pairs alternate order to expose cache/order effects.
All runs share the same thread limit and emit SHA256 hashes of final exports;
intermediate compressed VCF headers and log timings are intentionally excluded.
The harness rejects a parallel run that did not actually merge two or more
chromosome chunks. Use a fresh output directory and retain logs for every run.

Run small inputs first and full genomes last under a resource lease. A rebased
Install Lab mini-genome is useful for execution/byte-identity testing but **not**
for real vendor-chip concordance in native genomic coordinates.

Do not change a default based on speed alone. Require byte-identical complete
exports and representative small/full measurements, documenting CPU, memory,
cache/order effects and machine/thread limits. Deep coverage can trigger
independent caller depth subsampling, so concordance is not proof of byte identity.

### Recorded validation

The public rebased Install Lab HG00096 small input spans chromosomes 20/Y/MT;
its benchmark target contains only public marker coordinates and alleles.
Two alternating pairs at two threads produced identical hashes for all
22 final files in every run:

| Mode | Wall seconds, repeats 1 / 2 | CPU seconds, repeats 1 / 2 | Individual peak MiB, repeats 1 / 2 | Sampled tree peak MiB, repeats 1 / 2 |
| --- | --- | --- | --- | --- |
| Serial | 28.09 / 29.64 | 26.78 / 28.15 | 54.9 / 55.0 | 85.0 / 84.4 |
| Parallel | 26.36 / 26.32 | 25.54 / 26.11 | 55.9 / 56.2 | 171.1 / 140.9 |

The complete 15.74 GB public HG00096 GRCh38 CRAM was fetched from the
[AnVIL mirror](https://anvilproject.s3.amazonaws.com/file/d3354f61a055adfcfc988470bc507b2d.md5)
and verified against its official MD5 `d3354f61a055adfcfc988470bc507b2d`.
All 3,366 reference contig lengths/M5s matched the actual indexed FASTA.
Two alternating pairs, again at two threads, produced identical hashes for
all **23 final files in every run** (21 layouts plus native/projected CombinedKit):

| Mode | Wall seconds, repeats 1 / 2 | CPU seconds, repeats 1 / 2 | Individual peak MiB, repeats 1 / 2 | Sampled tree peak MiB, repeats 1 / 2 |
| --- | --- | --- | --- | --- |
| Serial | 1532.79 / 1627.38 | 1490.67 / 1596.04 | 787.9 / 787.4 | 788.5 / 788.5 |
| Parallel | 1059.41 / 1016.86 | 1598.37 / 1563.51 | 788.6 / 788.2 | 836.6 / 829.6 |

These are shared-host two-core measurements, not isolated eight-core scaling
evidence; other leased correctness/realignment work ran concurrently.
Full-genome paired wall speedups were 1.45x and 1.60x, with slightly higher average
CPU and higher sampled tree RSS in parallel. The small speed gain was modest
and its sampled aggregate RSS increased substantially. Those runs had no
deep-site reconciliation; its final exports happened to match, but per-contig
workers could change likelihoods at deep sites (see below).

A private 30x GRCh38 (hs38DH) CRAM has 81 sites with INFO/DP above 255 across
10 chromosomes (62 on chrM). Per-contig workers without reconciliation changed
QUAL/PL, but no genotype, at two of them; with reconciliation, the parallel VCF
body equals the serial one and all 23 final exports matched in every pair.
One pair per row, on an otherwise idle eight-core host; recalling the 10
chromosomes took 71–96 s:

| Threads | Code | Serial wall / CPU s | Parallel wall / CPU s |
| --- | --- | --- | --- |
| 2 | without reconciliation | 789.7 / 783.7 | 561.9 / 836.2 |
| 2 | with reconciliation | 772.7 / 771.6 | 721.4 / 1007.1 |
| 6 | without reconciliation | 722.1 / 726.8 | 433.2 / 1101.4 |
| 6 | with reconciliation | 687.7 / 693.2 | 434.2 / 1064.4 |

The recall runs after the workers, so it costs most at low thread counts. At six
threads parallel was 1.6x faster than serial and matched the unreconciled code;
repeated runs on this host varied by up to about 20%. Individual peak RSS stayed
near 0.9 GiB; sampled tree RSS reached 2.0 GiB in parallel at six threads.

Separately, a complete public GRCh38 HG00096 extraction was scored against
independently measured chip windows across all 22 autosomes and X. All
21 consumer layouts exceeded 99.76% called concordance and 99.937% conditional
call rate; every represented chromosome exceeded 98% concordance. The NCBI36
FTDNA V1 layout had overlapping truth on 22 chromosomes, the others on 23.
CombinedKit had 13,631 called overlaps, 13,620 exact (99.919%) and three no-calls.
This is sampled-window validation, not exhaustive whole-genome chip coverage.

A genuine complete native CHM13 HG00096 extraction was independently scored
against the same coordinate-selected chip windows. All 21 actual vendor exports
passed their full-run checks: minimum called concordance 99.760%, conditional
call rate 99.952%, and represented-chromosome concordance 98.684%.
Different-sample control concordance ranged from 51.16% to 57.14%.
CombinedKit retained 13,624/13,636 called exact (99.912%), 12 discordances and six
no-calls. Coordinate-only GRCh37-to-native inverted sites had 708/709 called exact,
including 275/275 with differing reference alleles and no inverted no-calls.
These map strata are not interchangeable with GRCh38-to-native WGS comparison
strata.

One historical chip annotation, [rs14115](https://www.ncbi.nlm.nih.gov/snp/rs14115),
labels chromosome 22 whereas NCBI places it at GRCh37 X:51666786, matching the
native extraction. The CombinedKit chromosome-mismatch counter and strict
full-run annotation failure remain visible: neither truth nor output was relabeled,
excluded or waived. All 21 vendor chromosome checks passed.
Every nonconcordant public row was retained locally for investigation; only
aggregate results are published. No genotype-based eligibility changes were made.

## Accuracy versus coverage

How much depth does a genome need for trustworthy kits? The whole public
HG00096 CRAM above (MD5 `d3354f61a055adfcfc988470bc507b2d`) was downsampled
with `samtools view -s 42.FRACTION`, one seed for every level, so each smaller
read set is contained in the larger ones. `wgsextract microarray` made all 21
consumer layouts at each depth (two threads, default bcftools caller), and
every kit was scored against the sample's own Omni 2.5 chip (MD5
`620462f7d8ffc9b82e1fff90c3aecd37`, 708,762 rsID markers with a genotype) by
the allele-exact identity scorer above. Eligibility never depends on what a kit
called.

Depth is the CLI's own measure, the one the low-coverage warning uses: mapped
bases, duplicates included, over the autosomes' non-N length. The full CRAM
measures 40.1x on it (about 34x without duplicates). Rows are 23andMe V5's
157,843 autosomal markers on the chip; chrX and chrY are scored only where the
chip calls this man homozygous, since its chrY "heterozygotes" are probe
artefacts. The Omni release has no mtDNA.

| Depth | Measured | Called concordance % | No-call % | Het correct % | Het called hom % | Hom-alt correct % | Hom-ref correct % | chrX / chrY concordance % | CombinedKit homozygosity % | Warnings |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| full | 40.1x | 99.93 | 0.08 | 99.79 | 0.14 | 99.80 | 99.92 | 99.98 / 98.91 | 77.9 | - |
| 30x | 30.0x | 99.89 | 0.08 | 99.66 | 0.27 | 99.80 | 99.92 | 99.98 / 98.91 | 77.9 | - |
| 20x | 20.0x | 99.53 | 0.08 | 98.60 | 1.32 | 99.79 | 99.92 | 99.98 / 98.91 | 78.2 | - |
| 15x | 15.0x | 98.57 | 0.08 | 95.80 | 4.13 | 99.79 | 99.92 | 99.96 / 98.90 | 78.8 | - |
| 10x | 10.0x | 95.71 | 0.11 | 87.37 | 12.53 | 99.73 | 99.90 | 99.86 / 98.99 | 80.7 | - |
| 8x | 8.0x | 92.90 | 0.20 | 79.07 | 20.74 | 99.59 | 99.81 | 99.79 / 98.97 | 82.5 | low_coverage:autosomes |
| 6x | 6.0x | 88.08 | 0.72 | 64.64 | 34.66 | 99.01 | 99.27 | 99.73 / 99.03 | 85.6 | low_coverage:autosomes |
| 5x | 5.0x | 84.52 | 1.58 | 53.86 | 44.62 | 98.03 | 98.39 | 99.59 / 99.10 | 87.9 | low_coverage:autosomes |
| 4x | 4.0x | 80.15 | 3.48 | 40.54 | 56.05 | 95.91 | 96.51 | 99.51 / 99.16 | 90.7 | high_homozygosity, low_coverage:autosomes |
| 3x | 3.0x | 75.41 | 7.97 | 25.99 | 66.07 | 91.14 | 92.05 | 99.39 / 99.31 | 93.8 | high_homozygosity, low_coverage:autosomes |
| 2x | 2.0x | 70.69 | 18.25 | 11.93 | 69.86 | 80.45 | 81.84 | 99.43 / 99.29 | 96.8 | high_homozygosity, high_no_call_rate, low_coverage:autosomes |
| 1x | 1.0x | 67.09 | 42.56 | 2.38 | 54.87 | 56.18 | 57.59 | 99.11 / 98.82 | 99.1 | high_homozygosity, high_no_call_rate, low_coverage:autosomes |

All 21 layouts track each other closely:

| Depth | Kits | Called concordance % | No-call % on truth markers | Kit no-call % (all markers) |
| --- | ---: | ---: | ---: | ---: |
| full | 21 | 99.84–99.95 | 0.04–0.10 | 0.06–0.25 |
| 30x | 21 | 99.79–99.91 | 0.04–0.10 | 0.06–0.25 |
| 20x | 21 | 99.47–99.55 | 0.04–0.10 | 0.07–0.27 |
| 15x | 21 | 98.53–98.72 | 0.04–0.11 | 0.07–0.28 |
| 10x | 21 | 95.58–96.19 | 0.07–0.14 | 0.10–0.33 |
| 8x | 21 | 92.73–93.76 | 0.17–0.24 | 0.21–0.44 |
| 6x | 21 | 87.81–89.48 | 0.69–0.76 | 0.71–0.98 |
| 5x | 21 | 84.15–86.36 | 1.54–1.60 | 1.55–1.84 |
| 4x | 21 | 79.71–82.57 | 3.28–3.49 | 3.45–3.77 |
| 3x | 21 | 74.80–78.35 | 7.86–8.12 | 7.87–8.33 |
| 2x | 21 | 69.98–74.26 | 18.07–18.76 | 18.16–18.88 |
| 1x | 21 | 66.25–71.07 | 42.40–43.25 | 42.47–43.34 |

What this means:

- **Below about 15x, heterozygous sites are what fail.** Homozygous sites stay
  above 99% correct down to 6x, but heterozygous sites called homozygous rise
  from 0.3% at 30x to 4% at 15x, 13% at 10x and 21% at 8x. Kits stay almost
  fully called, so the errors are silent: an 8x genome gives 99.8% call rate
  and 93% concordance.
- **The depth warning therefore now starts below 15x** (`qc_min_diploid_depth`,
  previously 8x), where at least 98.5% of called autosomal genotypes agree with
  the chip. The haploid 3x threshold stands: chrX/chrY calls remain above 98%
  concordant down to 1x, but at 3x autosomal depth (chrY 3x) 29% of chrY chip
  markers are no-calls.
- **The genotype-rate warnings are backstops, not depth gauges.** CombinedKit
  homozygosity passes 88% only near 4-5x and the no-call rate passes 10% only
  between 3x and 2x, so they catch low-pass and imputed inputs, which is their
  purpose (#306), not the 5-15x range. Both are unchanged; this genome's own
  homozygosity was 78% at full depth, and populations differ.
- One European male, one caller and one chip: DeepVariant and other callers
  degrade differently, and an effective depth without duplicates is about 0.84
  of the CLI measure here.

Reproduce, under a lease (about 25 minutes of two-thread `microarray` per
depth; the download is 15.7 GB):

```sh
agent-coord run --cores 2 --mem 6G --disk /home:40G --label "coverage sweep" -- \
  pixi run python scripts/benchmark_microarray_coverage.py \
    --input out/public/HG00096.final.cram --ref out/reference/hs38DH.fa.gz \
    --truth-vcf out/public/omni.vcf.gz --sample HG00096 \
    --outdir out/coverage --threads 2
```

The truth is the GRCh37 Omni release listed at the top of this page, with
its `.tbi`; markers are matched by rsID and chromosome, so its build does not
matter. The script resumes from completed depths and deletes each
downsampled CRAM after use unless `--keep-downsampled` is given. It writes
`summary.json` and `summary.md` with aggregate counts only.

## Coverage boundaries

The weekly cases exercise GRCh38 calling/projection and genuine native CHM13
calling/projection into GRCh37 consumer formats, each on chr20. Neither weekly
case establishes whole-genome, sex-chromosome, mitochondrial or I/D accuracy.
No rebased GRCh38 input, synthetic truth, skipped placeholder or chain-only test
constitutes a T2T chip benchmark.
AADR PLINK/EIGENSTRAT and proprietary I/D encodings are outside this consumer
SNP-chip gate and retain their own focused tests.
