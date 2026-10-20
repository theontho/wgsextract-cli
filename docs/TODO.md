# WGS Extract CLI Todo List

Planned features, improvements, and known bugs for WGS Extract CLI.

## Project

- [x] **Merge v5 changes**: DeepVariant, GATK, and trio analysis integrated.
- [x] **Pixi environments**: Python and bioinformatics tools are managed by Pixi (`pixi.toml`), with separate `pacbio`, `deepvariant`, `vep`, and `dev` environments.
- [ ] **Automated release process**: GitHub Actions for platform-specific installers.
- [ ] **Move to GitHub Issues**: Migrate this list to GitHub Issues.
- [ ] **Signed, notarized app builds from GitHub**: The app's Distribution workflow signs on GitHub's macOS runners, but notarization fails with HTTP 403 until Apple's current developer agreements are accepted (gui-for-cli #143).

## CLI and core engine

### VCF and variant calling

- [x] **Callers**: bcftools SNP/InDel, FreeBayes, GATK HaplotypeCaller, DeepVariant, Delly SV/CNV, pbsv and Sniffles for PacBio.
- [x] **Filtering**: `vcf filter` by expression, region, or gene name.
- [x] **Annotation**: `vcf annotate` and VEP (`vep run`).
- [x] **Chain annotation**: `vcf chain-annotate` applies several annotations in sequence.
- [ ] **General consensus generation**: A `vcf to-fasta` command to build a full consensus FASTA from a VCF and reference (currently only `extract mito-fasta`).

### Gene analysis and clinical annotation

- [x] **Gene-centric filtering**: Filter VCFs and BAMs by gene name (`--gene`, using `ref gene-map` data for hg19/hg38).
- [x] **Trio analysis**: `vcf trio` with de novo, recessive, and compound-heterozygous modes.
- [x] **Population frequencies**: gnomAD annotation (`vcf gnomad`).
- [ ] **gnomAD v4.1**: `ref gnomad` downloads v2.1.1 (2019): exomes only, and its hg38 file is lifted over from hg19. v4.1 is native GRCh38 with genomes and exomes but several hundred GB in per-chromosome files; offer an allele-frequency-only download built from them.
- [x] **ClinVar**: Clinical significance via `vcf clinvar`.
- [x] **Pathogenicity and conservation scores**: REVEL, AlphaMissense, SpliceAI, and PhyloP annotators. SIFT/PolyPhen are only available through VEP.
- [x] **Pharmacogenomics**: PharmGKB annotation (`vcf pharmgkb`) and star-allele diplotypes and phenotypes with PharmCAT (`vcf pharmcat`).
- [ ] **CYP2D6 calling**: PharmCAT can't call CYP2D6 from a VCF (copy-number and hybrid alleles). Add a BAM/CRAM-based caller (e.g. Cyrius or StellarPGx) and pass its call to PharmCAT as an outside call.
- [ ] **SpliceAI download**: Precomputed scores sit behind an Illumina BaseSpace login, so `ref spliceai` only indexes a manually downloaded file.
- [ ] **CADD and GERP++ annotation**: Not implemented.

### BAM/CRAM processing

- [x] **mtDNA/Y-DNA extraction**: `extract mito-fasta`, `mito-vcf`, `mt-bam`, `ydna-bam`, `ydna-vcf`, and `y-mt-extract`.
- [x] **Reference verification**: `ref verify` checks gzip integrity, the FASTA index, and the sequence-dictionary signature of catalog genomes.
- [x] **sambamba and samblaster**: Used for faster processing where available.
- [ ] **BAM/CRAM merging**: A `bam merge` command for multiple alignment files.
- [ ] **uBAM creator**: Convert FASTQ/BAM to unaligned BAM for GATK pipelines.

### Microarray and specialized analysis

- [x] **Pet sequencing**: Dog and cat alignment and variant calling via `pet-align`. Extraction and lineage commands are human-only.
- [x] **Parallel microarray generation**: `microarray --parallel` calls variants per chromosome.
- [ ] **Legacy formats**: 23andMe v1, FTDNA v1, and similar.
- [ ] **Digital signing**: Sign output files for verification.
- [ ] **Advanced typing**: HLA typing (also needed for PharmCAT's HLA-A/HLA-B guidance) and STR variant calling.

### CLI

- [x] **Exit codes**: Every command exits non-zero on failure.
- [x] **Machine-readable output** for selected commands: `info --csv`, `ref status --json`, `vep run --format json|tab`.
- [x] **Smoke tests**: Automated CLI smoke tests (`pixi run smoke-test`).
- [ ] **VEP on Apple Silicon**: VEP's Pixi environment runs under Rosetta (bioconda's osx-arm64 libdb build is x86_64-only). A native build, or the `fast` preset by default, would cut whole-genome runs (3.7 h with `--preset everything`).

## Install lab

- [x] **Install, use, update, uninstall on fresh machines**: macOS (Tart), Linux (container), Windows 11 (QEMU) and GitHub's runners, for the CLI and the WGS Extract app, with negative controls and mutation tests (`docs/install_testing.md`).
- [x] **Disk use**: Expired runs, images and leftovers are removed (`pixi run gc`).
- [ ] **Windows installer wizard**: The app's installer is run with `/S`; only uninstall goes through the real wizard.
- [ ] **Signed installers**: No signed release installer has been tested yet; the "must verify" check is covered only by a mutation.
- [ ] **`webAppRendered` on Windows**: The app's startup metric is never printed on Windows (the page works); the lab uses the page-load metric instead.

## Technical debt and known bugs

### Technical debt

- [x] **Python port**: Core Bash scripts ported to Python.
- [x] **Path validation**: `verify_paths_exist` supports Pixi command strings and restricted environments.

### Known bugs

- [x] **Cross-platform TTY issues**: Resolved.
- [x] **Yleaf integration**: Fixed command naming and execution on macOS ARM; output now goes to `<outdir>/yleaf/`.
- [ ] **samtools hangs**: `markdup` and `fastq` hang on some MGI/Dante files.
