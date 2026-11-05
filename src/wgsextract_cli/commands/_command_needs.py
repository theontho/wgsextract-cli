"""What each runnable command needs, and how long it typically takes.

Each command's ``--help``, ``wgsextract help --all --json`` and
docs/commands.md publish this. ``tools`` are the external tools every
documented run needs; ``optional`` are those only some options or inputs use
(an alternative caller or aligner, a VCF instead of a BAM, a download, or a
faster tool used when installed). Names are those ``wgsextract deps check``
reports on (core/tool_catalog.py), plus docker and ssh-keygen.

tests/test_command_needs.py keeps the two in step with the code by following
each handler's calls: ``tools`` are those it checks for with
``verify_dependencies``, ``env_tool``, ``pixi_env_tool`` or
``require_native_tool`` on every run, plus samtools when the examples give
the BAM/CRAM ``--input`` the CLI checks first; every other tool it checks,
probes for (``get_tool_path``, ``shutil.which``) or runs is ``optional``.

``runtime`` is a class, not a promise: for a 30x whole genome (about 100 GB
of BAM or 15-20 GB of CRAM) on a typical 8-thread computer, unless ``scale``
(the suffix of ``runtime_text``) says what it depends on instead. Downloads
depend on the connection.
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
    "info": _n(SECONDS, *_SAM, optional="bcftools"),
    "info calculate-coverage": _n(MINUTES, *_SAM, optional="bcftools"),
    "info coverage-sample": _n(MINUTES, *_SAM, optional="bcftools"),
    "info chip": _n(SECONDS, scale=" for a raw-data file"),
    "sample show": _n(INSTANT),
    "sample set": _n(INSTANT),
    "sample unset": _n(INSTANT),
    "microarray": _n(
        MINUTES, "bcftools", "bgzip", "samtools", "tabix", optional="curl"
    ),
    "lineage y-haplogroup": _n(MINUTES, "bcftools", "samtools"),
    "lineage y-str": _n(MINUTES, *_SAM, optional="bcftools"),
    "lineage mt-haplogroup": _n(
        MINUTES, "bcftools", "haplogrep", "java", "samtools", optional="tabix"
    ),
    "extract mito-fasta": _n(MINUTES, *_CALL, optional="sambamba"),
    "extract mito-vcf": _n(MINUTES, *_CALL, optional="sambamba"),
    "extract mt-bam": _n(MINUTES, *_SAM, optional="bcftools sambamba"),
    "extract ydna-bam": _n(MINUTES, *_SAM, optional="bcftools sambamba"),
    "extract ydna-vcf": _n(
        MINUTES, "bcftools", "bgzip", "samtools", "tabix", optional="sambamba"
    ),
    "extract y-mt-extract": _n(MINUTES, *_SAM, optional="bcftools sambamba"),
    "extract unmapped": _n(MINUTES, *_SAM, optional="bcftools"),
    "extract custom": _n(MINUTES, *_SAM, optional="bcftools sambamba"),
    "extract bam-subset": _n(MINUTES, *_SAM, optional="bcftools"),
    "realign": _n(
        HOURS,
        *_SAM,
        optional="bcftools bwa bwa-mem2 curl gatk minimap2 pbmm2 sambamba samblaster",
    ),
    "align": _n(
        HOURS,
        *_SAM,
        optional="bcftools bwa bwa-mem2 curl gatk minimap2 pbmm2 sambamba samblaster",
    ),
    "vcf snp": _n(HOURS, *_CALL),
    "vcf indel": _n(HOURS, *_CALL),
    "vcf annotate": _n(MINUTES, *_VCF, optional="bgzip samtools"),
    "vcf filter": _n(MINUTES, *_VCF, optional="bgzip samtools"),
    "vcf table": _n(MINUTES, "bcftools", optional="bgzip tabix"),
    "vcf clinvar": _n(MINUTES, *_VCF, optional="bgzip samtools"),
    "vcf revel": _n(MINUTES, *_VCF, optional="bgzip samtools"),
    "vcf gnomad": _n(MINUTES, *_VCF, optional="bgzip samtools"),
    "vcf pharmgkb": _n(MINUTES, *_VCF, optional="bgzip samtools"),
    "vcf to-fasta": _n(MINUTES, "bcftools", "bgzip", "samtools", "tabix"),
    "vcf trio": _n(MINUTES, *_CALL, optional="bgzip"),
    "vcf cnv": _n(HOURS, "bcftools", "delly", "samtools", "tabix"),
    "vcf sv": _n(HOURS, *_CALL, optional="delly pbsv sniffles"),
    "vcf freebayes": _n(HOURS, "bcftools", "freebayes", "samtools", "tabix"),
    "vcf gatk": _n(HOURS, "gatk", "samtools", "tabix", optional="bcftools"),
    "vcf deepvariant": _n(
        HOURS, "samtools", "tabix", optional="bcftools docker run_deepvariant sambamba"
    ),
    "vcf clair3": _n(HOURS, "bcftools", "run_clair3.sh", "samtools", "tabix"),
    "vcf phylop": _n(MINUTES, "bcftools", "bgzip", "tabix", optional="samtools"),
    "vcf cadd": _n(MINUTES, *_VCF, optional="bgzip samtools"),
    "vcf gerp": _n(MINUTES, "bcftools", "bgzip", "tabix", optional="samtools"),
    "vcf spliceai": _n(MINUTES, *_VCF, optional="bgzip samtools"),
    "vcf alphamissense": _n(MINUTES, *_VCF, optional="bgzip samtools"),
    "vcf pharmcat": _n(MINUTES, "bcftools", "pharmcat", "tabix", optional="bgzip"),
    "vcf chain-annotate": _n(MINUTES, *_VCF, optional="bgzip samtools vep"),
    "vep": _n(HOURS, "bcftools", "tabix", "vep", optional="samtools"),
    "vep run": _n(HOURS, "bcftools", "tabix", "vep", optional="samtools"),
    "vep download": _n(DOWNLOAD, "curl", "tar"),
    "vep verify": _n(SECONDS, scale=""),
    "ref download": _n(DOWNLOAD, optional="curl"),
    "ref index": _n(HOURS, *_SAM, optional="bwa bwa-mem2 curl", scale=REFERENCE),
    "ref count-ns": _n(MINUTES, scale=REFERENCE),
    "ref verify": _n(MINUTES, *_SAM, scale=REFERENCE),
    "ref library": _n(
        DOWNLOAD, "bcftools", "bgzip", "curl", "htsfile", "samtools", "tabix"
    ),
    "ref library-list": _n(INSTANT),
    "ref status": _n(SECONDS, scale=""),
    "ref bootstrap": _n(DOWNLOAD),
    "ref aadr": _n(DOWNLOAD, optional="curl"),
    "ref ybrowse": _n(DOWNLOAD, "bgzip", "tabix"),
    "ref gene-map": _n(DOWNLOAD, optional="curl"),
    "ref clinvar": _n(DOWNLOAD, optional="bgzip curl tabix"),
    "ref revel": _n(DOWNLOAD, optional="bgzip curl tabix"),
    "ref phylop": _n(DOWNLOAD, optional="curl"),
    "ref gnomad": _n(DOWNLOAD, optional="bcftools bgzip curl tabix"),
    "ref spliceai": _n(DOWNLOAD, optional="bgzip curl tabix"),
    "ref alphamissense": _n(DOWNLOAD, optional="bgzip curl tabix"),
    "ref pharmgkb": _n(DOWNLOAD, optional="bgzip curl tabix"),
    "ref cadd": _n(DOWNLOAD, optional="curl"),
    "ref gerp": _n(DOWNLOAD, optional="curl"),
    "deps check": _n(SECONDS, scale=""),
    "example-genome list": _n(INSTANT),
    "example-genome download": _n(DOWNLOAD, optional="curl"),
    "report": _n(SECONDS, scale=" from existing results"),
    "report-issue": _n(SECONDS, scale=""),
    "serve": _n(SECONDS, scale=" to start; runs until stopped"),
    "bam sort": _n(HOURS, *_SAM, optional="bcftools sambamba"),
    "bam index": _n(MINUTES, *_SAM, optional="sambamba"),
    "bam unindex": _n(INSTANT, *_SAM),
    "bam unsort": _n(HOURS, *_SAM, optional="bcftools"),
    "bam to-cram": _n(MINUTES, *_SAM, optional="bcftools sambamba"),
    "bam to-bam": _n(HOURS, *_SAM, optional="bcftools sambamba"),
    "bam unalign": _n(HOURS, *_SAM, optional="bcftools"),
    "bam identify": _n(SECONDS, *_SAM, optional="bcftools", scale=""),
    "bam merge": _n(HOURS, *_SAM, optional="bcftools"),
    "bam to-ubam": _n(HOURS, *_SAM, optional="bcftools"),
    "qc fastp": _n(MINUTES, "fastp"),
    "qc fastqc": _n(HOURS, "fastqc"),
    "qc multiqc": _n(SECONDS, "multiqc", optional="bcftools fastqc", scale=""),
    "qc multiqc-metrics": _n(SECONDS, scale=""),
    "qc vcf": _n(MINUTES, "bcftools"),
    "qc fake-data": _n(
        SECONDS,
        "bcftools",
        "bgzip",
        "samtools",
        "tabix",
        optional="sambamba",
        scale=_SMALL,
    ),
    "repair ftdna-bam": _n(MINUTES, *_SAM, optional="bcftools"),
    "repair ftdna-vcf": _n(SECONDS, optional="bcftools", scale=""),
    "analyze comprehensive": _n(HOURS, *_CALL),
    "analyze batch-gen": _n(INSTANT),
    "typing cyp2d6": _n(MINUTES, "cyrius", "minimap2", "samtools", optional="bcftools"),
    "typing hla": _n(
        MINUTES, "run-t1k", "samtools", "t1k-build.pl", optional="bcftools"
    ),
    "typing str": _n(MINUTES, "ExpansionHunter", "samtools", optional="bcftools"),
    "methylation pileup": _n(HOURS, "modkit", "samtools", "tabix", optional="bcftools"),
    "pet-align": _n(
        HOURS, "bcftools", "bwa", "samtools", optional="bwa-mem2 sambamba samblaster"
    ),
    "sign create": _n(MINUTES, optional="ssh-keygen", scale=" for genome-sized files"),
    "sign verify": _n(MINUTES, optional="ssh-keygen", scale=" for genome-sized files"),
    "benchmark": _n(
        HOURS,
        optional="bcftools bgzip curl samtools tabix",
        scale=" for the full suite",
    ),
}
