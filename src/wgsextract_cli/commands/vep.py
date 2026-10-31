import argparse

from wgsextract_cli.core.config import settings
from wgsextract_cli.core.messages import CLI_HELP

from ._vep_resources import (
    VEP_MIRRORS,
    cmd_vep_download,
    cmd_vep_verify,
)
from ._vep_run import (
    cmd_vep,
)


def _add_run_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--vep-cache",
        default=settings.get("vep_cache_directory"),
        help="Path to VEP cache directory (e.g., $HOME/.vep)",
    )
    parser.add_argument(
        "--assembly",
        dest="vep_assembly",
        choices=["GRCh37", "GRCh38"],
        help="Reference assembly for VEP (GRCh37 or GRCh38)",
    )
    parser.add_argument(
        "--vep-assembly",
        dest="vep_assembly",
        choices=["GRCh37", "GRCh38"],
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--vep-cache-version", default="116", help="VEP cache version (default: 116)"
    )
    parser.add_argument(
        "--vcf-type",
        choices=["auto", "snp-indel", "sv", "cnv"],
        default="auto",
        help="Type of variants (affects VEP args)",
    )
    parser.add_argument(
        "--add-chr", action="store_true", help="Add 'chr' prefix to chromosomes"
    )
    parser.add_argument(
        "--force", action="store_true", help="Force re-run even if output exists"
    )
    parser.add_argument(
        "--preset",
        choices=["everything", "fast"],
        default="everything",
        help="What VEP annotates: 'everything' (default), or 'fast' (about twice as "
        "fast: consequences, genes, MANE transcripts, SIFT and PolyPhen; no "
        "population frequencies, for which use 'vcf gnomad', and no HGVS)",
    )
    parser.add_argument(
        "--vep-args",
        help="Additional raw arguments to pass to VEP (e.g., '--everything --pick')",
    )
    parser.add_argument(
        "--format",
        choices=["vcf", "tab", "json"],
        default="vcf",
        help="Output format (default: vcf)",
    )
    # Variant calling options, used when the input is BAM/CRAM.
    parser.add_argument(
        "--ploidy-file",
        help="File defining ploidy per chromosome (auto-resolved if possible)",
    )
    parser.add_argument(
        "--ploidy", help="Predefined ploidy name or value (e.g., 'human')"
    )
    parser.add_argument(
        "-r", "--region", help="Chromosomal region (e.g. chrM, chrY:10000-20000)"
    )
    parser.set_defaults(func=cmd_vep)


def _add_cache_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--species", default="homo_sapiens", help="Species name (default: homo_sapiens)"
    )
    parser.add_argument(
        "--assembly",
        choices=["GRCh37", "GRCh38"],
        default="GRCh38",
        help="Assembly (default: GRCh38)",
    )
    parser.add_argument(
        "--vep-version", default="116", help="Ensembl release version (default: 116)"
    )
    parser.add_argument("--vep-cache", help="Path to VEP cache directory")


def register(
    subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",
    base_parser: argparse.ArgumentParser,
) -> None:
    parser = subparsers.add_parser(
        "vep", parents=[base_parser], help=CLI_HELP["cmd_vep"]
    )
    # `wgsextract vep ...` and `wgsextract vep run ...` both run VEP.
    _add_run_args(parser)
    vep_subs = parser.add_subparsers(dest="vep_cmd", required=False)

    dl_parser = vep_subs.add_parser(
        "download", parents=[base_parser], help=CLI_HELP["cmd_vep-download"]
    )
    _add_cache_args(dl_parser)
    dl_parser.add_argument(
        "--mirror",
        choices=sorted(VEP_MIRRORS),
        default="uk",
        help="Ensembl FTP mirror to use (default: uk)",
    )
    dl_parser.set_defaults(func=cmd_vep_download)

    verify_parser = vep_subs.add_parser(
        "verify", parents=[base_parser], help=CLI_HELP["cmd_vep-verify"]
    )
    _add_cache_args(verify_parser)
    verify_parser.set_defaults(func=cmd_vep_verify)

    run_parser = vep_subs.add_parser(
        "run", parents=[base_parser], help=CLI_HELP["cmd_vep-run"]
    )
    _add_run_args(run_parser)
