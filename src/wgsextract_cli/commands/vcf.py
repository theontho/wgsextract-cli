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


def register(subparsers, base_parser):
    parser = subparsers.add_parser(
        "vcf",
        help="Variant calling and processing using bcftools, delly, or freebayes.",
    )
    vcf_subs = parser.add_subparsers(dest="vcf_cmd", required=True)

    snp_parser = vcf_subs.add_parser(
        "snp", parents=[base_parser], help=CLI_HELP["cmd_snp"]
    )

    snp_group = snp_parser.add_mutually_exclusive_group(required=False)
    snp_group.add_argument(
        "--ploidy-file",
        help="File defining ploidy per chromosome (auto-resolved from --ref if possible)",
    )
    snp_group.add_argument("--ploidy", help="Predefined ploidy name or value")
    snp_parser.add_argument(
        "-r", "--region", help="Chromosomal region (e.g. chrM, chrY:10000-20000)"
    )
    snp_parser.set_defaults(func=cmd_snp)

    indel_parser = vcf_subs.add_parser(
        "indel", parents=[base_parser], help=CLI_HELP["cmd_indel"]
    )
    indel_group = indel_parser.add_mutually_exclusive_group(required=False)
    indel_group.add_argument(
        "--ploidy-file",
        help="File defining ploidy per chromosome (auto-resolved from --ref if possible)",
    )
    indel_group.add_argument("--ploidy", help="Predefined ploidy name or value")
    indel_parser.add_argument(
        "-r", "--region", help="Chromosomal region (e.g. chrM, chrY:10000-20000)"
    )
    indel_parser.set_defaults(func=cmd_indel)

    annotate_parser = vcf_subs.add_parser(
        "annotate", parents=[base_parser], help=CLI_HELP["cmd_annotate"]
    )
    annotate_parser.add_argument(
        "--vcf-input",
        default=settings.get("default_input_vcf"),
        help=CLI_HELP["arg_vcf_input"],
    )
    annotate_parser.add_argument(
        "--ann-vcf", help="Annotation VCF file (auto-resolved from --ref if possible)"
    )
    annotate_parser.add_argument("--cols", help="Columns to annotate (e.g. ID,INFO/HG)")
    annotate_parser.set_defaults(func=cmd_annotate)

    filter_parser = vcf_subs.add_parser(
        "filter", parents=[base_parser], help=CLI_HELP["cmd_filter"]
    )
    filter_parser.add_argument(
        "--vcf-input",
        default=settings.get("default_input_vcf"),
        help=CLI_HELP["arg_vcf_input"],
    )
    filter_parser.add_argument(
        "--expr", help="bcftools filter expression (e.g. 'QUAL>30')"
    )
    filter_parser.add_argument("--gene", help="Filter by Gene Name (e.g. BRCA1, KCNQ2)")
    filter_parser.add_argument(
        "--exclude-near-gaps",
        action="store_true",
        help="Exclude variants in or near genomic gaps (requires Count Ns output)",
    )
    filter_parser.add_argument("-r", "--region", help="Chromosomal region")
    filter_parser.set_defaults(func=cmd_filter)

    trio_parser = vcf_subs.add_parser(
        "trio", parents=[base_parser], help=CLI_HELP["cmd_trio"]
    )
    trio_parser.add_argument(
        "--vcf-input",
        default=settings.get("default_input_vcf"),
        help=CLI_HELP["arg_vcf_input"],
    )
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
    trio_parser.add_argument("-r", "--region", help="Chromosomal region")
    trio_parser.set_defaults(func=cmd_trio)

    cnv_parser = vcf_subs.add_parser(
        "cnv", parents=[base_parser], help=CLI_HELP["cmd_cnv"]
    )
    cnv_parser.add_argument("-r", "--region", help="Chromosomal region")
    cnv_parser.add_argument(
        "-M", "--map", help="Mappability map file (required for delly cnv)"
    )
    cnv_parser.add_argument("--ploidy", help="Predefined ploidy name or value")
    cnv_parser.set_defaults(func=cmd_cnv)

    sv_parser = vcf_subs.add_parser(
        "sv", parents=[base_parser], help=CLI_HELP["cmd_sv"]
    )
    sv_parser.add_argument("-r", "--region", help="Chromosomal region")
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
    freebayes_parser.add_argument(
        "-r", "--region", help="Chromosomal region (e.g. chrM, chrY:10000-20000)"
    )
    freebayes_parser.set_defaults(func=cmd_freebayes)

    gatk_parser = vcf_subs.add_parser(
        "gatk", parents=[base_parser], help=CLI_HELP["cmd_gatk"]
    )
    gatk_parser.add_argument("-r", "--region", help="Chromosomal region (e.g. chrM)")
    gatk_parser.set_defaults(func=cmd_gatk)

    deepvariant_parser = vcf_subs.add_parser(
        "deepvariant", parents=[base_parser], help=CLI_HELP["cmd_deepvariant"]
    )
    deepvariant_parser.add_argument(
        "-r", "--region", help="Chromosomal region (e.g. chrM)"
    )
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
    clinvar_parser.add_argument(
        "--vcf-input",
        default=settings.get("default_input_vcf"),
        help="Optional override for VCF input file.",
    )
    clinvar_parser.add_argument(
        "--clinvar-file",
        default=settings.get("clinvar_vcf_path"),
        help="Path to ClinVar VCF data file.",
    )
    clinvar_parser.set_defaults(func=cmd_clinvar)

    revel_parser = vcf_subs.add_parser(
        "revel",
        parents=[base_parser],
        help="Annotate VCF with REVEL pathogenicity scores.",
    )
    revel_parser.add_argument(
        "--vcf-input",
        default=settings.get("default_input_vcf"),
        help="Optional override for VCF input file.",
    )
    revel_parser.add_argument(
        "--revel-file",
        default=settings.get("revel_tsv_path"),
        help="Path to REVEL TSV or VCF data file.",
    )
    revel_parser.add_argument(
        "--min-score",
        type=float,
        help="Minimum REVEL score to filter for (e.g., 0.5).",
    )
    revel_parser.set_defaults(func=cmd_revel)

    phylop_parser = vcf_subs.add_parser(
        "phylop",
        parents=[base_parser],
        help="Annotate VCF with PhyloP conservation scores.",
    )
    phylop_parser.add_argument(
        "--vcf-input",
        default=settings.get("default_input_vcf"),
        help="Optional override for VCF input file.",
    )
    phylop_parser.add_argument(
        "--phylop-file",
        default=settings.get("phylop_tsv_path"),
        help="Path to PhyloP TSV or VCF data file.",
    )
    phylop_parser.add_argument(
        "--min-score",
        type=float,
        help="Minimum PhyloP score to filter for (e.g., 2.0).",
    )
    phylop_parser.set_defaults(func=cmd_phylop)

    gnomad_parser = vcf_subs.add_parser(
        "gnomad",
        parents=[base_parser],
        help="Annotate VCF with gnomAD population frequencies.",
    )
    gnomad_parser.add_argument(
        "--vcf-input",
        default=settings.get("default_input_vcf"),
        help="Optional override for VCF input file.",
    )
    gnomad_parser.add_argument(
        "--gnomad-file",
        default=settings.get("gnomad_vcf_path"),
        help="Path to gnomAD VCF data file.",
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
    spliceai_parser.add_argument(
        "--vcf-input",
        default=settings.get("default_input_vcf"),
        help="Optional override for VCF input file.",
    )
    spliceai_parser.add_argument(
        "--spliceai-file",
        default=settings.get("spliceai_vcf_path"),
        help="Path to SpliceAI VCF data file.",
    )
    spliceai_parser.set_defaults(func=cmd_spliceai)

    alphamissense_parser = vcf_subs.add_parser(
        "alphamissense",
        parents=[base_parser],
        help="Annotate VCF with AlphaMissense pathogenicity scores.",
    )
    alphamissense_parser.add_argument(
        "--vcf-input",
        default=settings.get("default_input_vcf"),
        help="Optional override for VCF input file.",
    )
    alphamissense_parser.add_argument(
        "--am-file",
        default=settings.get("alphamissense_vcf_path"),
        help="Path to AlphaMissense VCF data file.",
    )
    alphamissense_parser.add_argument(
        "--min-score",
        type=float,
        help="Minimum AlphaMissense score to filter for (e.g., 0.5).",
    )
    alphamissense_parser.set_defaults(func=cmd_alphamissense)

    pharmgkb_parser = vcf_subs.add_parser(
        "pharmgkb",
        parents=[base_parser],
        help="Annotate VCF with PharmGKB drug metabolism data.",
    )
    pharmgkb_parser.add_argument(
        "--vcf-input",
        default=settings.get("default_input_vcf"),
        help="Optional override for VCF input file.",
    )
    pharmgkb_parser.add_argument(
        "--pharmgkb-file",
        default=settings.get("pharmgkb_vcf_path"),
        help="Path to PharmGKB VCF or data file.",
    )
    pharmgkb_parser.set_defaults(func=cmd_pharmgkb)

    chain_annotate_parser = vcf_subs.add_parser(
        "chain-annotate",
        parents=[base_parser],
        help="Sequentially apply multiple annotations to a single VCF.",
    )
    chain_annotate_parser.add_argument(
        "--vcf-input",
        default=settings.get("default_input_vcf"),
        help="Optional override for VCF input file.",
    )
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
