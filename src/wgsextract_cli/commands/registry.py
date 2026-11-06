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

_TOP_LEVEL = {command.name: command for command in COMMAND_TIERS}


def command_tier(path: str) -> str:
    """The tier of a command path such as "vcf snp"."""
    if path in ADVANCED_SUBCOMMANDS:
        return ADVANCED
    top = path.split(" ", 1)[0]
    if top not in _TOP_LEVEL:
        raise KeyError(f"No tier for command '{top}'; add it to COMMAND_TIERS.")
    return _TOP_LEVEL[top].tier
