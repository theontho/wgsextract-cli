import argparse

from wgsextract_cli.core.config import settings
from wgsextract_cli.core.messages import CLI_HELP

from .basic import (
    cmd_annotate,
    cmd_indel,
    cmd_snp,
)
from .callers import (
    cmd_freebayes,
    cmd_gatk,
)
from .chain import (
    cmd_chain_annotate,
)
from .clinvar_revel import (
    cmd_clinvar,
    cmd_revel,
)
from .consensus import HAPLOTYPE_OPTIONS, cmd_to_fasta
from .deepvariant import (
    cmd_deepvariant,
)
from .deleteriousness import cmd_cadd, cmd_gerp
from .filter_trio import (
    cmd_filter,
    cmd_trio,
)
from .pharmcat import cmd_pharmcat
from .population import (
    cmd_gnomad,
    cmd_phylop,
)
from .splicing import (
    cmd_alphamissense,
    cmd_pharmgkb,
    cmd_spliceai,
)
from .structural import (
    cmd_cnv,
    cmd_sv,
)


def _add_region(
    parser: argparse.ArgumentParser, help_text: str = "Chromosomal region"
) -> None:
    parser.add_argument("-r", "--region", help=help_text)


def _add_ploidy_options(parser: argparse.ArgumentParser) -> None:
    ploidy_group = parser.add_mutually_exclusive_group(required=False)
    ploidy_group.add_argument(
        "--ploidy-file",
        help="File defining ploidy per chromosome (auto-resolved from --ref if possible)",
    )
    ploidy_group.add_argument("--ploidy", help="Predefined ploidy name or value")


def _add_parallel_contigs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--parallel",
        action="store_true",
        help="Call contigs in parallel with up to --threads workers. Faster, but "
        "calls at sites deeper than 255 reads can differ slightly from the default "
        "single stream, because bcftools subsamples them with one random stream "
        "per process.",
    )


def _add_vcf_input(
    parser: argparse.ArgumentParser,
    help_text: str = "Optional override for VCF input file.",
) -> None:
    parser.add_argument(
        "--vcf-input",
        default=settings.get("default_input_vcf"),
        help=help_text,
    )


def _add_annotation_resource(
    parser: argparse.ArgumentParser,
    option: str,
    setting_key: str,
    help_text: str,
) -> None:
    parser.add_argument(option, default=settings.get(setting_key), help=help_text)


def _add_min_score(
    parser: argparse.ArgumentParser,
    label: str,
    example: str,
) -> None:
    parser.add_argument(
        "--min-score",
        type=float,
        help=f"Minimum {label} score to filter for (e.g., {example}).",
    )


