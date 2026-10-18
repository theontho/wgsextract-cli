# WGS Extract CLI Todo List

Planned features, improvements, and known bugs for WGS Extract CLI.

## Project

- [x] **Merge v5 changes**: DeepVariant, GATK, and trio analysis integrated.
- [x] **Pixi environments**: Python and bioinformatics tools are managed by Pixi (`pixi.toml`), with separate `pacbio`, `deepvariant`, `vep`, and `dev` environments.
- [ ] **Automated release process**: GitHub Actions for platform-specific installers.
- [ ] **Move to GitHub Issues**: Migrate this list to GitHub Issues.

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
- [x] **ClinVar**: Clinical significance via `vcf clinvar`.
- [x] **Pathogenicity and conservation scores**: REVEL, AlphaMissense, SpliceAI, and PhyloP annotators. SIFT/PolyPhen are only available through VEP.
- [x] **Pharmacogenomics**: PharmGKB annotation (`vcf pharmgkb`).
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
- [ ] **Advanced typing**: HLA typing and STR variant calling.

### CLI

- [x] **Exit codes**: Every command exits non-zero on failure.
- [x] **Machine-readable output** for selected commands: `info --csv`, `ref status --json`, `vep run --format json|tab`.
- [x] **Smoke tests**: Automated CLI smoke tests (`pixi run smoke-test`).

## Technical debt and known bugs

### Technical debt

- [x] **Python port**: Core Bash scripts ported to Python.
- [x] **Path validation**: `verify_paths_exist` supports Pixi command strings and restricted environments.

### Known bugs

- [x] **Cross-platform TTY issues**: Resolved.
- [x] **Yleaf integration**: Fixed command naming and execution on macOS ARM; output now goes to `<outdir>/yleaf/`.
- [ ] **samtools hangs**: `markdup` and `fastq` hang on some MGI/Dante files.
