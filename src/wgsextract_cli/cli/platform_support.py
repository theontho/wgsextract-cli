"""Reject unavailable features before resolving inputs or creating outputs."""

import argparse
import sys

from wgsextract_cli.core.native_support import require_native_tool
from wgsextract_cli.core.reference_resolver import ReferenceLibrary
from wgsextract_cli.core.utils import UsageError, WGSExtractError
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


def _require_for_option(tool: str) -> None:
    """A tool only an option (such as --gpu) asked for: refusing it is a usage
    error (exit 2), since the command runs without that option."""
    try:
        require_native_tool(tool)
    except WGSExtractError as error:
        raise UsageError(str(error)) from error


def verify_platform_support(args: argparse.Namespace) -> None:
    """Refuse what this platform can't run: a usage error (exit 2) when only
    the chosen options are unsupported, a failure (exit 1) when the command or
    the input itself is."""
    command = getattr(args, "command", None)
    if command in {"align", "realign"} and getattr(args, "gpu", False):
        _require_for_option("parabricks")
    if command in {"align", "realign"} and getattr(args, "platform", None) == "ultima":
        require_native_tool("gatk")
    if command == "deps" and getattr(args, "tool", None):
        require_native_tool(args.tool)
    if command == "vcf":
        subcommand = getattr(args, "vcf_cmd", None)
        if subcommand in {"gatk", "deepvariant"}:
            require_native_tool("gatk" if subcommand == "gatk" else "run_deepvariant")
            if getattr(args, "gpu", False):
                _require_for_option("parabricks")
        elif subcommand == "clair3":
            from wgsextract_cli.commands.vcf.clair3 import require_supported_platform

            require_supported_platform()
    if command == "methylation":
        from wgsextract_cli.commands.methylation import (
            require_supported_platform as require_modkit_platform,
        )

        require_modkit_platform()
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
        raise UsageError(
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
    # The tool is unsupported only because an option chose it.
    chosen_by_option = False
    if command == "vep":
        tool = "VEP"
    elif command == "vcf":
        subcommand = getattr(args, "vcf_cmd", "") or ""
        tool = _WINDOWS_VCF_TOOLS.get(subcommand)
        if subcommand == "phylop" and _phylop_needs_bigwig(args):
            tool = "pyBigWig"
        if subcommand == "sv":
            tool = getattr(args, "caller", None) or (
                "pbsv/sniffles"
                if getattr(args, "pacbio", False)
                else "sniffles"
                if getattr(args, "ont", False) or getattr(args, "long_read", False)
                else "delly"
            )
    elif command == "typing":
        tool = _WINDOWS_TYPING_TOOLS.get(getattr(args, "typing_cmd", "") or "")
    elif command == "ref" and getattr(args, "ref_cmd", None) in ("phylop", "gerp"):
        tool = "pyBigWig"
    elif command in ("align", "realign") and getattr(args, "aligner", None) == "pbmm2":
        tool = "pbmm2"
        chosen_by_option = True
    if tool:
        error = UsageError if chosen_by_option else WGSExtractError
        raise error(
            f"{tool} is not supported on Windows; use this feature on Linux or macOS."
        )
