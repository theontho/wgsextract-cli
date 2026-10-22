# WGS Extract CLI Todo List

Completed work on WGS Extract CLI. Open work is tracked in [GitHub Issues](https://github.com/theontho/wgsextract-cli/issues).

## Project

- [x] **Merge v5 changes**: DeepVariant, GATK, and trio analysis integrated.
- [x] **Pixi environments**: Python and bioinformatics tools are managed by Pixi (`pixi.toml`), with separate `pacbio`, `deepvariant`, `vep`, and `dev` environments.
- [x] **Automated release process**: Pushing a `vX.Y.Z` tag runs `release.yml`: it checks the tag against the versions in `pyproject.toml`, `pixi.toml` and the package, runs lint, type checks, tests and the package smoke test, and creates a draft release with generated notes. Publishing the draft builds and attaches the Windows native tools (`release_windows_bwa.yml`); `install.sh` installs the latest published release.
- [x] **Move to GitHub Issues**: Open items moved from this file to [GitHub Issues](https://github.com/theontho/wgsextract-cli/issues).

## CLI and core engine

### VCF and variant calling

- [x] **Callers**: bcftools SNP/InDel, FreeBayes, GATK HaplotypeCaller, DeepVariant, Delly SV/CNV, pbsv and Sniffles for PacBio.
- [x] **Filtering**: `vcf filter` by expression, region, or gene name.
- [x] **Annotation**: `vcf annotate` and VEP (`vep run`).
- [x] **Chain annotation**: `vcf chain-annotate` applies several annotations in sequence.
- [x] **General consensus generation**: `vcf to-fasta` applies a VCF to the reference (heterozygous SNVs as IUPAC codes by default; `--haplotype` picks one allele), writes N where a gVCF has no call, and can write a chain file.

### Gene analysis and clinical annotation

- [x] **Gene-centric filtering**: Filter VCFs and BAMs by gene name (`--gene`, using `ref gene-map` data for hg19/hg38).
- [x] **Trio analysis**: `vcf trio` with de novo, recessive, and compound-heterozygous modes.
- [x] **Population frequencies**: gnomAD annotation (`vcf gnomad`).
- [x] **gnomAD v4.1**: `ref gnomad --release 4.1` streams the native-GRCh38 joint exome+genome VCFs (817 GiB, about 2.5 h at 90 MiB/s) into a 4.6 GB PASS-site allele-frequency table, used by `vcf gnomad`. The default stays v2.1.1 (exomes, lifted over to hg38).
- [x] **ClinVar**: Clinical significance via `vcf clinvar`.
- [x] **Pathogenicity and conservation scores**: REVEL, AlphaMissense, SpliceAI, and PhyloP annotators. SIFT/PolyPhen are only available through VEP.
- [x] **Pharmacogenomics**: PharmGKB annotation (`vcf pharmgkb`) and star-allele diplotypes and phenotypes with PharmCAT (`vcf pharmcat`).
- [x] **CYP2D6 calling**: `typing cyp2d6` runs Cyrius (noncommercial license, opt-in `cyrius` environment) on a WGS BAM/CRAM and writes a PharmCAT outside call; `vcf pharmcat --outside-call` passes it on.
- [x] **CADD and GERP++ annotation**: `ref cadd` / `vcf cadd` (CADD v1.7, SNVs plus gnomAD indels) and `ref gerp` / `vcf gerp` (GERP++ bigWig). Large annotation files are split by contig and annotated in parallel.

### BAM/CRAM processing

- [x] **mtDNA/Y-DNA extraction**: `extract mito-fasta`, `mito-vcf`, `mt-bam`, `ydna-bam`, `ydna-vcf`, and `y-mt-extract`.
- [x] **Reference verification**: `ref verify` checks gzip integrity, the FASTA index, and the sequence-dictionary signature of catalog genomes.
- [x] **sambamba and samblaster**: Used for faster processing where available.
- [x] **BAM/CRAM merging**: `bam merge` merges coordinate-sorted files aligned to the same reference into one indexed BAM or CRAM.
- [x] **uBAM creator**: `bam to-ubam` converts an aligned BAM/CRAM or FASTQs into a queryname-sorted unaligned BAM with read groups.

### Microarray and specialized analysis

- [x] **Pet sequencing**: Dog and cat alignment and variant calling via `pet-align`. Extraction and lineage commands are human-only.
- [x] **Parallel microarray generation**: `microarray --parallel` calls variants per chromosome.
- [x] **Legacy formats**: `microarray --formats ftdna_v1` (Affymetrix chip, NCBI36 positions). 23andMe v1/v2 need templates (#99).
- [x] **Digital signing**: `sign create` writes a `sha256sum`-compatible `SHA256SUMS` manifest, signed with `ssh-keygen -Y sign` when given a key; `sign verify` checks the signature, then every file's hash.
- [x] **Advanced typing**: `typing hla` (T1K on MHC reads, with a PharmCAT outside call for HLA-A/HLA-B) and `typing str` (ExpansionHunter, classified against STRchive's normal and pathogenic ranges).

### CLI

- [x] **Exit codes**: Every command exits non-zero on failure.
- [x] **Machine-readable output** for selected commands: `info --csv`, `ref status --json`, `vep run --format json|tab`.
- [x] **Smoke tests**: Automated CLI smoke tests (`pixi run smoke-test`).
- [x] **VEP on Apple Silicon**: VEP's Pixi environment runs natively on osx-arm64 (`PERL_DL_NONLAZY` works around conda-forge's x86_64-only libdb build); a whole-genome `--preset everything` run takes 1.9 h instead of 3.7 h under Rosetta.

## Install lab

- [x] **Install, use, update, uninstall on fresh machines**: macOS (Tart), Linux (container), Windows 11 (QEMU) and GitHub's runners, for the CLI and the WGS Extract app, with negative controls and mutation tests (`docs/install_testing.md`).
- [x] **Disk use**: Expired runs, images and leftovers are removed (`pixi run gc`).

## Technical debt and known bugs

### Technical debt

- [x] **Python port**: Core Bash scripts ported to Python.
- [x] **Path validation**: `verify_paths_exist` supports Pixi command strings and restricted environments.

### Known bugs

- [x] **Cross-platform TTY issues**: Resolved.
- [x] **Yleaf integration**: Fixed command naming and execution on macOS ARM; output now goes to `<outdir>/yleaf/`.
- [x] **samtools hangs**: Not reproduced. Whole-genome `fastq`, `collate`, and `sort` all finished on a 680M-read MGI CRAM; the CLI uses samblaster rather than `samtools markdup`; and pipeline stderr is spooled, so a full pipe can't deadlock. FASTQ export now uses `samtools collate` instead of a name sort (whole genome: 26 min down to 23 min; peak temp space about the same). Related: in samtools ≤1.24, `samtools view x.cram '*'` returns only the last unplaced container ([samtools#2396](https://github.com/samtools/samtools/issues/2396)); the CLI selects unmapped reads with `-f 4` instead.
