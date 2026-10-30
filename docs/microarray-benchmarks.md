# Microarray benchmarks

The weekly remote-resource test generates all **21 consumer layouts** and a
CombinedKit from public HG00096 GRCh38 WGS, then compares them with that sample's
independently measured Illumina Omni 2.5 chip. It also scores against HG00097 as
a different-sample negative control. This is not a comparison of two generated
kits, nor a validation of every marker or chromosome.

- WGS: [1000 Genomes NYGC HG00096 30x GRCh38 CRAM](https://ftp.sra.ebi.ac.uk/vol1/run/ERR324/ERR3240114/HG00096.final.cram),
  `chr20:10000000-12000000`.
- Reference: [GRCh38 full analysis set](https://ftp.1000genomes.ebi.ac.uk/vol1/ftp/technical/reference/GRCh38_reference_genome/GRCh38_full_analysis_set_plus_decoy_hla.fa).
- Chip: [1000 Genomes Omni release 20140818](https://ftp.1000genomes.ebi.ac.uk/vol1/ftp/release/20130502/supporting/hd_genotype_chip/ALL.chip.omni_broad_sanger_combined.20140818.snps.genotypes.vcf.gz),
  GRCh37 `20:9900000-12100000`.

For regional WGS, chip loci must independently lift uniquely and forward into
the actual extracted GRCh38 interval before scoring. The wider GRCh37 query
margin is not part of the call-rate denominator; eligibility is never selected
from which genotypes the generated kit happened to call.

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
concordance <90%. This is a SNP benchmark, not evidence for I/D accuracy.

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

## Coverage boundaries

The weekly case exercises GRCh38 calling and GRCh38-to-GRCh37 vendor projection.
Native T2T extraction requires the separately developed CHM13 runtime and a
genuine public CHM13 alignment/reference; no rebased GRCh38 input, synthetic
truth, skipped placeholder or chain-only test constitutes a T2T chip benchmark.
AADR PLINK/EIGENSTRAT and proprietary I/D encodings are outside this consumer
SNP-chip gate and retain their own focused tests.
