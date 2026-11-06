"""Examples and outputs for every runnable command (the help contract).

Each command's ``--help`` ends with these, ``wgsextract help --all --json``
and docs/commands.md publish them, and tests/test_cli_help.py checks that
every runnable command has an entry and that every example parses.

``outputs`` names the files a command writes, which go to the output
directory (--outdir, the configured outdir or the --genome folder) unless the
text says otherwise. A run that registers outputs also writes one
``.manifest.json`` beside its main output; that isn't repeated here.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class CommandHelp:
    examples: tuple[str, ...]
    outputs: str


def _h(outputs: str, *examples: str) -> CommandHelp:
    return CommandHelp(examples, outputs)


# From the curated example-genome catalog (_examples_catalog.EXAMPLES); a test
# checks the 'example-genome' examples name real IDs and tags.
_STARTER_EXAMPLE = "na12878-lowcov-bam"
_PACBIO_TAG = "pacbio"

_VCF_IN = "--vcf-input sample.vcf.gz"
_ANNOTATED = "{name}_annotated.vcf.gz (+ .tbi)"
_SCORED = _ANNOTATED + "; with --min-score N, also {name}_gt_N.vcf.gz."
_LIBRARY = "Into the reference library (reflib)"

COMMAND_HELP: dict[str, CommandHelp] = {
    "help": _h(
        "Prints to stdout; writes no files.",
        "wgsextract help --all",
        "wgsextract help vcf snp",
    ),
    "config": _h(
        "Prints the settings; in a terminal, offers to create a default config.toml.",
        "wgsextract config",
    ),
    # info
    "info": _h(
        "Prints a summary and caches it as <input file name>.wgse_info.json; "
        "with --format also <input file name>_chromosomes and _qc (QC stats) "
        "as .tsv or .parquet.",
        "wgsextract info --input sample.cram",
        "wgsextract info --input sample.cram --detailed",
        "wgsextract info --input sample.cram --format parquet",
    ),
    "info calculate-coverage": _h(
        "<input file name>_coverage.tsv, or with --bins <input file name>"
        "_coverage_bins.tsv (depth bands, or .parquet with --format parquet) "
        "instead. With --by, <input>_<bed>_target_coverage.tsv (depth per "
        "target) and <input>_<bed>_target_summary.txt (on- and off-target "
        "depth, enrichment and targets below --min-depth).",
        "wgsextract info calculate-coverage --input sample.cram",
        "wgsextract info calculate-coverage --input sample.cram --bins --format parquet",
        "wgsextract info calculate-coverage --input sample.bam --by panel.bed --min-depth 20",
    ),
    "info coverage-sample": _h(
        "<input file name>_samplecvg.json; a whole-file run also updates the "
        "info cache.",
        "wgsextract info coverage-sample --input sample.cram",
    ),
    "info chip": _h(
        "Prints the vendor, chip and matching --formats template; writes no files.",
        "wgsextract info chip --input genome_raw_data.txt",
        "wgsextract info chip --list",
    ),
    # sample workspace
    "sample show": _h(
        "Prints the sample's overrides, recorded facts and results; writes no files.",
        "wgsextract sample show --genome HG002",
        "wgsextract sample show --input sample.cram --json",
    ),
    "sample set": _h(
        "wgsextract.sample.toml in the sample folder (the --genome folder, "
        "--workspace or the input's folder).",
        "wgsextract sample set --genome HG002 --sex Female",
        "wgsextract sample set --input sample.cram --platform ont",
        "wgsextract sample set --input ~/Downloads/mom.bam --sex Female",
    ),
    "sample unset": _h(
        "wgsextract.sample.toml in the sample folder.",
        "wgsextract sample unset --genome HG002 --sex",
        "wgsextract sample unset --input sample.cram --platform",
    ),
    # microarray / lineage
    "microarray": _h(
        "<input stem>_<format>.txt (or the vendor's extension) per format, "
        "such as sample_23andMe_V5.txt, and <input stem>_CombinedKit.txt; with "
        "--format also <input stem>_genotypes.tsv or .parquet.",
        "wgsextract microarray --input sample.cram --formats 23andme_v5",
        "wgsextract microarray --input sample.cram --formats 23andme_v5,ancestry_v2",
    ),
    "lineage y-haplogroup": _h(
        "yleaf/: hg_prediction.hg, the per-sample .out/.fmf/.info reports and "
        "run.json; prints the haplogroup and the tree's name and version.",
        "wgsextract lineage y-haplogroup --input sample.cram",
    ),
    "lineage y-str": _h(
        "ystr/: <sample>.ystr.tsv, <sample>.ftdna.csv (FTDNA marker order) and "
        "run.json.",
        "wgsextract lineage y-str --input sample.cram --panel Y-37",
    ),
    "lineage mt-haplogroup": _h(
        "haplogrep_results.txt; prints the mtDNA haplogroup and Haplogrep 3 tree. "
        "With --contamination, also haplocheck_contamination.txt (+ .raw.txt, "
        ".html).",
        "wgsextract lineage mt-haplogroup --input sample.cram",
        "wgsextract lineage mt-haplogroup --input sample.cram --contamination",
        "wgsextract lineage mt-haplogroup --input sample.vcf.gz --tree mitotree-rcrs",
    ),
    # extract
    "extract mito-fasta": _h(
        "<input stem>_MT.fasta: the mtDNA consensus sequence.",
        "wgsextract extract mito-fasta --input sample.cram",
    ),
    "extract mito-vcf": _h(
        "<input stem>_MT.vcf.gz (+ .tbi).",
        "wgsextract extract mito-vcf --input sample.cram",
    ),
    "extract mt-bam": _h(
        "<input stem>_mtDNA.bam (+ .bai).",
        "wgsextract extract mt-bam --input sample.cram",
    ),
    "extract bam-subset": _h(
        "<input stem>_subset.bam (+ .bai).",
        "wgsextract extract bam-subset --input sample.cram --fraction 0.1",
    ),
    "extract ydna-bam": _h(
        "<input stem>_Y.bam (+ .bai).",
        "wgsextract extract ydna-bam --input sample.cram",
    ),
    "extract ydna-vcf": _h(
        "<input stem>_Y.vcf.gz (+ .tbi).",
        "wgsextract extract ydna-vcf --input sample.cram",
    ),
    "extract y-mt-extract": _h(
        "<input stem>_Y_MT.bam (+ .bai).",
        "wgsextract extract y-mt-extract --input sample.cram",
    ),
    "extract unmapped": _h(
        "<input stem>_unmapped.bam (+ .bai); FASTQ files with --fastq.",
        "wgsextract extract unmapped --input sample.cram",
        "wgsextract extract unmapped --input sample.cram --fastq",
    ),
    "extract custom": _h(
        "<input stem>_<region>.bam (+ .bai).",
        "wgsextract extract custom --input sample.cram --region chr17:43044295-43125483",
        "wgsextract extract custom --input sample.cram --gene BRCA1",
    ),
    # realign / align
    "realign": _h(
        "<input stem>_aligned.bam or .cram (+ index); with --keep-fastq, also "
        "<input stem>_R1/_R2.fastq.gz (single-end ONT/PacBio: <input "
        "stem>.fastq.gz). Completed steps are recorded in "
        "<output>.steps.json, so a rerun resumes after a crash.",
        "wgsextract realign --input sample_hg19.bam --ref hs38.fa",
        "wgsextract realign --input sample_hg19.bam --ref hs38.fa --dry-run",
    ),
    "basecall": _h(
        "<POD5 file or folder stem>.<model>[.<modifications>][.subset].bam: "
        "unaligned reads with Dorado's tags (MM/ML with --modified-bases), "
        "beside the POD5 input or in --outdir.",
        "wgsextract basecall --input pod5/ --modified-bases 5mCG_5hmCG",
        "wgsextract basecall --input pod5/ --model sup --regions-bed panel.bed "
        "--alignment sample_aligned.bam",
        "wgsextract basecall --input reads.pod5 --device cpu --model fast",
    ),
    "align": _h(
        "<reads stem>_aligned.bam or .cram (+ index).",
        "wgsextract align --r1 sample_R1.fastq.gz --r2 sample_R2.fastq.gz --ref hs38.fa",
        "wgsextract align --r1 hifi_reads.fastq.gz --preset map-hifi --ref hs38.fa",
    ),
    # vcf: calling
    "vcf snp": _h(
        "snps.vcf.gz (+ .tbi).",
        "wgsextract vcf snp --input sample.cram",
        "wgsextract vcf snp --input sample.cram --region chrM",
    ),
    "vcf indel": _h(
        "indels.vcf.gz (+ .tbi).",
        "wgsextract vcf indel --input sample.cram",
    ),
    "vcf freebayes": _h(
        "freebayes.vcf.gz (+ .tbi).",
        "wgsextract vcf freebayes --input sample.cram --region chr20",
    ),
    "vcf gatk": _h(
        "gatk.vcf.gz (+ .tbi).",
        "wgsextract vcf gatk --input sample.cram",
    ),
    "vcf deepvariant": _h(
        "deepvariant.vcf.gz (+ .tbi) and deepvariant.vcf.gz.steps.json; until "
        "it finishes, the stages' work in .deepvariant.work, where a rerun "
        "resumes.",
        "wgsextract vcf deepvariant --input sample.cram",
        "wgsextract vcf deepvariant --input hifi.bam --pacbio",
        "wgsextract vcf deepvariant --input sample.cram --force-step call_variants",
    ),
    "vcf clair3": _h(
        "clair3.vcf.gz (+ .tbi).",
        "wgsextract vcf clair3 --input ont.bam",
    ),
    "vcf phase": _h(
        "phased.vcf.gz (+ .tbi) and phased.blocks.tsv; with --haplotag, "
        "haplotagged.bam or .cram (+ index).",
        "wgsextract vcf phase --input ont.bam --vcf-input clair3.vcf.gz",
        "wgsextract vcf phase --input hifi.bam --vcf-input calls.vcf.gz --haplotag",
    ),
    "vcf sv": _h(
        "sv.vcf.gz (Delly), pbsv.vcf.gz or sniffles.vcf.gz (+ .tbi).",
        "wgsextract vcf sv --input sample.cram",
        "wgsextract vcf sv --input hifi.bam --pacbio",
    ),
    "vcf cnv": _h(
        "cnv.vcf.gz (+ .tbi).",
        "wgsextract vcf cnv --input sample.cram",
    ),
    # vcf: annotation, filtering and analysis
    "vcf annotate": _h(
        "annotated.vcf.gz (+ .tbi).",
        f"wgsextract vcf annotate {_VCF_IN} --ann-vcf dbsnp.vcf.gz --cols ID",
    ),
    "vcf filter": _h(
        "filtered.vcf.gz (+ .tbi).",
        f"wgsextract vcf filter {_VCF_IN} --expr 'QUAL>30'",
        f"wgsextract vcf filter {_VCF_IN} --gene BRCA1",
    ),
    "vcf table": _h(
        "<input stem>_variants.tsv (or .parquet with --format parquet) and "
        "<input stem>_variants.sqlite: one row per variant allele, with its "
        "annotations.",
        f"wgsextract vcf table {_VCF_IN}",
        "wgsextract vcf table --vcf-input clinvar_annotated.vcf.gz --region chr22",
        "wgsextract vcf table --vcf-input clinvar_annotated.vcf.gz --format parquet",
    ),
    "vcf clinvar": _h(
        "clinvar_annotated.vcf.gz and clinvar_pathogenic.vcf.gz (+ .tbi).",
        f"wgsextract vcf clinvar {_VCF_IN}",
    ),
    "vcf gnomad": _h(
        _ANNOTATED.format(name="gnomad")
        + "; with --max-af AF, also gnomad_af_lt_AF.vcf.gz and its "
        ".confidence.tsv table.",
        f"wgsextract vcf gnomad {_VCF_IN} --max-af 0.01",
    ),
    "vcf revel": _h(
        _SCORED.format(name="revel"),
        f"wgsextract vcf revel {_VCF_IN}",
    ),
    "vcf pharmgkb": _h(
        _ANNOTATED.format(name="pharmgkb"),
        f"wgsextract vcf pharmgkb {_VCF_IN}",
    ),
    "vcf phylop": _h(
        _SCORED.format(name="phylop"),
        f"wgsextract vcf phylop {_VCF_IN}",
    ),
    "vcf cadd": _h(
        _SCORED.format(name="cadd"),
        f"wgsextract vcf cadd {_VCF_IN} --min-score 20",
    ),
    "vcf gerp": _h(
        _SCORED.format(name="gerp"),
        f"wgsextract vcf gerp {_VCF_IN}",
    ),
    "vcf spliceai": _h(
        _ANNOTATED.format(name="spliceai"),
        f"wgsextract vcf spliceai {_VCF_IN}",
    ),
    "vcf alphamissense": _h(
        _SCORED.format(name="alphamissense"),
        f"wgsextract vcf alphamissense {_VCF_IN}",
    ),
    "vcf chain-annotate": _h(
        "chain_annotated.vcf.gz (+ .tbi).",
        f"wgsextract vcf chain-annotate {_VCF_IN} --annotations clinvar,gnomad",
    ),
    "vcf pharmcat": _h(
        "<stem>.report.html and <stem>.phenotype.json from PharmCAT.",
        f"wgsextract vcf pharmcat {_VCF_IN}",
    ),
    "vcf trio": _h(
        "trio_<mode>.vcf.gz (+ .tbi); from alignments, also trio_joint.vcf.gz "
        "(and trio_denovo_isolated.vcf.gz for --mode denovo). The manifest is "
        "trio.manifest.json.",
        "wgsextract vcf trio --proband child.vcf.gz --mother mother.vcf.gz "
        "--father father.vcf.gz --mode denovo",
    ),
    "vcf to-fasta": _h(
        "<input stem>_consensus.fa.gz (+ .fai); with --chain, a .chain file.",
        f"wgsextract vcf to-fasta {_VCF_IN} --ref hs38.fa",
    ),
    # vep
    "vep": _h(
        "Same as 'vep run'.",
        "wgsextract vep --input sample.vcf.gz",
    ),
    "vep run": _h(
        "<input stem>_vep.vcf.gz (or .tsv/.json with --format), with VEP's summary.",
        "wgsextract vep run --input sample.vcf.gz",
        "wgsextract vep run --input sample.vcf.gz --assembly GRCh37 --format tab",
    ),
    "vep download": _h(
        "Into the VEP cache directory (vep_cache, or --vep-cache).",
        "wgsextract vep download --assembly GRCh38",
    ),
    "vep verify": _h(
        "Prints what is missing or damaged; writes no files.",
        "wgsextract vep verify --assembly GRCh38",
    ),
    # setup: ref, deps, example-genome
    "ref bootstrap": _h(
        f"{_LIBRARY}: the bootstrap VCFs, liftover chains and other small files.",
        "wgsextract ref bootstrap",
    ),
    "ref library": _h(
        f"{_LIBRARY}: the chosen reference genome and its indexes.",
        "wgsextract ref library --list",
        "wgsextract ref library --install hs38",
    ),
    "ref library-list": _h(
        "Prints the installed and available assets; writes no files.",
        "wgsextract ref library-list",
    ),
    "ref status": _h(
        "Prints the reference library's status; writes no files.",
        "wgsextract ref status",
    ),
    "ref download": _h(
        "The FASTA at --out.",
        "wgsextract ref download --url https://example.org/genome.fa.gz "
        "--out genome.fa.gz",
    ),
    "ref index": _h(
        "Beside the FASTA: .fai, .dict and the aligner's index files.",
        "wgsextract ref index --ref hs38.fa",
        "wgsextract ref index --ref hs38.fa --aligner bwa-mem2",
    ),
    "ref verify": _h(
        "Prints what is missing or damaged; creates a missing .fai (and .gzi for "
        "a compressed FASTA) beside the FASTA.",
        "wgsextract ref verify --ref hs38.fa",
    ),
    "ref count-ns": _h(
        "<reference stem>_ncnt.csv and <reference stem>_nbin.csv.",
        "wgsextract ref count-ns --ref hs38.fa",
    ),
    "ref gene-map": _h(
        f"{_LIBRARY}: the hg19 and hg38 gene maps (--delete removes them).",
        "wgsextract ref gene-map",
    ),
    "ref clinvar": _h(
        f"{_LIBRARY}: ClinVar VCFs for hg19 and hg38.",
        "wgsextract ref clinvar",
    ),
    "ref gnomad": _h(
        f"{_LIBRARY}: gnomAD allele-frequency files.",
        "wgsextract ref gnomad",
    ),
    "ref revel": _h(f"{_LIBRARY}: REVEL score files.", "wgsextract ref revel"),
    "ref pharmgkb": _h(
        f"{_LIBRARY}: PharmGKB annotation files.", "wgsextract ref pharmgkb"
    ),
    "ref phylop": _h(
        f"{_LIBRARY}: the PhyloP bigWig.", "wgsextract ref phylop --build hg38"
    ),
    "ref cadd": _h(
        f"{_LIBRARY}: CADD score files and tabix indexes.",
        "wgsextract ref cadd --build hg38",
    ),
    "ref gerp": _h(
        f"{_LIBRARY}: the GERP++ bigWig.", "wgsextract ref gerp --build hg38"
    ),
    "ref spliceai": _h(f"{_LIBRARY}: SpliceAI score files.", "wgsextract ref spliceai"),
    "ref alphamissense": _h(
        f"{_LIBRARY}: AlphaMissense score files.", "wgsextract ref alphamissense"
    ),
    "ref aadr": _h(
        f"{_LIBRARY}: the AADR 2M SNP panel.", "wgsextract ref aadr --install"
    ),
    "ref ybrowse": _h(
        f"{_LIBRARY}: the yBrowse GRCh38 SNP sites.",
        "wgsextract ref ybrowse --install",
    ),
    "deps check": _h(
        "Prints each tool's status; writes no files.",
        "wgsextract deps check",
        "wgsextract deps check --tool samtools",
    ),
    "panel bed": _h(
        "panel_<presets>[_<genes>]_<build>_pad<N>kb.bed, the genes named when "
        "up to three, else <N>genes-<digest> (BED4: contig, start, end, genes; "
        "contig names from --ref when given).",
        "wgsextract panel bed --preset pharmacogenes",
        "wgsextract panel bed --genes CYP2D6,HLA-A,HLA-B --pad-kb 50 --ref hg38.fa",
        "wgsextract panel bed --preset hla --preset cardiac --build t2t",
        "wgsextract panel bed --gene-file my_genes.txt -o panels",
        "wgsextract panel bed --list-presets",
    ),
    "report": _h(
        "<sample>_personal_report.html, .md and .json and <sample>_personal_report_findings.tsv "
        "(or .parquet with --format parquet) in the sample folder (or --outdir); "
        "evidence links are relative to the sample folder.",
        "wgsextract report --genome HG002",
        "wgsextract report --input sample.cram",
        "wgsextract report --genome HG002 --gene-list acmg-sf-3.2",
        "wgsextract report --genome HG002 --gene-list my_genes.bed",
        "wgsextract report --workspace HG002 --results ~/wgs-out/HG002 --format parquet",
    ),
    "report-issue": _h(
        "Prints a redacted bug report and its GitHub link and saves the "
        "report as issue-report-<time>.md in state/reports (setting "
        "state_directory); opens the browser only after you agree or with --open.",
        "wgsextract report-issue",
        "wgsextract report-issue --last-failed --print",
        "wgsextract report-issue --run out/sample.vcf.gz.manifest.json --open",
        "wgsextract report-issue --title 'vcf snp hangs' --description 'Stops at 40%.'",
    ),
    "serve": _h(
        "Prints the server's address and token, also saved to "
        "state/server/server.json (setting state_directory) for you only; "
        "jobs and their logs go in state/server/jobs. Each operation writes "
        "its usual outputs and result manifest.",
        "wgsextract serve --allow ~/wgs",
        "wgsextract serve --port 8765 --max-jobs 2",
        "wgsextract serve --mcp --allow ~/wgs",
        "wgsextract serve --openapi",
    ),
    "example-genome list": _h(
        "Prints the curated examples; writes no files.",
        "wgsextract example-genome list",
        f"wgsextract example-genome list --tag {_PACBIO_TAG}",
    ),
    "example-genome download": _h(
        "Into <genome_library>/test-1000genomes/<example id>/, with a genome "
        "config file.",
        f"wgsextract example-genome download {_STARTER_EXAMPLE}",
        f"wgsextract example-genome download {_STARTER_EXAMPLE} --dry-run",
    ),
    # advanced: bam
    "bam identify": _h(
        "Prints the reference the file was aligned to; writes no files.",
        "wgsextract bam identify --input sample.bam",
    ),
    "bam sort": _h(
        "<input stem>_sorted.bam or .cram (+ index).",
        "wgsextract bam sort --input sample.bam",
    ),
    "bam index": _h(
        "Beside the input: <input>.bai or .crai.",
        "wgsextract bam index --input sample.bam",
    ),
    "bam unindex": _h(
        "Deletes the input's .bai/.crai index; writes no files.",
        "wgsextract bam unindex --input sample.bam",
    ),
    "bam unsort": _h(
        "<input stem>_unsorted.bam or .cram.",
        "wgsextract bam unsort --input sample.bam",
    ),
    "bam to-cram": _h(
        "<input stem>.cram (+ .crai).",
        "wgsextract bam to-cram --input sample.bam --ref hs38.fa",
    ),
    "bam to-bam": _h(
        "<input stem>.bam (+ .bai).",
        "wgsextract bam to-bam --input sample.cram",
    ),
    "bam unalign": _h(
        "The FASTQ files named by --r1/--r2 (or --se).",
        "wgsextract bam unalign --input sample.cram --r1 sample_R1.fastq.gz "
        "--r2 sample_R2.fastq.gz",
    ),
    "bam merge": _h(
        "merged.bam or merged.cram (+ index), or the --output path; each run "
        "keeps its own read group under one sample name (SM).",
        "wgsextract bam merge lane1.bam lane2.bam --output sample.bam",
        "wgsextract bam merge run1.bam run2.bam --sample HG002 --output HG002.bam",
    ),
    "bam to-ubam": _h(
        "<input stem>_unaligned.bam: queryname-sorted unaligned reads.",
        "wgsextract bam to-ubam --input sample.cram",
        "wgsextract bam to-ubam --r1 sample_R1.fastq.gz --r2 sample_R2.fastq.gz "
        "--sample NA12878",
    ),
    # advanced: qc, repair
    "qc fastqc": _h(
        "FastQC's <input>_fastqc.html and .zip.",
        "wgsextract qc fastqc --input sample_R1.fastq.gz",
    ),
    "qc fastp": _h(
        "<stem>_fp_1.fastq.gz (and _fp_2), <stem>_fastp.html and <stem>_fastp.json.",
        "wgsextract qc fastp --r1 sample_R1.fastq.gz --r2 sample_R2.fastq.gz",
    ),
    "qc multiqc": _h(
        "multiqc_report.html, in --outdir, else in the --input directory (or "
        "beside an --input file).",
        "wgsextract qc multiqc --input fastqc_reports/",
    ),
    "qc multiqc-metrics": _h(
        "wgsextract_*_mqc.json MultiQC custom-content files, in --outdir, else "
        "in the --input directory (or beside an --input file).",
        "wgsextract qc multiqc-metrics --input results/",
    ),
    "qc vcf": _h(
        "<input file name>.vcfstats.txt (e.g. sample.vcf.gz.vcfstats.txt), in "
        "--outdir, else beside the input.",
        f"wgsextract qc vcf {_VCF_IN}",
    ),
    "qc fake-data": _h(
        "fake.cram, fake.bam, fake.vcf or fake_R1/_R2.fastq.gz, and the fake "
        "reference.",
        "wgsextract qc fake-data --outdir out/fake --type bam,vcf --seed 1",
    ),
    "repair ftdna-bam": _h(
        "<input stem>_repaired.bam (+ .bai), or the --output path.",
        "wgsextract repair ftdna-bam --input ftdna.bam",
    ),
    "repair ftdna-vcf": _h(
        "<input stem>_repaired.vcf, or the --output path.",
        "wgsextract repair ftdna-vcf --input ftdna.vcf",
    ),
    # advanced: analyze, typing, methylation, pet-align, sign, benchmark
    "analyze comprehensive": _h(
        "The QC, lineage, variant and annotation outputs of each step, and a "
        "summary; analyze.steps.json records the completed steps.",
        "wgsextract analyze comprehensive --input sample.cram",
        "wgsextract analyze comprehensive --input sample.cram --force-step annotate",
        "wgsextract analyze comprehensive --vcf-inputs snps.vcf.gz sv.vcf.gz",
    ),
    "analyze batch-gen": _h(
        "A batch CSV/TSV for 'analyze comprehensive --batch' (--output).",
        "wgsextract analyze batch-gen --directory genomes/ --output batch.tsv",
    ),
    "typing cyp2d6": _h(
        "Cyrius's <stem>.json/.tsv and <stem>.cyp2d6.pharmcat.tsv.",
        "wgsextract typing cyp2d6 --input sample.cram",
    ),
    "typing hla": _h(
        "<stem>.t1k_genotype.tsv (and T1K's other files) and <stem>.hla.pharmcat.tsv.",
        "wgsextract typing hla --input sample.cram",
    ),
    "typing str": _h(
        "ExpansionHunter's <stem>.str.vcf/.json and a flagged-repeat summary.",
        "wgsextract typing str --input sample.cram",
    ),
    "methylation pileup": _h(
        "<input stem>.<context>.bedmethyl.gz (+ .tbi) and "
        "<input stem>.<context>.methylation.tsv; with --format parquet the "
        "summary is .methylation.parquet and the calls are also in "
        "<input stem>.<context>.bedmethyl.parquet.",
        "wgsextract methylation pileup --input ont.bam",
        "wgsextract methylation pileup --input ont.bam --format parquet",
    ),
    "pet-align": _h(
        "<reads stem>_<species>.bam or .cram (+ index) and "
        "<reads stem>_<species>.vcf.gz (+ .tbi).",
        "wgsextract pet-align --r1 dog_R1.fastq.gz --r2 dog_R2.fastq.gz --species dog",
    ),
    "sign create": _h(
        "SHA256SUMS in the inputs' folder (or --output); SHA256SUMS.sig with --key.",
        "wgsextract sign create results/",
        "wgsextract sign create results/ --key ~/.ssh/id_ed25519",
    ),
    "sign verify": _h(
        "Prints the verification result; writes no files.",
        "wgsextract sign verify results/SHA256SUMS --public-key key.pub",
    ),
    "benchmark": _h(
        "runs/<timestamp>/ under --outdir (default out/benchmark): "
        "benchmark_report.md and benchmark_results.json (timings, tool versions) "
        "and per-step logs.",
        "wgsextract benchmark --profile smoke",
    ),
}