def register(
    subparsers: argparse._SubParsersAction, base_parser: argparse.ArgumentParser
) -> None:
    parser = subparsers.add_parser(
        "vcf",
        help=CLI_HELP["cmd_vcf"],
    )
    vcf_subs = parser.add_subparsers(dest="vcf_cmd", required=True)

    snp_parser = vcf_subs.add_parser(
        "snp", parents=[base_parser], help=CLI_HELP["cmd_snp"]
    )
    _add_ploidy_options(snp_parser)
    _add_region(snp_parser, "Chromosomal region (e.g. chrM, chrY:10000-20000)")
    _add_parallel_contigs(snp_parser)
    snp_parser.set_defaults(func=cmd_snp)

    indel_parser = vcf_subs.add_parser(
        "indel", parents=[base_parser], help=CLI_HELP["cmd_indel"]
    )
    _add_ploidy_options(indel_parser)
    _add_region(indel_parser, "Chromosomal region (e.g. chrM, chrY:10000-20000)")
    _add_parallel_contigs(indel_parser)
    indel_parser.set_defaults(func=cmd_indel)

    annotate_parser = vcf_subs.add_parser(
        "annotate", parents=[base_parser], help=CLI_HELP["cmd_annotate"]
    )
    _add_vcf_input(annotate_parser, CLI_HELP["arg_vcf_input"])
    annotate_parser.add_argument(
        "--ann-vcf", help="Annotation VCF file (auto-resolved from --ref if possible)"
    )
    annotate_parser.add_argument("--cols", help="Columns to annotate (e.g. ID,INFO/HG)")
    annotate_parser.set_defaults(func=cmd_annotate)

    filter_parser = vcf_subs.add_parser(
        "filter", parents=[base_parser], help=CLI_HELP["cmd_filter"]
    )
    _add_vcf_input(filter_parser, CLI_HELP["arg_vcf_input"])
    filter_parser.add_argument(
        "--expr", help="bcftools filter expression (e.g. 'QUAL>30')"
    )
    filter_parser.add_argument("--gene", help="Filter by Gene Name (e.g. BRCA1, KCNQ2)")
    filter_parser.add_argument(
        "--exclude-near-gaps",
        action="store_true",
        help="Exclude variants in or near genomic gaps (requires Count Ns output)",
    )
    _add_region(filter_parser)
    filter_parser.set_defaults(func=cmd_filter)

    to_fasta_parser = vcf_subs.add_parser(
        "to-fasta", parents=[base_parser], help=CLI_HELP["cmd_vcf-to-fasta"]
    )
    _add_vcf_input(to_fasta_parser, CLI_HELP["arg_vcf_input"])
    _add_region(
        to_fasta_parser,
        "Only build this region (e.g. chr21 or 21:30000000-31000000); "
        "default: the whole reference.",
    )
    to_fasta_parser.add_argument(
        "--sample", help="Sample to use from a multi-sample VCF."
    )
    to_fasta_parser.add_argument(
        "--haplotype",
        choices=list(HAPLOTYPE_OPTIONS),
        default="iupac",
        help="How genotypes are applied: iupac (default) writes heterozygous "
        "SNVs as IUPAC codes (e.g. R for A/G) and applies homozygous ALTs; alt "
        "applies every ALT; 1 or 2 applies that haplotype of a phased VCF (for "
        "unphased genotypes, the first or second allele, so 1 keeps the "
        "reference at heterozygous sites).",
    )
    to_fasta_parser.add_argument(
        "--uncalled",
        choices=["auto", "n", "ref"],
        default="auto",
        help="What to write where the VCF has no passing call: n (an N), ref "
        "(the reference base), or auto (default): n for a gVCF, whose reference "
        "blocks say which positions were called, ref for a variant-only VCF.",
    )
    to_fasta_parser.add_argument(
        "--include-filtered",
        action="store_true",
        help="Also apply variants whose FILTER is not PASS.",
    )
    to_fasta_parser.add_argument(
        "--mask", help="BED file of regions to replace with N (e.g. low coverage)."
    )
    to_fasta_parser.add_argument(
        "--chain",
        action="store_true",
        help="Also write a chain file mapping reference coordinates to the "
        "consensus (indels shift positions).",
    )
    to_fasta_parser.set_defaults(func=cmd_to_fasta)

    trio_parser = vcf_subs.add_parser(
        "trio", parents=[base_parser], help=CLI_HELP["cmd_trio"]
    )
    _add_vcf_input(trio_parser, CLI_HELP["arg_vcf_input"])
    trio_parser.add_argument(
        "--mother",
        default=settings.get("mother_vcf_path"),
        help=CLI_HELP["arg_mother"],
    )
    trio_parser.add_argument(
        "--father",
        default=settings.get("father_vcf_path"),
        help=CLI_HELP["arg_father"],
    )
    trio_parser.add_argument(
        "--proband",
        help="The child's VCF or BAM/CRAM; overrides --vcf-input/--input and "
        "configured defaults. With the parents' BAM/CRAMs, genotyped jointly "
        "from the reads, which de novo calls need.",
    )
    trio_parser.add_argument(
        "--mode",
        choices=["denovo", "recessive", "comphet", "all"],
        default="denovo",
        help="Inheritance mode to filter for",
    )
    trio_parser.add_argument(
        "--min-depth",
        type=int,
        help="BAM/CRAM input: minimum read depth in each person at a reported site "
        "(default 15)",
    )
    trio_parser.add_argument(
        "--proband-sex",
        choices=["male", "female"],
        help="BAM/CRAM input: the child's sex, for chrX ploidy (default: inferred "
        "from chrX/chrY coverage)",
    )
    _add_region(trio_parser)
    trio_parser.set_defaults(func=cmd_trio)

    cnv_parser = vcf_subs.add_parser(
        "cnv", parents=[base_parser], help=CLI_HELP["cmd_cnv"]
    )
    _add_region(cnv_parser)
    cnv_parser.add_argument(
        "-M",
        "--map",
        help="Deprecated and ignored: Delly 2 no longer uses mappability maps.",
    )
    cnv_parser.add_argument(
        "--ploidy", type=int, help="Baseline ploidy for delly cnv (default: 2)"
    )
    cnv_parser.set_defaults(func=cmd_cnv)

    sv_parser = vcf_subs.add_parser(
        "sv", parents=[base_parser], help=CLI_HELP["cmd_sv"]
    )
    _add_region(sv_parser)
    sv_parser.add_argument(
        "--caller",
        choices=["delly", "pbsv", "sniffles"],
        help="SV caller to use (default: delly, or pbsv/sniffles with --pacbio). "
        "Use pbsv or sniffles for PacBio long-read BAMs.",
    )
    sv_parser.add_argument(
        "--pacbio",
        action="store_true",
        help="PacBio HiFi/CCS data: use pbsv (or sniffles if pbsv is missing) "
        "with CCS thresholds.",
    )
    sv_parser.add_argument(
        "--ccs",
        action="store_true",
        help="Use pbsv CCS/HiFi thresholds when --caller pbsv is selected.",
    )
    sv_parser.add_argument(
        "--tandem-repeats",
        help="Optional tandem-repeat BED for pbsv discover.",
    )
    sv_parser.set_defaults(func=cmd_sv)

    freebayes_parser = vcf_subs.add_parser(
        "freebayes", parents=[base_parser], help=CLI_HELP["cmd_freebayes"]
    )
    _add_region(freebayes_parser, "Chromosomal region (e.g. chrM, chrY:10000-20000)")
    freebayes_parser.set_defaults(func=cmd_freebayes)

    gatk_parser = vcf_subs.add_parser(
        "gatk", parents=[base_parser], help=CLI_HELP["cmd_gatk"]
    )
    _add_region(gatk_parser, "Chromosomal region (e.g. chrM)")
    gatk_parser.add_argument(
        "--gpu",
        action="store_true",
        help="Call on an NVIDIA GPU with Parabricks (Linux, Docker; see README).",
    )
    gatk_parser.set_defaults(func=cmd_gatk)

    deepvariant_parser = vcf_subs.add_parser(
        "deepvariant", parents=[base_parser], help=CLI_HELP["cmd_deepvariant"]
    )
    _add_region(deepvariant_parser, "Chromosomal region (e.g. chrM)")
    deepvariant_parser.add_argument(
        "--wes", action="store_true", help="Set model type to WES (default: WGS)"
    )
    deepvariant_parser.add_argument(
        "--model-type",
        choices=["WGS", "WES", "PACBIO", "HYBRID_PACBIO_ILLUMINA", "ULTIMA"],
        help="DeepVariant model type (default: ULTIMA for Ultima reads with their "
        "flow tags, else WGS). Use PACBIO for PacBio HiFi/CCS alignments.",
    )
    deepvariant_parser.add_argument(
        "--pacbio",
        action="store_true",
        help="Alias for --model-type PACBIO.",
    )
    deepvariant_parser.add_argument(
        "--checkpoint", help="Path to DeepVariant model checkpoint"
    )
    deepvariant_parser.add_argument(
        "--gpu",
        action="store_true",
        help="Call on an NVIDIA GPU with Parabricks (Linux, Docker; see README).",
    )
    deepvariant_parser.set_defaults(func=cmd_deepvariant)

    clinvar_parser = vcf_subs.add_parser(
        "clinvar",
        parents=[base_parser],
        help="Annotate VCF with ClinVar pathogenicity data.",
    )
    _add_vcf_input(clinvar_parser)
    _add_annotation_resource(
        clinvar_parser,
        "--clinvar-file",
        "clinvar_vcf_path",
        "Path to ClinVar VCF data file.",
    )
    clinvar_parser.set_defaults(func=cmd_clinvar)

    revel_parser = vcf_subs.add_parser(
        "revel",
        parents=[base_parser],
        help="Annotate VCF with REVEL pathogenicity scores.",
    )
    _add_vcf_input(revel_parser)
    _add_annotation_resource(
        revel_parser,
        "--revel-file",
        "revel_tsv_path",
        "Path to REVEL TSV or VCF data file.",
    )
    _add_min_score(revel_parser, "REVEL", "0.5")
    revel_parser.set_defaults(func=cmd_revel)

    phylop_parser = vcf_subs.add_parser(
        "phylop",
        parents=[base_parser],
        help="Annotate VCF with PhyloP conservation scores.",
    )
    _add_vcf_input(phylop_parser)
    _add_annotation_resource(
        phylop_parser,
        "--phylop-file",
        "phylop_tsv_path",
        "Path to a PhyloP bigWig (phylop_<build>.bw), or a TSV/VCF of scores.",
    )
    _add_min_score(phylop_parser, "PhyloP", "2.0")
    phylop_parser.set_defaults(func=cmd_phylop)

    cadd_parser = vcf_subs.add_parser(
        "cadd",
        parents=[base_parser],
        help="Annotate VCF with CADD deleteriousness scores (CADD_RAW, CADD_PHRED).",
    )
    _add_vcf_input(cadd_parser)
    _add_annotation_resource(
        cadd_parser,
        "--cadd-file",
        "cadd_tsv_path",
        "Path to the CADD whole-genome SNV TSV (cadd_<build>.tsv.gz).",
    )
    _add_annotation_resource(
        cadd_parser,
        "--cadd-indel-file",
        "cadd_indel_tsv_path",
        "Path to a CADD indel TSV (cadd_indel_<build>.tsv.gz); used when present.",
    )
    _add_min_score(cadd_parser, "CADD PHRED", "20")
    cadd_parser.set_defaults(func=cmd_cadd)

    gerp_parser = vcf_subs.add_parser(
        "gerp",
        parents=[base_parser],
        help="Annotate VCF with GERP++ conservation scores (GERP).",
    )
    _add_vcf_input(gerp_parser)
    _add_annotation_resource(
        gerp_parser,
        "--gerp-file",
        "gerp_bigwig_path",
        "Path to a GERP++ RS bigWig (gerp_<build>.bw).",
    )
    _add_min_score(gerp_parser, "GERP", "2.0")
    gerp_parser.set_defaults(func=cmd_gerp)

    gnomad_parser = vcf_subs.add_parser(
        "gnomad",
        parents=[base_parser],
        help="Annotate VCF with gnomAD population frequencies.",
    )
    _add_vcf_input(gnomad_parser)
    _add_annotation_resource(
        gnomad_parser,
        "--gnomad-file",
        "gnomad_vcf_path",
        "Path to gnomAD data: a sites VCF, or the allele-frequency table "
        "from 'ref gnomad --release 4.1'.",
    )
    gnomad_parser.add_argument(
        "--max-af",
        type=float,
        help="Maximum Allele Frequency to filter for (e.g., 0.01 for 1%%).",
    )
    gnomad_parser.set_defaults(func=cmd_gnomad)

    spliceai_parser = vcf_subs.add_parser(
        "spliceai",
        parents=[base_parser],
        help="Annotate VCF with SpliceAI splicing scores.",
    )
    _add_vcf_input(spliceai_parser)
    _add_annotation_resource(
        spliceai_parser,
        "--spliceai-file",
        "spliceai_vcf_path",
        "Path to SpliceAI VCF data file.",
    )
    spliceai_parser.set_defaults(func=cmd_spliceai)

    alphamissense_parser = vcf_subs.add_parser(
        "alphamissense",
        parents=[base_parser],
        help="Annotate VCF with AlphaMissense pathogenicity scores.",
    )
    _add_vcf_input(alphamissense_parser)
    _add_annotation_resource(
        alphamissense_parser,
        "--am-file",
        "alphamissense_vcf_path",
        "Path to AlphaMissense VCF data file.",
    )
    _add_min_score(alphamissense_parser, "AlphaMissense", "0.5")
    alphamissense_parser.set_defaults(func=cmd_alphamissense)

    pharmgkb_parser = vcf_subs.add_parser(
        "pharmgkb",
        parents=[base_parser],
        help="Annotate VCF with PharmGKB drug metabolism data.",
    )
    _add_vcf_input(pharmgkb_parser)
    _add_annotation_resource(
        pharmgkb_parser,
        "--pharmgkb-file",
        "pharmgkb_vcf_path",
        "Path to PharmGKB VCF or data file.",
    )
    pharmgkb_parser.set_defaults(func=cmd_pharmgkb)

    pharmcat_parser = vcf_subs.add_parser(
        "pharmcat",
        parents=[base_parser],
        help="Call star-allele diplotypes and drug-response phenotypes (CYP2C19, "
        "CYP2C9, DPYD, ...) with PharmCAT, and write its prescribing report.",
    )
    _add_vcf_input(pharmcat_parser)
    _add_annotation_resource(
        pharmcat_parser,
        "--pharmcat-dir",
        "pharmcat_directory",
        "Extracted PharmCAT pipeline release (default: <reference library>/pharmcat).",
    )
    pharmcat_parser.add_argument(
        "--absent-to-ref",
        action="store_true",
        help="Treat positions absent from the VCF as reference. Only for a "
        "whole-genome VCF that lists variants only (not a gVCF).",
    )
    pharmcat_parser.add_argument(
        "--outside-call",
        action="append",
        metavar="FILE",
        help="PharmCAT outside-call file (gene<TAB>diplotype), e.g. from "
        "`typing cyp2d6` or `typing hla`. Repeatable; one call per gene.",
    )
    pharmcat_parser.set_defaults(func=cmd_pharmcat)

    chain_annotate_parser = vcf_subs.add_parser(
        "chain-annotate",
        parents=[base_parser],
        help="Sequentially apply multiple annotations to a single VCF.",
    )
    _add_vcf_input(chain_annotate_parser)
    chain_annotate_parser.add_argument(
        "--annotations",
        default="clinvar,revel,phylop,gnomad,vep",
        help="Comma-separated list of annotations to apply in order (default: clinvar,revel,phylop,gnomad,vep).",
    )
    chain_annotate_parser.add_argument(
        "--keep-intermediates",
        action="store_true",
        help="Keep intermediate VCF files generated during the chain process.",
    )
    chain_annotate_parser.set_defaults(func=cmd_chain_annotate)
