import argparse
from collections.abc import Callable

from wgsextract_cli.api._cli import add_arguments
from wgsextract_cli.api._params import Params
from wgsextract_cli.api.vcf import (
    VcfAlphamissenseParams,
    VcfAnnotateParams,
    VcfCaddParams,
    VcfChainAnnotateParams,
    VcfClinvarParams,
    VcfFilterParams,
    VcfGerpParams,
    VcfGnomadParams,
    VcfIndelParams,
    VcfPharmcatParams,
    VcfPharmgkbParams,
    VcfPhylopParams,
    VcfRevelParams,
    VcfSnpParams,
    VcfSpliceaiParams,
    VcfTableParams,
    VcfToFastaParams,
    VcfTrioParams,
)
from wgsextract_cli.core.config import settings
from wgsextract_cli.core.genotype_qc import add_depth_argument
from wgsextract_cli.core.messages import CLI_HELP
from wgsextract_cli.core.steps import add_resume_arguments

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
from .clair3 import cmd_clair3
from .clinvar_revel import (
    cmd_clinvar,
    cmd_revel,
)
from .consensus import cmd_to_fasta
from .deepvariant import (
    STEPS as DEEPVARIANT_STEPS,
)
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
from .table import cmd_table


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
        help="Call contigs in parallel with up to --threads workers. SNP calling "
        "then recalls the stretches with sites deeper than 255 reads in one "
        "stream, because bcftools subsamples them with one random stream per "
        "process, so the calls match the default single stream.",
    )


def _add_vcf_command(
    vcf_subs: "argparse._SubParsersAction[argparse.ArgumentParser]",
    base_parser: argparse.ArgumentParser,
    name: str,
    help_text: str,
    cls: type[Params],
    func: Callable[[argparse.Namespace], None],
) -> None:
    """A command on a VCF; --vcf-input defaults to config default_input_vcf."""
    parser = vcf_subs.add_parser(name, parents=[base_parser], help=help_text)
    add_arguments(parser, cls, config_defaults=True)
    parser.set_defaults(func=func)
    # A parse-time default, as for the shared options, so dispatch records it.
    for action in parser._actions:
        if action.dest == "vcf_input":
            action.default = settings.get("default_input_vcf")


