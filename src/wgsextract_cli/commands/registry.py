from dataclasses import dataclass
from types import ModuleType

from . import (
    align,
    analyze,
    bam,
    basecall,
    benchmark,
    deps,
    examples,
    extract,
    genotyping,
    info,
    lineage,
    methylation,
    microarray,
    panel,
    pet,
    qc,
    realign,
    ref,
    repair,
    report,
    report_issue,
    sample,
    serve,
    sign,
    vcf,
    vep,
)

COMMAND_MODULES: tuple[ModuleType, ...] = (
    info,
    deps,
    examples,
    bam,
    benchmark,
    extract,
    microarray,
    lineage,
    vcf,
    repair,
    qc,
    ref,
    align,
    realign,
    basecall,
    pet,
    vep,
    analyze,
    sample,
    sign,
    genotyping,
    methylation,
    report,
    report_issue,
    panel,
    serve,
)

CORE = "core"
SETUP = "setup"
ADVANCED = "advanced"
TIERS = (CORE, SETUP, ADVANCED)


@dataclass(frozen=True)
class CommandTier:
    """A top-level command's tier and the plain-language summary shown for it
    in 'wgsextract --help'."""

    name: str
    tier: str
    summary: str


# Top-level commands by tier. The core tier is the consumer journey, in the
# order a typical user takes it; top-level help and the generated command
# reference list commands in this order. Tools that are optional, hard to
# install on some platforms or need their own Pixi environment are advanced.
COMMAND_TIERS: tuple[CommandTier, ...] = (
    CommandTier(
        "info",
        CORE,
        "Check a BAM/CRAM: reference build, file health, coverage.",
    ),
    CommandTier(
        "microarray",
        CORE,
        "Make 23andMe/AncestryDNA-style raw-data files to upload.",
    ),
    CommandTier(
        "lineage",
        CORE,
        "Find your paternal (Y-DNA) and maternal (mtDNA) haplogroups.",
    ),
    CommandTier(
        "extract",
        CORE,
        "Copy Y, mtDNA, unmapped or one region's reads to a file.",
    ),
    CommandTier(
        "realign",
        CORE,
        "Re-map a BAM/CRAM to another build, e.g. hg19 to hg38.",
    ),
    CommandTier(
        "align",
        CORE,
        "Map raw FASTQ reads to a reference, making a BAM/CRAM.",
    ),
    CommandTier(
        "vcf",
        CORE,
        "Call variants into a VCF, then annotate and filter them.",
    ),
    CommandTier(
        "vep",
        CORE,
        "Predict what each variant does to genes, with Ensembl VEP.",
    ),
    CommandTier(
        "report",
        CORE,
        "Summarise a sample's results in one local HTML report.",
    ),
    CommandTier(
        "ref", SETUP, "Download and check reference genomes and annotation data."
    ),
    CommandTier("deps", SETUP, "Check that the external tools are installed."),
    CommandTier(
        "example-genome", SETUP, "Download public example genomes to practise on."
    ),
    CommandTier(
        "sample",
        SETUP,
        "Show recorded sample facts; correct sex or platform.",
    ),
    CommandTier("config", SETUP, "Show the settings in use and the config.toml path."),
    CommandTier(
        "report-issue",
        SETUP,
        "Report a problem: a redacted, pre-filled GitHub bug report.",
    ),
    CommandTier("help", SETUP, "List every command, or explain one: 'help vcf snp'."),
    CommandTier("bam", ADVANCED, "Sort, index, convert and merge BAM/CRAM files."),
    CommandTier("qc", ADVANCED, "Read and VCF quality reports; fake test data."),
    CommandTier("repair", ADVANCED, "Fix FTDNA BAM/VCF formatting problems."),
    CommandTier(
        "analyze", ADVANCED, "Run several analyses in one go, for one or many genomes."
    ),
    CommandTier(
        "typing", ADVANCED, "Genotype CYP2D6, HLA and repeat expansions (30x WGS)."
    ),
    CommandTier(
        "methylation",
        ADVANCED,
        "5mC/5hmC methylation from Nanopore and PacBio reads.",
    ),
    CommandTier(
        "basecall",
        ADVANCED,
        "Nanopore POD5 signal to reads with Dorado (optional).",
    ),
    CommandTier(
        "panel",
        ADVANCED,
        "Gene lists to BED targets for Nanopore adaptive sampling.",
    ),
    CommandTier(
        "pet-align", ADVANCED, "Align and call variants for a cat or dog genome."
    ),
    CommandTier("sign", ADVANCED, "Checksum and sign output files; verify them."),
    CommandTier("benchmark", ADVANCED, "Time the main workflows on test data."),
    CommandTier(
        "serve", ADVANCED, "Local HTTP/JSON and MCP server for apps and AI agents."
    ),
)

