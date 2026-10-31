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

The default remains **serial**. `--parallel --region ...` does not exercise the
parallel caller, and VCF input does not either. Use a matching BAM/CRAM, FASTA
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
and its sampled aggregate RSS increased substantially. These limited measurements
do not justify changing the default across machines, so **serial remains the default**.

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

## Coverage boundaries

The weekly cases exercise GRCh38 calling/projection and genuine native CHM13
calling/projection into GRCh37 consumer formats, each on chr20. Neither weekly
case establishes whole-genome, sex-chromosome, mitochondrial or I/D accuracy.
No rebased GRCh38 input, synthetic truth, skipped placeholder or chain-only test
constitutes a T2T chip benchmark.
AADR PLINK/EIGENSTRAT and proprietary I/D encodings are outside this consumer
SNP-chip gate and retain their own focused tests.
