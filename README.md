# WGS Extract CLI (`wgsextract-cli`)

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Python 3.10](https://img.shields.io/badge/python-3.10-blue.svg)](https://www.python.org/downloads/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

An independent command-line recreation of [WGS Extract](https://github.com/WGSExtract/WGSExtract-Dev/). It wraps samtools, bcftools, bwa/minimap2, GATK, Yleaf, Haplogrep, VEP and other tools so you can inspect BAM/CRAM files, simulate consumer microarray files, extract Y/mtDNA data, call and annotate variants, and manage reference genomes from scripts or the terminal. [Pixi](https://pixi.sh) provides Python and the bioinformatics tools on macOS, Linux, and Windows.

Documentation site: <https://theontho.github.io/wgsextract-cli/>. A separate graphical app is available from [gui-for-cli](https://github.com/theontho/gui-for-cli/releases).

## Install

macOS and Linux: open Terminal, paste this command, and press Enter.
Native Linux ARM64 is not supported yet: the installer refuses before downloading
or installing dependencies. Native ARM64 support is tracked in #236; this is not
an x86-emulation fallback.

```bash
curl -fsSL https://raw.githubusercontent.com/theontho/wgsextract-cli/main/install.sh | sh
```

The bootstrap script comes from `main`, but it installs the latest published GitHub release, not `main`. It creates `wgsextract-cli/` in the current directory (or next to a downloaded `install.sh`) containing the app (`app/`), Pixi files (`.pixi/`), the `wgsextract` launcher, and `uninstall.sh`. On macOS it checks for the Xcode Command Line Tools first.

Windows: clone or download the repository and run `install_windows.bat` from PowerShell. It installs Pixi when missing, then the CLI and its native Windows tools (WGS Extract's own builds of samtools, bcftools, BWA, bwa-mem2, minimap2, fastp and samblaster) into the Pixi environment. No WSL or MSYS2 is needed. See [docs/windows.md](docs/windows.md).

The [install guide](https://theontho.github.io/wgsextract-cli/install.html) covers platform notes, download verification, and the reference library in more detail.

### Installer options

Set these environment variables before running `install.sh`:

| Variable | Default | Purpose |
| :--- | :--- | :--- |
| `WGSEXTRACT_INSTALL_DIR` | `wgsextract-cli` next to `install.sh`, or `./wgsextract-cli` for `curl \| sh` | Install location |
| `WGSEXTRACT_RELEASE_TAG` | latest GitHub release | Release tag to install, or `latest` |
| `WGSEXTRACT_REF` | `WGSEXTRACT_RELEASE_TAG` | Git ref to install, or `latest` |
| `WGSEXTRACT_ARCHIVE_URL` | GitHub source archive for `WGSEXTRACT_REF` | Exact source archive URL |
| `WGSEXTRACT_BIN_DIR` | `$WGSEXTRACT_INSTALL_DIR` | CLI launcher directory |
| `WGSEXTRACT_PIXI_HOME` | unset | Install the SHA-256-verified pinned Pixi under this directory when Pixi is missing, instead of `~/.pixi` |
| `WGSEXTRACT_PIXI_CACHE_DIR` | `$WGSEXTRACT_INSTALL_DIR/.pixi/cache` | Pixi package cache directory |
| `WGSEXTRACT_PIXI_ENV_DIR` | `$WGSEXTRACT_INSTALL_DIR/app/.pixi/envs` | Pixi environment directory (kept across updates) |
| `WGSEXTRACT_NO_OPEN` | unset | Set to `1` to skip opening Finder on macOS |

Leave `WGSEXTRACT_BIN_DIR`, `WGSEXTRACT_PIXI_HOME`, `WGSEXTRACT_PIXI_CACHE_DIR`, and `WGSEXTRACT_PIXI_ENV_DIR` unset to keep everything inside one directory. For reproducible installs set `WGSEXTRACT_RELEASE_TAG=vX.Y.Z`; for testing unreleased code set `WGSEXTRACT_REF=main` or `WGSEXTRACT_ARCHIVE_URL=<url>`.

When Pixi is missing, installers verify the pinned release's SHA-256 before running it; unsigned upstream installer scripts are not executed. Updating the Pixi pin requires updating its trusted release digests too. `ref library --install` also verifies catalogued references against their sequence-name/length signature after indexing. The catalog's `md5` is this dictionary signature, not a FASTA content checksum.

### Uninstall

```bash
./wgsextract-cli/uninstall.sh --yes
```

This removes the install directory, launcher, Pixi environments, and Pixi cache under it, including a reference library in the default `app/reference/` location. Rerunning the installer to update keeps `app/reference/`, `app/genomes/`, and the Pixi environments. `config.toml` is kept unless you add `--remove-config`. Interactive runs ask whether to remove Pixi from `~/.pixi`; with `--yes`, Pixi is kept unless you add `--remove-pixi`, which also removes Pixi's `PATH` line from your shell profiles (and deletes a profile such as `~/.zshrc` if the install created it and nothing else was added). Custom `WGSEXTRACT_BIN_DIR`, `WGSEXTRACT_PIXI_CACHE_DIR`, and `WGSEXTRACT_PIXI_ENV_DIR` locations are recorded at install time, so the uninstaller finds them; `--bin-dir`, `--pixi-cache-dir`, and `--pixi-env-dir` override them.

### Running the CLI

How you invoke the CLI depends on how you installed it:

| Install type | Command |
| :--- | :--- |
| `install.sh` | `./wgsextract-cli/wgsextract` (or add `wgsextract-cli/` to your `PATH`) |
| Source checkout or Windows install | `pixi run wgsextract`, from the repository directory |

The rest of this README writes plain `wgsextract`; substitute the form that matches your install.

## Quick start

```bash
# Download the reference library bootstrap (VCFs, liftover chains, metadata)
wgsextract ref bootstrap

# List and install a reference genome
wgsextract ref library --list
wgsextract ref library --install hs38

# Check that the required tools are available (exits 1 if a mandatory tool is missing)
wgsextract deps check

# Inspect a BAM/CRAM: reference build, file integrity, read metrics
wgsextract --input sample.cram info --detailed

# Simulate a 23andMe v5 raw-data file
wgsextract --input sample.cram microarray --formats 23andme_v5

# Mitochondrial variants and haplogroups
wgsextract --input sample.cram extract mito-vcf
wgsextract --input sample.cram lineage mt-haplogroup
wgsextract --input sample.cram lineage y-haplogroup
```

Try commands on a small region (`--region chrM`, where supported) before running whole-genome jobs, which can take hours and hundreds of gigabytes of working space. Every command exits non-zero on failure, so they can be chained in scripts.

Alignment/realignment pipelines and GATK scatter print a stderr heartbeat every
60 seconds, including processed versus expected reads (when known), or completed
shards, elapsed time and a rate-based ETA. Sampled FASTQ read counts are estimates;
reaching the read count does not mean sorting/indexing has finished. Stdout data,
alignment/caller arguments and generated genomic files are unchanged.

Persistent logging is off by default. Use a **new, dedicated** path:

```sh
pixi run wgsextract --log-file out/run.log --input sample.bam info
```

Alternatively set `log_dir = "~/wgse_logs"` in `config.toml` to create a unique log
for each run. The UTF-8 DEBUG log retains Python diagnostics and pipeline/scatter
tool messages; `run.log.provenance.json` records argv, CLI/platform versions,
executed tool commands/versions and small reference-index signatures. Provenance
is available during the run and finalized on normal exit or handled failure. A
hard kill may leave its status as `running`. Existing logs are never truncated;
`--quiet` still suppresses informational console messages, not the file log.
Review logs before sharing: commands and diagnostics may contain private paths
or URLs. No environment is captured, no tool is installed for logging, and
complete reference genomes are not hashed.

Output files are named after the full input stem: `HG002.chr20.bam` produces `HG002.chr20_sorted.bam`, `HG002.chr20_coverage.tsv`, and so on. Outputs go to `--outdir`, the configured `outdir`, or the `--genome` folder.

## Commands

Run `wgsextract help` for the full command tree, and `wgsextract <command> --help` for options.

`info calculate-coverage --bins` replaces the normal summary with a TSV of per-contig counts and fractions at depth **0 / 1–3 / 4–7 / 8+**, also saved as `<input>_coverage_bins.tsv`. It makes one streaming `samtools depth` pass, never writes per-base depths, includes contigs with no reads, and supports `--region` (fractions then describe that interval). As in original WGS Extract v4, depth uses samtools' default base/mapping quality thresholds (zero), excludes unmapped/secondary/QC-failed/duplicate reads, excludes deletion bases and counts overlapping mates separately. Fractions use all reference coordinates (including N bases), not an estimated N-adjustment; the original UI's double-subtraction of N counts is not reproduced. Without `--bins`, the existing report and cache behavior are unchanged.

CRAM bin coverage requires an existing reference FASTA from `--ref`, configuration or the reference library. Missing references fail before decoding; htslib is never allowed to download one implicitly.

| Command | Subcommands / purpose |
| :--- | :--- |
| `info` | Reference build, file integrity, and read metrics. `calculate-coverage` (exact, via samtools coverage), `coverage-sample` (fast estimate) |
| `bam` | `identify`, `sort`, `index`, `unindex`, `unsort`, `to-cram`, `to-bam`, `unalign`, `merge`, `to-ubam` (unaligned BAM for GATK) |
| `extract` | `mito-fasta`, `mito-vcf`, `mt-bam`, `ydna-bam`, `ydna-vcf`, `y-mt-extract`, `unmapped`, `custom`, `bam-subset` |
| `microarray` | Simulate 23andMe, AncestryDNA, FTDNA, Living DNA, MyHeritage, and combined (`all`) raw-data files |
| `microarray --formats aadr_1240k,aadr_ho,aadr_1240k+ho` | Export pinned AADR research panels as PLINK and EIGENSTRAT ([conventions and validation](docs/aadr.md)) |
| `lineage` | `y-haplogroup` (Yleaf), `mt-haplogroup` (Haplogrep) |
| `vcf` | Calling: `snp`, `indel`, `freebayes`, `gatk`, `deepvariant`, `sv`, `cnv`. Annotation: `annotate`, `chain-annotate`, `clinvar`, `gnomad`, `revel`, `phylop`, `cadd`, `gerp`, `spliceai`, `alphamissense`, `pharmgkb`, `pharmcat`. Other: `filter`, `trio`, `to-fasta` (personal consensus genome) |
| `typing` | From a WGS BAM/CRAM: `cyp2d6` (Cyrius), `hla` (T1K), `str` (ExpansionHunter repeat expansions) |
| `gatk`, `freebayes` | Used by `vcf gatk` / `vcf freebayes` from optional environments; [Windows GATK](docs/windows.md#gatk-on-native-windows) is prepared at setup, not during calling |
| `vep` | `download`, `run`, `verify` (Ensembl Variant Effect Predictor) |
| `align` | Align FASTQ reads with bwa, bwa-mem2, minimap2, or pbmm2, or on an NVIDIA GPU with `--gpu` |
| `realign` | Re-align an existing BAM/CRAM to another reference, streaming paired reads into BWA or reusing FASTQs |
| `pet-align` | Align dog or cat FASTQs and call variants |
| `qc` | `fastp`, `fastqc`, `vcf` (stats), `fake-data` (synthetic BAM/CRAM/VCF/FASTQ) |
| `repair` | `ftdna-bam`, `ftdna-vcf` |
| `analyze` | `comprehensive` (QC, lineage, calling, annotation in one run), `batch-gen` |
| `ref` | Library: `bootstrap`, `library`, `library-list`, `status`, `download`, `index`, `verify`, `count-ns`, `gene-map`. Annotation data: `clinvar`, `gnomad`, `revel`, `phylop`, `cadd`, `gerp`, `spliceai`, `alphamissense`, `pharmgkb` |
| `sign` | `create` (SHA256SUMS manifest, optionally signed with an OpenSSH key), `verify` |
| `example-genome` | `list`, `download` curated 1000 Genomes / HGSVC2 datasets |
| `deps` | `check` |
| `benchmark` | Benchmark the main workflows on generated or release-backed data |
| `config` | Show the active config file and settings |

Notes on a few commands:

- `extract ydna-bam --poz` writes an indexed `_Y_poz.bam` containing reads overlapping the original WGS Extract CombBED/McDonald/Poznik union (about 12 Mb), **not** all stable Y SNPs or a BigY/exome capture. It supports exact GRCh37/hg19 or GRCh38 Y lengths with either `chrY` or `Y`, including Y-only inputs, and never includes the original BEDs' mitochondrial regions. Reads overlapping BED boundaries are kept whole. T2T, unknown and inconsistent assemblies are rejected rather than silently lifted. Bundled BED provenance/license is in `assets/reference/poz/NOTICE.txt`; no download is needed. Without `--poz`, whole-Y extraction is unchanged.

- `ref verify` checks gzip integrity, the FASTA index, and the sequence-dictionary signature of catalog genomes, and fails on mismatch. `ref count-ns` writes `_ncnt.csv`/`_nbin.csv` next to the FASTA, or into `--outdir`.
- `vcf snp` and `vcf indel` call the whole alignment as one bcftools stream by default, which keeps results reproducible. `--parallel` calls independent contigs at once for indexed whole-genome BAM/CRAM input, using up to `--threads` workers; sex and ploidy are resolved once, and the results are concatenated in reference-index order before indexing. Parallel calls are not byte-identical on deep data: bcftools subsamples sites with more than 255 bases using one random stream per process, so a few low-quality calls at very deep sites (on a 30x genome, 9 of 4.4 million SNPs, in pericentromeric and unplaced regions) can differ. `--threads 1`, explicit `--region`, unindexed inputs, and inputs whose contig order differs from the reference always use one stream.
- `vcf indel` resolves IUPAC reference ambiguity codes to their first alphabetical base (for example, `R` to `A`) during normalization, as the VCF specification requires. This prevents full hs38DH calls from aborting at ambiguous bases; ordinary reference alleles are unchanged. `vcf to-fasta` reconciles compatible ambiguity codes in its temporary input without changing the exported VCF or its called ALT bases.
- Gene-name filtering (`--gene`) needs gene maps from `ref gene-map` (hg19/hg38 only; T2T is rejected). Gene maps installed by older versions should be rebuilt with `ref gene-map`.
- `microarray` exports hg19 nuclear coordinates. The existing hg38 vendor route retains its legacy Yoruba-to-hg19 mitochondrial chain shifts and gaps; those positions are not rCRS. CHM13v2 original-MT/masked-Y input has a separate pinned hg19/rCRS projection path; see [reference requirements and limitations](docs/microarray-chm13.md).
- `microarray` genotypes every chip's markers at once from a genome-wide target list. A library set up with `ref bootstrap` has none, so the first run builds `microarray/All_SNPs_<build>_ref.tab.gz` from the chip templates (for hg38, lifted with UCSC's hg19ToHg38 chain, downloaded into the library) and later runs reuse it. Reads with mapping quality under 10 are ignored, so a marker whose reads could belong elsewhere in the genome is a no-call (`--`) rather than a guessed homozygous call. The kits match their owners' real AncestryDNA and MyHeritage chips at about 99% of markers; a weekly test checks 1000 Genomes sample HG00096's kit against its Omni chip.
- Microarray quality gates are opt-in: `--min-gq 20 --min-dp 5` requires sample FORMAT/GQ and FORMAT/DP at least those values; a missing required field is a no-call, not a pass. bcftools can omit GQ on reference-only records, which also become no-calls under an enabled GQ threshold. Both defaults are `0` (disabled), and FILTER values remain accepted unless `--exclude-filtered` is selected. Quality/reason options require a single sample; select one from a multi-sample VCF first. With VCF input, absent markers on represented contigs are assumed homozygous reference by default; `--no-assume-hom-ref` instead writes no-calls. An ordinary variant-only VCF cannot establish coverage or reference genotypes at absent sites; reference-block interpretation is not added here. Enabled quality thresholds also prevent unsupported reference imputation without GQ/DP.
- `microarray --call-reasons` adds `<sample>_CombinedKit_reasons.tsv`, a complete target-level table with the usual four fields plus `CALL_REASON`, in the input build. It distinguishes `no_reads` (no aligned base evidence under standard read-flag filters), `low_mapq` (evidence exists only below MAPQ 10), low/missing GQ or DP, `filtered`, `missing_gt`, unsupported alleles and absent VCF markers/contigs; reference-filled sites are labeled `assumed_hom_ref`. The legacy four-column CombinedKit and vendor schemas stay unchanged. Reason-only mode also preserves legacy unsupported-VCF skips/imputation; unsupported records become explicit no-calls when quality/FILTER gates are selected. BAM/CRAM reporting adds two depth passes per requested interval using indexed traversal with `--region`; without a region, it reads the whole alignment twice. Reporting is disabled by default.
- These microarray options have matching top-level `config.toml` settings: `microarray_min_gq`, `microarray_min_dp`, `microarray_assume_hom_ref`, `microarray_exclude_filtered` and `microarray_call_reasons`. Explicit CLI values override configuration, including `--min-gq 0`, `--assume-hom-ref`, `--no-exclude-filtered` and `--no-call-reasons`. Per-vendor profiles retain existing haploid, allele-order, quoting and no-call behavior; declaring a vendor's I/D encoding does not enable indel calling.
- `microarray --indel-manifest FORMAT=PATH` optionally calls sequence-defined insertion/deletion probes from a matching GRCh37 Illumina manifest (CSV, gzip or ZIP). Repeat for explicitly selected 23andMe, Ancestry or MyHeritage layouts. Full reference contexts and DNA alleles must verify; unsupported/absent or quality/identity-rejected calls stay missing. The separate caller leaves the SNP CombinedKit unchanged. See [manifest requirements, real encoding evidence and coverage limits](docs/microarray-indels.md); proprietary probe definitions and Ancestry probe-order reproduction are not inferred.
- [Microarray benchmarks](docs/microarray-benchmarks.md) score all consumer layouts against independent public chip data, report per-chromosome call rate/concordance, and measure serial/parallel CPU, wall time, memory and final-export byte identity. Serial remains the default.
- Identity checks are also opt-in, with unchanged default output: `--strand-ambiguous no-call` masks known biallelic A/T and C/G **input REF/ALT sites**, including homozygous calls. This is a conservative site filter, **not probe-strand validation**: absent sites, reference-only records and multiallelic sites cannot be classified this way. It does not infer a vendor's probe orientation or correct mitochondrial complements. The default is `--strand-ambiguous keep`; the matching setting is `microarray_strand_ambiguous`.
- `--resolve-rsids` uses a bundled, compact [dbSNP build157 history](src/wgsextract_cli/assets/microarray/rsid/README.md) to match merged aliases in rsID-based vendor lookup and exclude withdrawn, ambiguous or unresolved identities. Vendor marker IDs, coordinates and row order remain unchanged; no alleles are flipped. Conflicting aliases, including an explicit quality no-call versus a called allele, remain no-calls. Unlisted IDs are **not** certified active. This does not replace an NCBI36 liftover chain or validate probe alleles. The default is disabled (`microarray_resolve_rsids = false`); `--no-resolve-rsids` explicitly overrides configuration. Call-policy options require a single sample. `--call-reasons` includes `strand_ambiguous`, `retired_rsid`, `ambiguous_rsid` and `unresolved_rsid` for native CombinedKit calls; additional vendor-projection conflicts are counted in the log.
- `vcf trio` takes the proband's, mother's and father's VCFs, or their BAM/CRAMs. `--proband` overrides other proband inputs; otherwise the usual `--vcf-input`/`--input` selection applies, including explicit `--input` taking precedence over a configured default VCF. Mixed chr/no-chr naming is normalized across primary, random, unplaced and ALT contigs; gVCFs follow the resolved reference's naming. From VCFs a parent's missing record or reference block (often GQ 0) can't show the parent lacks a variant, so de novo lists fill with inherited variants, and the command warns when that's likely. From BAM/CRAMs the child is called first and all three are then genotyped together from their reads at the child's variant positions, so each parent's depth and allele counts are known there. Samples are named by role, even when every file says `SM:WGSE` as WGS Extract v4 wrote. The proband's sex comes from chrX/chrY coverage (or `--proband-sex`), chrY and chrM aren't called, a de novo call needs `--min-depth` reads (default 15) in all three and no alt read in either parent, and `trio_denovo_isolated.vcf.gz` keeps the candidates with no other within 1 kb, since clusters are mostly misaligned reads in repeats. A 30x whole-genome trio takes about 70 minutes with 8 threads.
- `ref pharmgkb` builds `ref/pharmgkb_hg38.tsv.gz` from ClinPGx's (formerly PharmGKB) clinical annotations and variant locations; `vcf pharmgkb` labels the records that carry an annotated SNV allele. Star-allele/haplotype annotations (e.g. CYP2D6*4) and indels are not included, and it is hg38 only.
- `vcf pharmcat` calls star-allele diplotypes and drug-response phenotypes (CYP2C19, CYP2C9, DPYD, TPMT, ...) with [PharmCAT](https://pharmcat.org) and writes its prescribing report. PharmCAT isn't on conda: extract `pharmcat-pipeline-<version>.tar.gz` from its [releases](https://github.com/PharmGKB/PharmCAT/releases) into `<reference library>/pharmcat` (or set `pharmcat_directory`); `pixi install -e pharmcat` provides its runtime (macOS/Linux). Input must be GRCh38; gVCFs are handled. CYP2D6 and HLA can't be called from a VCF: pass the `<stem>.cyp2d6.pharmcat.tsv` and `<stem>.hla.pharmcat.tsv` files written by `typing cyp2d6` and `typing hla` with `--outside-call` (repeatable); they are merged into `<stem>.outside.tsv` in the output directory, which PharmCAT also picks up on later runs.
- `typing cyp2d6`, `typing hla`, and `typing str` need a whole-genome BAM/CRAM (about 30x) with an index. Their tools live in the `cyrius`, `t1k`, and `expansionhunter` Pixi environments (Linux and macOS; install each with `pixi install -e <environment>`). Cyrius is licensed for noncommercial use only. The first `typing hla` run builds T1K's allele index from IPD-IMGT/HLA into `<reference library>/ref/t1k_hla`. `typing str` compares repeat sizes with [STRchive](https://strchive.org)'s ranges; give `--sex` when known, otherwise it is inferred from read depth.
- `ref gnomad --release 4.1` streams gnomAD v4.1's native-GRCh38 exome and genome VCFs (about 880 GB, hours) into one allele-frequency-only table; the default 2.1.1 release is exome-only and lifted over to hg38.
- Reference downloads resume only explicit `.partial` files. Refreshing an existing complete file starts from byte zero and preserves it until the replacement finishes and passes available checksum verification, then replaces it atomically.
- `ref cadd` downloads CADD v1.7 (about 85 GB per build) and `ref gerp` the GERP++ bigWig; `vcf cadd` and `vcf gerp` add `CADD_RAW`/`CADD_PHRED` and `GERP` to a VCF.
- `sign create` writes a `SHA256SUMS` manifest that `sha256sum -c` also reads, signed with `ssh-keygen -Y sign` when `--key` is given; `sign verify` checks the signature against a trusted key, then re-hashes every file.
- `align` with FASTQs needs `--ref <FASTA>` when the reference library holds more than one genome, since reads don't say which genome they belong to.
- BWA and bwa-mem2 alignment use a fixed 100-million-base batch (`-K 100000000`) for reproducible results when `--threads` changes. This also applies to `realign`, `pet-align` and Ultima alignment; earlier releases used thread-dependent batches.
- Paired short-read `realign` streams newly extracted reads into BWA/bwa-mem2 without gzipped FASTQ intermediates. `--keep-fastq`, supplied/reused FASTQs, GPU and long-read workflows retain file handling. `--force-extract` also retains file handling when cached FASTQs exist at the exact output paths, refreshing them and preserving the existing success cleanup/failure retention; fresh forced extraction still streams. Streaming preserves the extracted pair order and decoded alignment records; the BAM/CRAM program header truthfully records interleaved stdin (`-p`). Collation still needs temporary disk/RAM alongside sorting, included conservatively in preflight. Failed streams leave no reusable FASTQs; use `--keep-fastq` when you need them for a retry.
- `align` with BWA on a reference with ALT contigs (hs38DH and other full GRCh38 sets) writes `<ref>.alt` listing them, which makes BWA ALT-aware. Without it, reads in the ~110 Mb those contigs repeat (HLA among them) get mapping quality 0 or land on the ALT copy, and callers lose their variants. WGS Extract v4 aligned this way; the variant callers, `microarray` and `vcf trio` warn when an input looks like it, and re-aligning (`realign`) recovers those regions.
- `align`, `realign` and `pet-align` use bwa-mem2 rather than bwa when it's installed (Linux, macOS and Windows) and the reference has a bwa-mem2 index that fits in memory: the same alignments in about three quarters of the time (192 s against 259 s for 4.2M read pairs on 12 threads). Its index needs 17 GB of memory for a human genome, against bwa's 5 GB, and building it ~28 bytes per base (~90 GB), so `ref index --aligner bwa-mem2` downloads hs38DH's (17 GB; nf-core's iGenomes publishes it for Broad's GRCh38, whose sequences are hs38DH's) and builds the index for other references only where there's the memory. `--aligner bwa` or `--aligner bwa-mem2` chooses explicitly.
- When bwa-mem2 is selected, sorting is capped at 256 MiB per thread to leave memory for its index. Smaller explicit `--memory` budgets remain smaller; BWA and other aligners keep the requested sort budget. This also applies to Ultima and pet alignment.
- Native short-read alignment checks currently available RAM on Linux, macOS and Windows, not just installed RAM. Its estimate includes resident index files, aligner working buffers, samblaster when used, per-thread sort memory plus overhead, and pipeline bookkeeping. Samblaster and transient headroom scale with **input read count**, not reference length. FASTQ counts come from file size and a bounded prefix sample (up to 4 MiB compressed/16 MiB decoded per mate; plain FASTQ, gzip and BGZF supported); `realign` uses indexed BAM/CRAM `idxstats` before extraction. Unreadable/unsupported samples or missing alignment indexes use a documented 680-million-read memory fallback, not a guarantee for larger inputs. Memory-component and per-read sort-spill estimates are logged; inadequate sort spill space warns for `align`, while `realign` retains its disk preflight refusal. Automatic selection falls back to BWA when the MEM2 pipeline will not fit; explicit MEM2 fails before alignment (and before `realign` extracts FASTQs). BWA only warns and continues. Free memory, lower `--memory`/`--threads`, or choose `--aligner bwa`; pagefile/swap capacity is not treated as available RAM.
- Full human-genome measurements (339.8 million read pairs) include a 17–17.5 GiB BWA working set with paged-out memory. At six threads and `--memory 256M`, the guard conservatively budgets about 18 GiB available for BWA and 29 GiB for MEM2, including transient headroom. Smaller inputs scale the read-dependent allowance down, while deeper or shorter-read inputs increase it even for the same reference. Counts extrapolated from a FASTQ prefix are approximate; changing read lengths/compressibility later in a file can affect accuracy. The capped resident peak alone is not the memory requirement.
- Native BWA alignment/realignment, BAM sorting/conversion, and SNP/indel/GATK calling print a compact memory/disk preflight with `OK`/`LOW` status before heavy processing. Disk budgets combine temporary files, outputs and indexes on each shared volume; existing refusal policies remain unchanged. The same read-only estimator exposes versioned JSON-compatible data for GUI integration; see [resource estimates and calibration](docs/resource_estimates.md).
- `info` reports how an alignment was made, from a sample of reads near chr1:20 Mb: whether duplicates were marked (and what share are duplicates), how many base-quality values the sequencer wrote (binned or full resolution), and whether the mapping was ALT-aware. The variant callers, `microarray` and `vcf trio` warn when an input is ALT-unaware or has no duplicates marked, as WGS Extract v4's alignments are; `realign` fixes both.
- `ref spliceai` downloads Ensembl's open GRCh38 SpliceAI scores (about 29 GB with its published index): every SNV in MANE Select transcripts, no indels. Illumina's full hg19 and hg38 sets sit behind a BaseSpace login, so hg19 needs a manual download; the command explains where to place it. SpliceAI scores are free for academic and not-for-profit use only.
- `vcf deepvariant` runs Google's DeepVariant from its Docker image on Linux and macOS (`docker pull google/deepvariant:1.10.0`; set `deepvariant_image` in `config.toml` for another version), or Google's `run_deepvariant` when it's on `PATH`, or Parabricks with `--gpu`. Docker and the image are checked, not installed. Bioconda's `deepvariant` package has contained no DeepVariant since 1.8, so there is no Pixi environment for it. Apple Silicon runs the x86-64 image under emulation, slowly.
- `vcf sv` and `vcf cnv` use Delly 2 (`delly sr` / `delly cnv`), which needs no mappability map; `vcf cnv --map` is accepted but ignored.
- `lineage y-haplogroup` writes Yleaf output to `<outdir>/yleaf/`, because Yleaf clears its output folder.
- `extract unmapped --fastq` also writes `_unmapped_R1.fastq.gz`, `_unmapped_R2.fastq.gz`, `_unmapped_SE.fastq.gz` (unpaired reads) and `_unmapped_singletons.fastq.gz` (mates absent from the extraction). Without the flag, only the BAM is written.
- `lineage y-haplogroup --tree-links` adds YFull/FTDNA links; `lineage mt-haplogroup --tree-links` adds the YFull mtDNA branch and FTDNA public mtDNA tree (use its branch search). Detailed info omits the uncomputed WES-depth row. Missing-reference errors identify the explicit library install command when the alignment matches a catalog model; references are never downloaded automatically.
- `pet-align` writes next to the R1 FASTQ unless `--outdir` is given.
- Ultima Genomics (UG 100) reads, as in MyHeritage's and Nebula/DNA360's WGS, are single-end (~300 bp) and err mostly in homopolymer lengths. Each read carries flow-space probabilities in `tp`/`t0` tags, and its read group has `PL:ULTIMA` and the flow order (`FO`). WGS Extract recognizes them from the read group (or `--platform ultima`, needed for FASTQs, whose names SRA replaces) and:
  - `align`/`realign` use BWA-MEM (`-Y`), as Ultima's and Broad's pipelines do. `tp`/`t0` stay attached to each read: they are reversed for reads that change strand, because `samtools reset` doesn't. Read groups (flow order) are kept. `realign` streams the BAM/CRAM into BWA without writing FASTQs.
  - Duplicates are marked with GATK's `MarkDuplicates --FLOW_MODE`, which needs a single-end read's start and end to match. Without GATK, `samtools markdup` (start only) is used and the command warns. `--gpu` is refused: Parabricks keeps no flow tags.
  - `vcf snp`/`indel` use bcftools' Ultima profile (`mpileup -X ultima`) and `bcftools call`'s default prior instead of `-P 0`, which more than halved indel precision on Ultima reads. bcftools still calls many false indels in homopolymers, so prefer DeepVariant (see the accuracy table). `vcf gatk` calls with `--flow-mode ADVANCED`, and `vcf deepvariant` with the `ULTIMA` model, from Google's experimental `google/deepvariant:gh1100-ultima` image (`deepvariant_ultima_image` in `config.toml`). `vcf freebayes` warns that it has no homopolymer error model.
  - `bam to-ubam` keeps `tp`, `t0`, `tm`, `rq` and `a3`, in read orientation.
  - Low-pass Ultima WGS (MyHeritage's is about 2x) is too shallow for reliable genotypes without imputation, which WGS Extract doesn't do.
- `vcf sv` uses Delly by default and pbsv/Sniffles with `--pacbio`; an explicit `--caller` is always respected, and `--pacbio --caller delly` is rejected.
- `vep download --mirror` accepts `uk` (default) or `ebi`.

### Variant calling accuracy

Scored against [Genome in a Bottle](https://www.nist.gov/programs-projects/genome-bottle)'s HG002 v4.2.1 truth set on chr20, inside its confident regions, with `rtg vcfeval` (genotypes must match). The reads are Google's 35x NovaSeq PCR-free HG002 chr20 reads, aligned to hs38DH with `align`. Each caller ran with `-r chr20` and 8 threads (DeepVariant with 4) on a 4-core Ryzen 5350GE.

| Caller | SNP precision | SNP recall | SNP F1 | Indel precision | Indel recall | Indel F1 | Time |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `vcf snp` + `vcf indel` (bcftools) | 0.974 | 0.993 | 0.984 | 0.949 | 0.949 | 0.949 | 6 min |
| `vcf freebayes` | 0.976 | 0.995 | 0.985 | 0.970 | 0.966 | 0.968 | 9.5 min |
| `vcf gatk` | 0.991 | 0.994 | 0.993 | 0.993 | 0.991 | 0.992 | 21 min |
| `vcf deepvariant` | 0.999 | 0.995 | **0.997** | 0.998 | 0.994 | **0.996** | 18.5 min |

**Ultima reads.** There's no GIAB truth set for an Ultima-sequenced sample with public reads. Instead, this compares against the same tissue (GIAB HG008-N-D) sequenced on Illumina: its 169x BAM downsampled to ~40x and called with bcftools. Both are scored on chr20 inside GIAB's "not in any difficult region" stratification (50.8 Mb). The setup is otherwise the same as above, without rtg (alleles and genotype class must match).

The Ultima reads are Ultima's GIAB CRAM, downsampled to ~30x and realigned to hs38DH's chr20 with `realign`. Each caller ran with 2–3 threads on a busy shared host. GATK's flow mode is slow, about 1 Mb per minute (its flow likelihood engine is Java only).

| Caller on Ultima reads | SNP precision | SNP recall | SNP F1 | Indel precision | Indel recall | Indel F1 |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: |
| `vcf snp` + `vcf indel` (bcftools, `-X ultima`) | 0.998 | 0.994 | 0.996 | 0.354 | 0.984 | 0.520 |
| `vcf gatk` (`--flow-mode ADVANCED`) | 0.985 | 0.996 | 0.991 | 0.396 | 0.971 | 0.563 |
| `vcf deepvariant` (ULTIMA model) | 0.997 | 0.994 | **0.995** | 0.969 | 0.950 | **0.959** |

bcftools and GATK call extra indels in homopolymers, Ultima's main error mode, so use DeepVariant for Ultima indels.

For flow-aware Ultima input, `vcf gatk --scatter` optionally runs independent
HaplotypeCaller processes over nonoverlapping intervals, then gathers in reference
order. It is **off by default**: annotations and genotypes can change, including
far from interval boundaries. Separate processes reset GATK's random state
(used by annotation jitter and read downsampling), as well as assembly context.
Concurrency is capped by `--threads` and available physical RAM, allowing 6 GiB per
worker with an explicit 4 GiB Java heap. If only one worker fits, the original
unscattered path is used. `--memory` remains the shared sort-memory setting, not a
GATK heap setting. Scattering requires bcftools and tabix in addition to GATK and
samtools, and cannot be combined with `--gpu`.

In a functional comparison on the original GIABv3-mapped public HG008 Ultima
chr20 reads, downsampled to nominal 30x (7,012,718 primary reads), GATK 4.6.2.0
with two workers retained the same **131,299 sites**, but **85 records and two
genotypes differed**. Both genotype changes were `0/1` → `1/1` at
chr20:47,895,004 and chr20:47,895,025. All differences were 1.91–32.08 Mb from
the split at chr20:32,222,084, not boundary-local; this is **not equivalence**.
One run took 57.2 minutes unscattered versus 37.5 minutes scattered, including
CRAM preparation; this is not a controlled throughput benchmark or an accuracy
comparison. A separate 200 kb realigned-read smoke was byte-identical across
395 decoded records, but slower with scattering because of startup overhead.

### Pixi environments

Some tools live in their own Pixi environment. Install optional environments explicitly with `pixi install --locked -e <environment>` from the app directory (`wgsextract-cli/app/`) or a source checkout before using them. The CLI never installs dependencies at runtime; missing tools report the exact install command, including the app's manifest path so it works from any directory. Installed tools not on `PATH` are launched through `pixi run --no-install -e <environment>`. Cyrius is licensed for noncommercial use only. You can also run a whole command inside an installed environment:

| Environment | Used for |
| :--- | :--- |
| `default` | All standard commands |
| `pacbio` | `pbmm2`, `pbsv`, `sniffles`: `pixi run -e pacbio wgsextract vcf sv --pacbio` |
| `vep` | `pixi run -e vep wgsextract vep run` (not native Windows) |
| `cyrius`, `t1k`, `expansionhunter` | `typing cyp2d6`, `typing hla`, `typing str` (Linux and macOS) |
| `pharmcat` | PharmCAT's Python runtime for `vcf pharmcat` (Linux and macOS) |
| `dev` | Contributor tooling (pytest, ruff, mypy); selected automatically by the development tasks below |

### NVIDIA GPUs (Parabricks)

With an NVIDIA GPU, `--gpu` runs the slow steps with [NVIDIA Parabricks](https://docs.nvidia.com/clara/parabricks/), which gives BWA-MEM, DeepVariant and GATK-equivalent results:

| Command | With `--gpu` |
| :--- | :--- |
| `align`, `realign` (short reads) | BWA-MEM, sorting and duplicate marking (`pbrun fq2bam`), with the same index, `.alt` file and read group as `--aligner bwa` |
| `align`, `realign` (HiFi, ONT) | minimap2 (`map-hifi`, `map-ont`; `map-pbmm2` for `--aligner pbmm2`). PacBio CLR has no GPU preset |
| `vcf deepvariant` | DeepVariant's WGS, WES and PACBIO models (not `--checkpoint`, the hybrid model or Ultima reads, which `align`/`realign` also refuse with `--gpu`) |
| `vcf gatk` | HaplotypeCaller |

On an RTX 3090 (24 GB), a 30x genome aligns in 40-50 minutes, against 3.5-4 hours for BWA on that machine's 12 CPU threads. DeepVariant and HaplotypeCaller take about a minute for chr20.

It needs Linux (WSL2 should work but is untested), an NVIDIA GPU with 16 GB or more, Docker with the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/), and the image (`docker pull nvcr.io/nvidia/clara/clara-parabricks:4.8.0-1`; set `parabricks_image` in `config.toml` for another version). The CLI checks each of these and says what's missing; it doesn't install them. Reads, outputs and the reference are mounted into the container at their own paths, and the tools run as you. Parabricks can't write CRAM against a gzipped reference, so then it writes BAM and samtools converts it.

## Configuration

Settings live in `config.toml`. Run `wgsextract config` to see its path and every active value.

- macOS: `~/.config/wgsextract/config.toml` if `~/.config/` exists, otherwise `~/Library/Application Support/wgsextract/config.toml`
- Linux: `~/.config/wgsextract/config.toml`
- Windows: `%LocalAppData%\theontho\wgsextract\config.toml`

`--config FILE` (accepted anywhere on the command line) reads and saves settings in another file instead, so a tool that bundles the CLI, such as the GUI, keeps its settings out of your own config.

```toml
# Defaults for --input and --outdir
input = "/data/genomes/joe/joe.cram"
outdir = "/data/wgse-out"

# Reference genome FASTA (same as --ref)
ref = "/data/reference/genomes/hs38.fa.gz"

# Reference library directory (default: reference/ in the app directory)
reflib = "/data/reference"

# One subfolder per person/sample, selected with --genome
genome_library = "/data/genome-library"

# Resources: a number, or "auto"
threads = "auto"   # Apple Silicon performance cores, otherwise all cores
memory = "auto"    # per thread, e.g. "2G"; auto uses 25% of RAM split across threads

# External tool overrides (optional when the tools are on PATH)
yleaf_path = "/path/to/yleaf"
haplogrep_path = "/path/to/haplogrep"
vep_cache = "~/.vep"
```

The short keys above are aliases; `wgsextract config` shows the canonical names (`input_path`, `output_directory`, `reference_fasta`, `reference_library`, `cpu_threads`, `memory_limit`, `yleaf_executable`, and so on). Either form works. Command-line options override config values for a single run. Other settings cover annotation data paths (`clinvar_vcf_path`, `gnomad_vcf_path`, `revel_tsv_path`, ...), default trio VCFs (`mother_vcf`, `father_vcf`), and the developer settings listed under [Development](#development).

### Keep-awake

Add `--keep-awake` to long commands, or set `keep_awake = true` in `config.toml`, to request idle/sleep inhibition until the command exits. `--no-keep-awake` overrides the config, including delegated analyze, annotation-chain and benchmark commands; `keep_awake = false` disables it persistently (the default). macOS uses `caffeinate`, Linux uses `systemd-inhibit` if already present and permitted, and native Windows uses `SetThreadExecutionState`. Missing helpers, denied permissions and cleanup failures never fail the run. This prevents automatic sleep, not shutdown or a forced/manual suspend; display sleep remains allowed. Report-only `help`/`config`/`deps` never inhibit sleep.

### Genome library

Set `genome_library` to a directory with one subfolder per person or sample. The subfolder name is the `--genome` ID:

```text
/data/genome-library/
  joe/
    joe.cram
    joe.vcf.gz
    raw-fastqs/joe_R1.fastq.gz, joe_R2.fastq.gz
  ken mcdonald/
    bam files/sample.bam
```

With `--genome <id>`, the CLI finds inputs in that folder (recursively) and writes outputs there unless `--outdir` is given. It creates a `genome-config.toml` in the folder. If several BAM/CRAM files, VCFs, or FASTQ sets match, the command fails instead of guessing; pick the files in `genome-config.toml`:

```toml
alignment = "bam files/sample.bam"
vcf = "variants/sample.vcf.gz"
fastq_r1 = "raw-fastqs/sample_R1.fastq.gz"
fastq_r2 = "raw-fastqs/sample_R2.fastq.gz"
```

```bash
wgsextract --genome joe info
wgsextract --genome "ken mcdonald" microarray --formats 23andme_v5
wgsextract --genome joe vcf filter --expr 'QUAL>30'
```

### Example genomes

`example-genome` downloads curated 1000 Genomes and HGSVC2 datasets into the genome library. Some are very large, so check with `--dry-run` first. Interrupted downloads resume; `--force` starts over.

```bash
wgsextract example-genome list
wgsextract example-genome download hgsvc2-hg00733-pacbio-hifi-bam --dry-run
wgsextract example-genome download hgsvc2-hg00733-pacbio-hifi-bam

pixi run -e pacbio wgsextract --genome test-1000genomes/hgsvc2-hg00733-pacbio-hifi-bam align --platform hifi --ref /path/to/hs38.fa
pixi run -e pacbio wgsextract --genome test-1000genomes/hgsvc2-hg00733-pacbio-hifi-bam vcf sv --pacbio --ref /path/to/hs38.fa
```

## Development

```bash
git clone https://github.com/theontho/wgsextract-cli.git
cd wgsextract-cli
pixi install
pixi run wgsextract --help
```

Use `pathlib.Path` for filesystem operations. Shared path helpers accept strings
and `os.PathLike[str]` (the `PathInput` alias in `core.paths`); keep path objects
while manipulating paths and convert at string-based CLI, configuration, tool
wrapper, and public return-value boundaries. Use `input_stem` / `fasta_prefix`
for compound genomic extensions and `append_suffix` for sidecars such as
`.vcf.gz.tbi` and `.cram.crai`. `absolute_path` normalizes `..` without following
symlinks; use `Path.resolve()` when comparing real targets. Wrapper commands and samtools'
`out##idx##index` arguments must remain strings; native filesystem paths must
not be converted to POSIX spelling for Windows.

Developer tools live in the `dev` Pixi environment, which Pixi selects automatically for these tasks:

| Task | What it runs |
| :--- | :--- |
| `pixi run test` | Unit and integration tests (excludes the end-to-end suites) |
| `pixi run smoke-test` | CLI smoke tests (`smoke-test-full` adds full-genome and pet tests) |
| `pixi run test-e2e` | End-to-end suites against real data |
| `pixi run test-e2e-fake` | End-to-end suites against generated data (`pytest --fake-data`); CI runs them on Linux and macOS |
| `pixi run test-remote` | Live checks that every file the CLI downloads is still published, SpliceAI is in the expected format, and a microarray kit made from public 1000 Genomes data (HG00096) matches its real Omni chip; CI runs them weekly |
| `pixi run lint` | Ruff, site-source validation, and GitHub Actions checks |
| `pixi run format` | `ruff format .` |
| `pixi run typecheck` | mypy on `src/wgsextract_cli` |
| `pixi run site-build` | Rebuild the documentation site (see [docs/site_generation.md](docs/site_generation.md)) |
| `pixi run benchmark-dataset` | Rebuild the release-backed mini-genome benchmark dataset |
| `pixi run stats` | Code statistics (needs [cloc](https://github.com/AlDanial/cloc) installed separately) |

If you have [just](https://github.com/casey/just) installed, the `justfile` offers short aliases for these tasks: `just run <args>` runs the CLI, and `just test`, `just lint`, `just fmt`, `just typecheck` and `just check` (all four) run the checks. Run `just` to list every recipe.

Real-data tests read their inputs from pytest options or from config:

```bash
pixi run pytest tests/test_smoke_real_basics.py \
  --real-input /data/HG002.cram --real-ref /data/reference/hs38.fa --real-region chr20
```

Options include `--real-input`, `--real-ref`, `--real-vcf`, `--real-region`, `--real-targets`, `--real-fastq-r1`/`--real-fastq-r2`, `--fake-data`, and `--full-data` (see `tests/conftest.py`). Alternatively, set `real_genome_test_path` and `real_genome_tests_enabled = true` in `config.toml`. Setting `dev_download_cache = true` caches downloads under `$XDG_CACHE_HOME/wgsextract/downloads` (`~/.cache/wgsextract/downloads` by default) for reuse across test runs, pruned after `dev_download_cache_ttl_seconds` (default one week).

`wgsextract qc fake-data` generates synthetic BAM/CRAM/VCF/FASTQ files for tests and bug reports; see the [workflows page](https://theontho.github.io/wgsextract-cli/workflows.html#testing).

To cut a release, and get it into the WGS Extract app, see [docs/releasing.md](docs/releasing.md).

## License

Distributed under the GPL-3.0 License. See `LICENSE`.
