"""What each runnable command needs, and how long it typically takes.

Each command's ``--help``, ``wgsextract help --all --json`` and
docs/commands.md publish this. ``tools`` are the external tools a run always
checks for; ``optional`` are those only some options or inputs use (an
alternative caller or aligner, or a faster tool used when installed). Names
are those in core/tool_catalog.py, which ``wgsextract deps check`` reports on.

tests/test_command_needs.py keeps the two in step with the code: the tools
must be exactly those a command's handler checks for, found by following its
calls to ``verify_dependencies``, ``env_tool``, ``pixi_env_tool`` and
``require_native_tool``; ``optional`` may also name tools it probes for with
``get_tool_path``.

``runtime`` is a class, not a promise: for a 30x whole genome (about 100 GB
of BAM or 15-20 GB of CRAM) on a typical 8-thread computer, unless ``scale``
(the suffix of ``runtime_text``) says what it depends on instead. Downloads depend on the connection.
"""

from dataclasses import dataclass

INSTANT = "instant"
SECONDS = "seconds"
MINUTES = "minutes"
HOURS = "hours"
DOWNLOAD = "download"

# Upper bounds in seconds, matched to core/warnings.py's EXPECTED_TIME.
RUNTIME_LIMITS = {INSTANT: 5, SECONDS: 60, MINUTES: 3600}
RUNTIMES = (INSTANT, SECONDS, MINUTES, HOURS, DOWNLOAD)
GENOME = " on a 30x genome"
REFERENCE = ", once per reference"


@dataclass(frozen=True)
class CommandNeeds:
    runtime: str
    tools: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    scale: str = GENOME

    @property
    def runtime_text(self) -> str:
        if self.runtime == DOWNLOAD:
            return "depends on download speed"
        if self.runtime == INSTANT:
            return INSTANT
        return self.runtime + self.scale

    @property
    def tools_text(self) -> str:
        """'bcftools, tabix; depending on options and inputs, also: delly, sniffles'."""
        text = ", ".join(self.tools) or "no external tools"
        if self.optional:
            text += "; depending on options and inputs, also: " + ", ".join(
                self.optional
            )
        return text


def _n(
    runtime: str, *tools: str, optional: str = "", scale: str = GENOME
) -> CommandNeeds:
    return CommandNeeds(runtime, tools, tuple(optional.split()), scale)


_SAM = ("samtools",)
_VCF = ("bcftools", "tabix")
_CALL = ("bcftools", "samtools", "tabix")
_SMALL = " for small inputs"

