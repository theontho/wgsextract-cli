"""Reject unavailable Windows features before resolving inputs or creating outputs."""

import argparse
import sys

from wgsextract_cli.core.reference_resolver import ReferenceLibrary
from wgsextract_cli.core.utils import WGSExtractError
from wgsextract_cli.core.windows_arm64 import (
    assert_native_windows_python,
    unsupported_windows_arm64_tool,
)

_WINDOWS_VCF_TOOLS = {
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


def _phylop_needs_bigwig(args: argparse.Namespace) -> bool:
    from wgsextract_cli.commands.vcf.basic import _select_vcf_input

    resource = getattr(args, "phylop_file", None)
    if not resource:
        # Library resolution reads metadata only, without preparing the input
        # or creating outputs. Keep table-backed annotation available.
        lib = ReferenceLibrary(
            getattr(args, "ref", None),
            input_path=_select_vcf_input(args),
        )
        resource = lib.phylop_file
    return not resource or str(resource).lower().endswith((".bw", ".bigwig"))


def verify_platform_support(args: argparse.Namespace) -> None:
    command = getattr(args, "command", None)
    if (
        not sys.platform.startswith("linux")
        and getattr(args, "gpu", False)
        and (
            command in ("align", "realign")
            or (
                command == "vcf"
                and getattr(args, "vcf_cmd", None) in ("gatk", "deepvariant")
            )
        )
    ):
        raise WGSExtractError(
            "Parabricks --gpu is not supported on this platform; "
            "use Linux with an NVIDIA GPU."
        )
    if sys.platform != "win32":
        return
    if command == "vcf":
        reason = unsupported_windows_arm64_tool(getattr(args, "vcf_cmd", "") or "")
        if reason:
            raise WGSExtractError(reason)
    try:
        assert_native_windows_python()
    except ValueError as error:
        raise WGSExtractError(str(error)) from error
    tool = None
    if command == "vep":
        tool = "VEP"
    elif command == "vcf":
        subcommand = getattr(args, "vcf_cmd", None)
        tool = _WINDOWS_VCF_TOOLS.get(subcommand)
        if subcommand == "phylop" and _phylop_needs_bigwig(args):
            tool = "pyBigWig"
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