# Subcommands whose tier differs from their command group's: specialist,
# optional or hard-to-install tools inside the core groups.
ADVANCED_SUBCOMMANDS = frozenset(
    {
        "extract bam-subset",
        "vcf freebayes",
        "vcf gatk",
        "vcf deepvariant",
        "vcf clair3",
        "vcf phase",
        "vcf cnv",
        "vcf sv",
        "vcf trio",
        "vcf pharmcat",
        "vcf to-fasta",
        "vcf chain-annotate",
        "vcf cadd",
        "vcf gerp",
        "vcf spliceai",
        "vcf alphamissense",
        "vcf phylop",
    }
)


@dataclass(frozen=True)
class Deprecation:
    """A command that still works but warns, naming its replacement; it is
    removed in ``remove_in`` (one minor release after it is deprecated).
    ``bare`` deprecates only running a group without a subcommand; the group
    and its subcommands stay."""

    path: str
    replacement: str
    remove_in: str
    bare: bool = False


# Commands that duplicate another one exactly (the #352 wrapper audit). Help
# and the generated docs list them as deprecated, and running one warns.
DEPRECATIONS: tuple[Deprecation, ...] = (
    Deprecation("ref library-list", "ref library --list", "0.8.0"),
    Deprecation("vep", "vep run", "0.8.0", bare=True),
)
DEPRECATED = {deprecation.path: deprecation for deprecation in DEPRECATIONS}


@dataclass(frozen=True)
class Journey:
    """A starting point ("I have ...") and the commands to run from it, in
    order. Each step is (command path, what it gives you)."""

    have: str
    steps: tuple[tuple[str, str], ...]


_THEN_VARIANTS = (
    ("vcf snp", "call SNPs into a VCF"),
    ("vep run", "predict each variant's effect on genes"),
    ("report", "summarise everything in one HTML report"),
)

# The documentation's "start from what you have" paths: generated into
# docs/commands.md and the site's CLI page. Steps are core or setup commands.
JOURNEYS: tuple[Journey, ...] = (
    Journey(
        "Nothing yet (a first run)",
        (
            ("deps check", "check that the external tools are installed"),
            ("ref bootstrap", "download the small shared files, e.g. liftover chains"),
            ("ref library", "download a reference genome"),
            ("example-genome download", "get a public example genome to practise on"),
        ),
    ),
    Journey(
        "A BAM or CRAM from a sequencing service (Nebula, Dante Labs, "
        "Sequencing.com, ...)",
        (
            ("info", "check the reference build, file health and coverage"),
            ("microarray", "make 23andMe/AncestryDNA-style files to upload"),
            ("lineage y-haplogroup", "find the paternal haplogroup"),
            ("lineage mt-haplogroup", "find the maternal haplogroup"),
            *_THEN_VARIANTS,
        ),
    ),
    Journey(
        "A BAM or CRAM on the wrong build (hg19 when you want hg38, or the reverse)",
        (
            ("info", "see which build it is"),
            ("realign", "re-map it to the other build"),
            ("info", "check the new file, then continue as above"),
        ),
    ),
    Journey(
        "FASTQ files (raw reads)",
        (
            ("align", "map the reads to a reference, making a BAM/CRAM"),
            ("info", "check the result, then continue as for a BAM/CRAM"),
        ),
    ),
    Journey(
        "A VCF (from a provider or another pipeline)",
        (
            ("vcf clinvar", "add ClinVar clinical significance"),
            ("vcf filter", "keep a gene, region or quality range"),
            ("vep run", "predict each variant's effect on genes"),
            ("vcf table", "one row per variant, for spreadsheets and SQL"),
        ),
    ),
    Journey(
        "A consumer raw-data file (23andMe, AncestryDNA, ...)",
        (("info chip", "identify the vendor, chip and revision"),),
    ),
)

_TOP_LEVEL = {command.name: command for command in COMMAND_TIERS}


def command_tier(path: str) -> str:
    """The tier of a command path such as "vcf snp"."""
    if path in ADVANCED_SUBCOMMANDS:
        return ADVANCED
    top = path.split(" ", 1)[0]
    if top not in _TOP_LEVEL:
        raise KeyError(f"No tier for command '{top}'; add it to COMMAND_TIERS.")
    return _TOP_LEVEL[top].tier
