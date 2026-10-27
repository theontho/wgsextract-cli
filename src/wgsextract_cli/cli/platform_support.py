"""Reject unavailable Windows features before resolving inputs or creating outputs."""

import argparse
import sys

from wgsextract_cli.core.utils import WGSExtractError

_WINDOWS_VCF_TOOLS = {
    "phylop": "pyBigWig",
    "gerp": "pyBigWig",
    "cnv": "delly",
    "freebayes": "FreeBayes",
    "deepvariant": "DeepVariant",
    "pharmcat": "PharmCAT",
}
_WINDOWS_TYPING_TOOLS = {
    "cyp2d6": "Cyrius",
    "hla": "T1K",
    "str": "ExpansionHunter",
}


def verify_platform_support(args: argparse.Namespace) -> None:
    if sys.platform != "win32":
        return
    command = getattr(args, "command", None)
    tool = None
    if command == "vep":
        tool = "VEP"
    elif command == "vcf":
        subcommand = getattr(args, "vcf_cmd", None)
        tool = _WINDOWS_VCF_TOOLS.get(subcommand)
        if subcommand == "sv":
            tool = getattr(args, "caller", None) or (
                "pbsv/sniffles" if getattr(args, "pacbio", False) else "delly"
            )
    elif command == "typing":
        tool = _WINDOWS_TYPING_TOOLS.get(getattr(args, "typing_cmd", None))
    elif command == "ref" and getattr(args, "ref_cmd", None) in ("phylop", "gerp"):
        tool = "pyBigWig"
    elif command in ("align", "realign") and getattr(args, "aligner", None) == "pbmm2":
        tool = "pbmm2"
    if tool:
        raise WGSExtractError(
            f"{tool} is not supported on Windows; use this feature on Linux or macOS."
        )
