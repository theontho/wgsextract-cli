import argparse

from wgsextract_cli.core.config import settings
from wgsextract_cli.core.messages import CLI_HELP

from ._vcf_basic import (
    cmd_annotate,
    cmd_indel,
    cmd_snp,
)
from ._vcf_callers import (
    cmd_freebayes,
    cmd_gatk,
)
from ._vcf_chain import (
    cmd_chain_annotate,
)
from ._vcf_clinvar_revel import (
    cmd_clinvar,
    cmd_revel,
)
from ._vcf_deepvariant import (
    cmd_deepvariant,
)
from ._vcf_filter_trio import (
    cmd_filter,
    cmd_trio,
)
from ._vcf_population import (
    cmd_gnomad,
    cmd_phylop,
)
from ._vcf_splicing import (
    cmd_alphamissense,
    cmd_pharmgkb,
    cmd_spliceai,
)
from ._vcf_structural import (
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
        help="Variant calling and processing using bcftools, delly, or freebayes.",
    )
    vcf_subs = parser.add_subparsers(dest="vcf_cmd", required=True)

    snp_parser = vcf_subs.add_parser(
        "snp", parents=[base_parser], help=CLI_HELP["cmd_snp"]
    )
    _add_ploidy_options(snp_parser)
    _add_region(snp_parser, "Chromosomal region (e.g. chrM, chrY:10000-20000)")
    snp_parser.set_defaults(func=cmd_snp)

    indel_parser = vcf_subs.add_parser(
        "indel", parents=[base_parser], help=CLI_HELP["cmd_indel"]
    )
    _add_ploidy_options(indel_parser)
    _add_region(indel_parser, "Chromosomal region (e.g. chrM, chrY:10000-20000)")
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
    trio_parser.add_argument("--proband", help="VCF file for the child")
    trio_parser.add_argument(
        "--mode",
        choices=["denovo", "recessive", "comphet", "all"],
        default="denovo",
        help="Inheritance mode to filter for",
    )
    _add_region(trio_parser)
    trio_parser.set_defaults(func=cmd_trio)

    cnv_parser = vcf_subs.add_parser(
        "cnv", parents=[base_parser], help=CLI_HELP["cmd_cnv"]
    )
    _add_region(cnv_parser)
    cnv_parser.add_argument(
        "-M", "--map", help="Mappability map file (required for delly cnv)"
    )
    cnv_parser.add_argument("--ploidy", help="Predefined ploidy name or value")
    cnv_parser.set_defaults(func=cmd_cnv)

    sv_parser = vcf_subs.add_parser(
        "sv", parents=[base_parser], help=CLI_HELP["cmd_sv"]
    )
    _add_region(sv_parser)
    sv_parser.add_argument(
        "--caller",
        choices=["delly", "pbsv", "sniffles"],
        default="delly",
        help="SV caller to use. Use pbsv or sniffles for PacBio long-read BAMs.",
    )
    sv_parser.add_argument(
        "--pacbio",
        action="store_true",
        help="Alias for --caller pbsv --ccs for PacBio HiFi/CCS data.",
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
        choices=["WGS", "WES", "PACBIO", "HYBRID_PACBIO_ILLUMINA"],
        help="DeepVariant model type. Use PACBIO for PacBio HiFi/CCS alignments.",
    )
    deepvariant_parser.add_argument(
        "--pacbio",
        action="store_true",
        help="Alias for --model-type PACBIO.",
    )
    deepvariant_parser.add_argument(
        "--checkpoint", help="Path to DeepVariant model checkpoint"
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
        "Path to PhyloP TSV or VCF data file.",
    )
    _add_min_score(phylop_parser, "PhyloP", "2.0")
    phylop_parser.set_defaults(func=cmd_phylop)

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
        "Path to gnomAD VCF data file.",
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
