import argparse

from wgsextract_cli.core.config import settings
from wgsextract_cli.core.messages import CLI_HELP

from ._vep_resources import (
    cmd_vep_download,
    cmd_vep_verify,
)
from ._vep_run import (
    cmd_vep,
)


def register(
    subparsers: argparse._SubParsersAction, base_parser: argparse.ArgumentParser
) -> None:
    parser = subparsers.add_parser(
        "vep", parents=[base_parser], help=CLI_HELP["cmd_vep-run"]
    )

    vep_subs = parser.add_subparsers(dest="vep_cmd", required=False)

    # Download helper
    dl_parser = vep_subs.add_parser(
        "download", parents=[base_parser], help=CLI_HELP["cmd_vep-download"]
    )
    dl_parser.add_argument(
        "--species", default="homo_sapiens", help="Species name (default: homo_sapiens)"
    )
    dl_parser.add_argument(
        "--assembly",
        choices=["GRCh37", "GRCh38"],
        default="GRCh38",
        help="Assembly (default: GRCh38)",
    )
    dl_parser.add_argument(
        "--vep-version", default="115", help="Ensembl release version (default: 115)"
    )
    dl_parser.add_argument(
        "--mirror",
        choices=["us-east", "uk", "asia", "aws"],
        default="uk",
        help="Ensembl mirror to use (default: uk)",
    )
    dl_parser.add_argument("--vep-cache", help="Path to VEP cache directory")
    dl_parser.set_defaults(func=cmd_vep_download)

    # Verify helper
    verify_parser = vep_subs.add_parser(
        "verify", parents=[base_parser], help=CLI_HELP["cmd_vep-verify"]
    )
    verify_parser.add_argument(
        "--species", default="homo_sapiens", help="Species name (default: homo_sapiens)"
    )
    verify_parser.add_argument(
        "--assembly",
        choices=["GRCh37", "GRCh38"],
        default="GRCh38",
        help="Assembly (default: GRCh38)",
    )
    verify_parser.add_argument(
        "--vep-version", default="115", help="Ensembl release version (default: 115)"
    )
    verify_parser.add_argument("--vep-cache", help="Path to VEP cache directory")
    verify_parser.set_defaults(func=cmd_vep_verify)

    # Main run arguments
    parser.add_argument(
        "--vep-cache",
        default=settings.get("vep_cache_directory"),
        help="Path to VEP cache directory (e.g., $HOME/.vep)",
    )

    parser.add_argument(
        "--vep-assembly",
        choices=["GRCh37", "GRCh38"],
        help="Reference assembly for VEP (GRCh37 or GRCh38)",
    )
    parser.add_argument(
        "--vep-cache-version", default="115", help="VEP cache version (default: 115)"
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
        "--vep-args",
        help="Additional raw arguments to pass to VEP (e.g., '--everything --pick')",
    )
    parser.add_argument(
        "--format",
        choices=["vcf", "tab", "json"],
        default="vcf",
        help="Output format (default: vcf)",
    )

    # Variant Calling (if BAM/CRAM input)
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

    # Run helper (explicit subcommand for consistency)
    run_parser = vep_subs.add_parser(
        "run", parents=[base_parser], help=CLI_HELP["cmd_vep-run"]
    )
    # Add the same arguments to the run_parser
    for p in [parser, run_parser]:
        # Skip shared ones already in base_parser if they are not already there
        # but argparse handles parents automatically.
        # We need to add the vep-specific ones to both if we want both to work.
        if p == run_parser:
            p.add_argument(
                "--vep-cache",
                default=settings.get("vep_cache_directory"),
                help="Path to VEP cache directory (e.g., $HOME/.vep)",
            )

            p.add_argument(
                "--vep-assembly",
                choices=["GRCh37", "GRCh38"],
                help="Reference assembly for VEP (GRCh37 or GRCh38)",
            )
            p.add_argument(
                "--vep-cache-version",
                default="115",
                help="VEP cache version (default: 115)",
            )
            p.add_argument(
                "--vcf-type",
                choices=["auto", "snp-indel", "sv", "cnv"],
                default="auto",
                help="Type of variants (affects VEP args)",
            )
            p.add_argument(
                "--add-chr", action="store_true", help="Add 'chr' prefix to chromosomes"
            )
            p.add_argument(
                "--force",
                action="store_true",
                help="Force re-run even if output exists",
            )
            p.add_argument(
                "--vep-args",
                help="Additional raw arguments to pass to VEP (e.g., '--everything --pick')",
            )
            p.add_argument(
                "--format",
                choices=["vcf", "tab", "json"],
                default="vcf",
                help="Output format (default: vcf)",
            )
            p.add_argument(
                "--ploidy-file",
                help="File defining ploidy per chromosome (auto-resolved if possible)",
            )
            p.add_argument(
                "--ploidy", help="Predefined ploidy name or value (e.g., 'human')"
            )
            p.add_argument(
                "-r",
                "--region",
                help="Chromosomal region (e.g. chrM, chrY:10000-20000)",
            )
        p.set_defaults(func=cmd_vep)
