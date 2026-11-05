import argparse
from collections.abc import Callable

from wgsextract_cli.api._cli import add_arguments
from wgsextract_cli.api._params import Params
from wgsextract_cli.api.vcf import (
    VcfAlphamissenseParams,
    VcfAnnotateParams,
    VcfCaddParams,
    VcfChainAnnotateParams,
    VcfClair3Params,
    VcfClinvarParams,
    VcfCnvParams,
    VcfDeepvariantParams,
    VcfFilterParams,
    VcfFreebayesParams,
    VcfGatkParams,
    VcfGerpParams,
    VcfGnomadParams,
    VcfIndelParams,
    VcfPharmcatParams,
    VcfPharmgkbParams,
    VcfPhylopParams,
    VcfRevelParams,
    VcfSnpParams,
    VcfSpliceaiParams,
    VcfSvParams,
    VcfTableParams,
    VcfToFastaParams,
    VcfTrioParams,
)
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
from .clair3 import cmd_clair3
from .clinvar_revel import (
    cmd_clinvar,
    cmd_revel,
)
from .consensus import cmd_to_fasta
from .deepvariant import cmd_deepvariant
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

    caller_commands: list[
        tuple[str, type[Params], Callable[[argparse.Namespace], None]]
    ] = [
        ("cnv", VcfCnvParams, cmd_cnv),
        ("sv", VcfSvParams, cmd_sv),
        ("freebayes", VcfFreebayesParams, cmd_freebayes),
        ("gatk", VcfGatkParams, cmd_gatk),
        ("deepvariant", VcfDeepvariantParams, cmd_deepvariant),
        ("clair3", VcfClair3Params, cmd_clair3),
    ]
    for name, caller_cls, caller_func in caller_commands:
        caller_parser = vcf_subs.add_parser(
            name, parents=[base_parser], help=CLI_HELP[f"cmd_{name}"]
        )
        add_arguments(caller_parser, caller_cls)
        caller_parser.set_defaults(func=caller_func)

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
