"""Architecture limits for tools without a qualified native ARM64 runtime."""

import platform
import sys

from wgsextract_cli.core.utils import WGSExtractError


def is_linux_arm64() -> bool:
    return sys.platform.startswith("linux") and platform.machine().lower() in {
        "aarch64",
        "arm64",
    }


def tool_unsupported_reason(tool: str) -> str | None:
    if not is_linux_arm64():
        return None
    if tool == "gatk":
        return (
            "GATK is not supported on Linux ARM64 in phase 1: the pinned GKL "
            "native PairHMM/Smith-Waterman kernels are x86-only. A slow "
            "pure-Java fallback is not enabled. Use bcftools calling instead; "
            "native GKL support is tracked in #236 phase 2."
        )
    if tool == "parabricks":
        return (
            "Parabricks --gpu is not supported on Linux ARM64 in phase 1: "
            "no qualified native ARM64 image is available. x86 Docker images "
            "and emulation are not supported. Use the native CPU pipeline instead."
        )
    return None


def require_native_tool(tool: str) -> None:
    if reason := tool_unsupported_reason(tool):
        raise WGSExtractError(reason)