COMMAND_NEEDS: dict[str, CommandNeeds] = {
    "help": _n(INSTANT),
    "config": _n(INSTANT),
    "info": _n(SECONDS, *_SAM),
    "info calculate-coverage": _n(MINUTES, *_SAM),
    "info coverage-sample": _n(MINUTES, *_SAM),
    "info chip": _n(SECONDS, scale=" for a raw-data file"),
    "sample show": _n(INSTANT),
    "sample set": _n(INSTANT),
    "sample unset": _n(INSTANT),
    "microarray": _n(MINUTES, "bcftools", "bgzip", "samtools", "tabix"),
    "lineage y-haplogroup": _n(MINUTES, "bcftools", "samtools"),
    "lineage y-str": _n(MINUTES, *_SAM),
    "lineage mt-haplogroup": _n(MINUTES, "bcftools", "java"),
    "extract mito-fasta": _n(MINUTES, *_CALL),
    "extract mito-vcf": _n(MINUTES, *_CALL),
    "extract mt-bam": _n(MINUTES, *_SAM),
    "extract ydna-bam": _n(MINUTES, *_SAM),
    "extract ydna-vcf": _n(MINUTES, "bcftools", "bgzip", "samtools", "tabix"),
    "extract y-mt-extract": _n(MINUTES, *_SAM),
    "extract unmapped": _n(MINUTES, *_SAM),
    "extract custom": _n(MINUTES, *_SAM),
    "extract bam-subset": _n(MINUTES, *_SAM),
    "realign": _n(
        HOURS,
        *_SAM,
        optional="bwa bwa-mem2 minimap2 pbmm2 samblaster gatk",
    ),
    "align": _n(
        HOURS,
        *_SAM,
        optional="bwa bwa-mem2 minimap2 pbmm2 samblaster gatk",
    ),
    "vcf snp": _n(HOURS, *_VCF),
    "vcf indel": _n(HOURS, *_VCF),
    "vcf annotate": _n(MINUTES, *_VCF),
    "vcf filter": _n(MINUTES, *_VCF),
    "vcf table": _n(MINUTES, "bcftools"),
    "vcf clinvar": _n(MINUTES, *_VCF),
    "vcf revel": _n(MINUTES, *_VCF),
    "vcf gnomad": _n(MINUTES, *_VCF),
    "vcf pharmgkb": _n(MINUTES, *_VCF),
    "vcf to-fasta": _n(MINUTES, "bcftools", "bgzip", "samtools", "tabix"),
    "vcf trio": _n(MINUTES, *_CALL),
    "vcf cnv": _n(HOURS, "bcftools", "delly", "samtools", "tabix"),
    "vcf sv": _n(HOURS, *_CALL, optional="delly pbsv sniffles"),
    "vcf freebayes": _n(HOURS, "bcftools", "freebayes", "samtools", "tabix"),
    "vcf gatk": _n(HOURS, "gatk", "samtools", "tabix", optional="bcftools"),
    "vcf deepvariant": _n(
        HOURS, "samtools", "tabix", optional="run_deepvariant docker"
    ),
    "vcf clair3": _n(HOURS, "bcftools", "run_clair3.sh", "samtools", "tabix"),
    "vcf phylop": _n(MINUTES, "bcftools", "bgzip", "tabix"),
    "vcf cadd": _n(MINUTES, *_VCF),
    "vcf gerp": _n(MINUTES, "bcftools", "bgzip", "tabix"),
    "vcf spliceai": _n(MINUTES, *_VCF),
    "vcf alphamissense": _n(MINUTES, *_VCF),
    "vcf pharmcat": _n(MINUTES, "bcftools", "pharmcat", "tabix"),
    "vcf chain-annotate": _n(MINUTES, *_VCF),
    "vep": _n(HOURS, "bcftools", "tabix", "vep"),
    "vep run": _n(HOURS, "bcftools", "tabix", "vep"),
    "vep download": _n(DOWNLOAD, "curl", "tar"),
    "vep verify": _n(SECONDS, scale=""),
    "ref download": _n(DOWNLOAD),
    "ref index": _n(HOURS, "bwa", "samtools", scale=REFERENCE),
    "ref count-ns": _n(MINUTES, scale=REFERENCE),
    "ref verify": _n(MINUTES, *_SAM, scale=REFERENCE),
    "ref library": _n(
        DOWNLOAD,
        "bcftools",
        "bgzip",
        "curl",
        "htsfile",
        "samtools",
        "tabix",
    ),
    "ref library-list": _n(INSTANT),
    "ref status": _n(SECONDS, scale=""),
    "ref bootstrap": _n(DOWNLOAD),
    "ref aadr": _n(DOWNLOAD),
    "ref ybrowse": _n(DOWNLOAD, "bgzip", "tabix"),
    "ref gene-map": _n(DOWNLOAD),
    "ref clinvar": _n(DOWNLOAD),
    "ref revel": _n(DOWNLOAD),
    "ref phylop": _n(DOWNLOAD),
    "ref gnomad": _n(DOWNLOAD),
    "ref spliceai": _n(DOWNLOAD),
    "ref alphamissense": _n(DOWNLOAD),
    "ref pharmgkb": _n(DOWNLOAD),
    "ref cadd": _n(DOWNLOAD),
    "ref gerp": _n(DOWNLOAD),
    "deps check": _n(SECONDS, scale=""),
    "example-genome list": _n(INSTANT),
    "example-genome download": _n(DOWNLOAD),
    "report-issue": _n(SECONDS, scale=""),
    "bam sort": _n(HOURS, *_SAM),
    "bam index": _n(MINUTES, *_SAM),
    "bam unindex": _n(INSTANT),
    "bam unsort": _n(HOURS, *_SAM),
    "bam to-cram": _n(MINUTES, *_SAM),
    "bam to-bam": _n(HOURS, *_SAM),
    "bam unalign": _n(HOURS, *_SAM),
    "bam identify": _n(SECONDS, *_SAM, scale=""),
    "bam merge": _n(HOURS, *_SAM),
    "bam to-ubam": _n(HOURS, *_SAM),
    "qc fastp": _n(MINUTES, "fastp"),
    "qc fastqc": _n(HOURS, "fastqc"),
    "qc multiqc": _n(SECONDS, "multiqc", scale=""),
    "qc multiqc-metrics": _n(SECONDS, scale=""),
    "qc vcf": _n(MINUTES, "bcftools"),
    "qc fake-data": _n(SECONDS, "bcftools", "bgzip", "samtools", "tabix", scale=_SMALL),
    "repair ftdna-bam": _n(MINUTES, *_SAM),
    "repair ftdna-vcf": _n(SECONDS, scale=""),
    "analyze comprehensive": _n(HOURS, *_CALL),
    "analyze batch-gen": _n(INSTANT),
    "typing cyp2d6": _n(MINUTES, "cyrius", "minimap2", "samtools"),
    "typing hla": _n(MINUTES, "run-t1k", "samtools", "t1k-build.pl"),
    "typing str": _n(MINUTES, "ExpansionHunter", "samtools"),
    "methylation pileup": _n(HOURS, "modkit", "samtools", "tabix"),
    "pet-align": _n(HOURS, "bcftools", "bwa", "samtools"),
    "sign create": _n(MINUTES, scale=" for genome-sized files"),
    "sign verify": _n(MINUTES, scale=" for genome-sized files"),
    "benchmark": _n(HOURS, scale=" for the full suite"),
}