def register(
    subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",
    base_parser: argparse.ArgumentParser,
) -> None:
    parser = subparsers.add_parser(
        "vcf",
        help=CLI_HELP["cmd_vcf"],
    )
    vcf_subs = parser.add_subparsers(dest="vcf_cmd", required=True)

    snp_parser = vcf_subs.add_parser(
        "snp", parents=[base_parser], help=CLI_HELP["cmd_snp"]
    )
    add_arguments(snp_parser, VcfSnpParams)
    snp_parser.set_defaults(func=cmd_snp)

    indel_parser = vcf_subs.add_parser(
        "indel", parents=[base_parser], help=CLI_HELP["cmd_indel"]
    )
    add_arguments(indel_parser, VcfIndelParams)
    indel_parser.set_defaults(func=cmd_indel)

    _add_vcf_command(
        vcf_subs,
        base_parser,
        "annotate",
        CLI_HELP["cmd_annotate"],
        VcfAnnotateParams,
        cmd_annotate,
    )
    _add_vcf_command(
        vcf_subs,
        base_parser,
        "filter",
        CLI_HELP["cmd_filter"],
        VcfFilterParams,
        cmd_filter,
    )
    _add_vcf_command(
        vcf_subs,
        base_parser,
        "table",
        "Export variants and their annotations (gene, consequence, ClinVar, "
        "gnomAD, genotype, depth, ...) as one row per variant, in a TSV (or "
        "Parquet) table and an SQLite database to query.",
        VcfTableParams,
        cmd_table,
    )
    _add_vcf_command(
        vcf_subs,
        base_parser,
        "to-fasta",
        CLI_HELP["cmd_vcf-to-fasta"],
        VcfToFastaParams,
        cmd_to_fasta,
    )
    _add_vcf_command(
        vcf_subs, base_parser, "trio", CLI_HELP["cmd_trio"],
        VcfTrioParams, cmd_trio,
    )  # fmt: skip

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
    add_depth_argument(cnv_parser)
    cnv_parser.set_defaults(func=cmd_cnv)

    sv_parser = vcf_subs.add_parser(
        "sv", parents=[base_parser], help=CLI_HELP["cmd_sv"]
    )
    _add_region(sv_parser)
    sv_parser.add_argument(
        "--caller",
        choices=["delly", "pbsv", "sniffles"],
        help="SV caller to use (default: sniffles for detected ONT, else delly; "
        "pbsv/sniffles with --pacbio; sniffles with --ont or --long-read). "
        "pbsv is PacBio-only and is refused for ONT reads.",
    )
    sv_parser.add_argument(
        "--pacbio",
        action="store_true",
        help="PacBio HiFi/CCS data: use pbsv (or sniffles if pbsv is missing) "
        "with CCS thresholds.",
    )
    sv_parser.add_argument(
        "--ont",
        action="store_true",
        help="Oxford Nanopore data: use Sniffles (detected ONT input selects it "
        "automatically).",
    )
    sv_parser.add_argument(
        "--long-read",
        action="store_true",
        help="Long-read data from any platform: use Sniffles unless --caller "
        "selects pbsv for PacBio reads.",
    )
    sv_parser.add_argument(
        "--ccs",
        action="store_true",
        help="Use pbsv CCS/HiFi thresholds when --caller pbsv is selected.",
    )
    sv_parser.add_argument(
        "--tandem-repeats",
        help="Optional tandem-repeat BED for pbsv discover or Sniffles "
        "(recommended for long-read SV calling).",
    )
    add_depth_argument(sv_parser)
    sv_parser.set_defaults(func=cmd_sv)

    freebayes_parser = vcf_subs.add_parser(
        "freebayes", parents=[base_parser], help=CLI_HELP["cmd_freebayes"]
    )
    _add_region(freebayes_parser, "Chromosomal region (e.g. chrM, chrY:10000-20000)")
    add_depth_argument(freebayes_parser)
    freebayes_parser.set_defaults(func=cmd_freebayes)

    gatk_parser = vcf_subs.add_parser(
        "gatk", parents=[base_parser], help=CLI_HELP["cmd_gatk"]
    )
    _add_region(gatk_parser, "Chromosomal region (e.g. chrM)")
    gatk_parser.add_argument(
        "--scatter",
        action="store_true",
        help="Opt-in Ultima flow-mode interval scattering, bounded by --threads "
        "and available RAM (6 GiB per worker, 4 GiB Java heap). "
        "Can change genotypes/annotations away from interval boundaries; "
        "default: one unscattered process.",
    )
    gatk_parser.add_argument(
        "--gpu",
        action="store_true",
        help="Call on an NVIDIA GPU with Parabricks (Linux, Docker; see README).",
    )
    add_depth_argument(gatk_parser)
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
        choices=[
            "WGS",
            "WES",
            "PACBIO",
            "HYBRID_PACBIO_ILLUMINA",
            "ULTIMA",
            "ONT_R104",
        ],
        help="DeepVariant model type (default: ONT_R104 for detected ONT, "
        "ULTIMA for Ultima reads with their flow tags, else WGS). "
        "ONT_R104 supports R10.4.1 only and requires x86-64; "
        "Linux ARM64 supports WGS only. Use PACBIO for PacBio HiFi/CCS alignments.",
    )
    deepvariant_parser.add_argument(
        "--ont",
        action="store_true",
        help="Alias for --model-type ONT_R104 (R10.4.1 only; x86-64).",
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
        "--batch-size",
        type=int,
        help="CPU inference batch size (upstream default: 1024). "
        "Use 256 to lower TensorFlow peak RAM without changing models or precision; "
        "--threads controls make_examples shards, not this batch.",
    )
    deepvariant_parser.add_argument(
        "--gpu",
        action="store_true",
        help="Call on an NVIDIA GPU with Parabricks (Linux, Docker; see README).",
    )
    add_depth_argument(deepvariant_parser)
    add_resume_arguments(deepvariant_parser, DEEPVARIANT_STEPS)
    deepvariant_parser.set_defaults(func=cmd_deepvariant)

    clair3_parser = vcf_subs.add_parser(
        "clair3", parents=[base_parser], help=CLI_HELP["cmd_clair3"]
    )
    _add_region(clair3_parser, "Chromosomal region (e.g. chr20:1-1000000)")
    clair3_parser.add_argument(
        "--model",
        metavar="NAME|DIR",
        help="Clair3 model directory, or the name of a bundled model, matching the "
        "reads' basecaller (e.g. r1041_e82_400bps_sup_v500). Overrides config "
        "clair3_model; default: chosen from the basecaller metadata.",
    )
    add_depth_argument(clair3_parser)
    clair3_parser.set_defaults(func=cmd_clair3)

    for name, help_text, cls, func in (
        ("clinvar", "Annotate VCF with ClinVar pathogenicity data.",
         VcfClinvarParams, cmd_clinvar),
        ("revel", "Annotate VCF with REVEL pathogenicity scores.",
         VcfRevelParams, cmd_revel),
        ("phylop", "Annotate VCF with PhyloP conservation scores.",
         VcfPhylopParams, cmd_phylop),
        ("cadd", "Annotate VCF with CADD deleteriousness scores (CADD_RAW, "
         "CADD_PHRED).", VcfCaddParams, cmd_cadd),
        ("gerp", "Annotate VCF with GERP++ conservation scores (GERP).",
         VcfGerpParams, cmd_gerp),
        ("gnomad", "Annotate VCF with gnomAD population frequencies.",
         VcfGnomadParams, cmd_gnomad),
        ("spliceai", "Annotate VCF with SpliceAI splicing scores.",
         VcfSpliceaiParams, cmd_spliceai),
        ("alphamissense", "Annotate VCF with AlphaMissense pathogenicity scores.",
         VcfAlphamissenseParams, cmd_alphamissense),
        ("pharmgkb", "Annotate VCF with PharmGKB drug metabolism data.",
         VcfPharmgkbParams, cmd_pharmgkb),
        ("pharmcat", "Call star-allele diplotypes and drug-response phenotypes "
         "(CYP2C19, CYP2C9, DPYD, ...) with PharmCAT, and write its prescribing "
         "report.", VcfPharmcatParams, cmd_pharmcat),
        ("chain-annotate", "Sequentially apply multiple annotations to a single "
         "VCF.", VcfChainAnnotateParams, cmd_chain_annotate),
    ):  # fmt: skip
        _add_vcf_command(vcf_subs, base_parser, name, help_text, cls, func)
