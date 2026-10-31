"""Native Windows ARM64 qualification; never treat Prism as native support."""

import ctypes
import platform
import struct
import sys
from pathlib import Path

IMAGE_FILE_MACHINE_ARM64 = 0xAA64


def windows_native_machine() -> int | None:
    """Query the OS architecture, including from an emulated Python process."""
    if sys.platform != "win32":
        return None
    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:
        # Allows platform simulation in non-Windows tests.
        return (
            IMAGE_FILE_MACHINE_ARM64
            if platform.machine().lower() in ("arm64", "aarch64")
            else None
        )
    kernel = loader("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    query = kernel.IsWow64Process2
    query.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ushort),
        ctypes.POINTER(ctypes.c_ushort),
    ]
    query.restype = ctypes.c_int
    process = ctypes.c_ushort()
    native = ctypes.c_ushort()
    if not query(
        kernel.GetCurrentProcess(), ctypes.byref(process), ctypes.byref(native)
    ):
        raise OSError(
            "IsWow64Process2 could not determine the native Windows architecture"
        )
    return int(native.value)


def unsupported_windows_arm64_tool(tool: str) -> str | None:
    if tool not in ("gatk", "deepvariant", "run_deepvariant"):
        return None
    if windows_native_machine() != IMAGE_FILE_MACHINE_ARM64:
        return None
    if tool == "gatk":
        return (
            "GATK is not supported on native Windows ARM64 (win-arm64) in phase 1: "
            "the pinned GKL PairHMM and Smith-Waterman kernels are x86-only. "
            "Java fallback, Prism and ARM64EC are not supported. "
            "Use `vcf snp` or `vcf indel` with native bcftools."
        )
    return (
        "DeepVariant is not supported on native Windows ARM64 (win-arm64): "
        "no qualified native toolchain is available. "
        "Prism, WSL and x86 containers are not supported. "
        "Use `vcf snp` or `vcf indel` with native bcftools."
    )


def pe_machine(path: Path) -> int:
    """Read only the PE headers; reject malformed/truncated files."""
    with path.open("rb") as stream:
        header = stream.read(64)
        if len(header) != 64 or header[:2] != b"MZ":
            raise ValueError(f"{path}: missing DOS header")
        offset = struct.unpack_from("<I", header, 60)[0]
        if offset < 64:
            raise ValueError(f"{path}: invalid PE offset")
        stream.seek(offset)
        coff = stream.read(24)
        if len(coff) != 24 or coff[:4] != b"PE\0\0":
            raise ValueError(f"{path}: missing PE/COFF header")
        return int(struct.unpack_from("<H", coff, 4)[0])


def assert_arm64_pe(path: Path) -> None:
    machine = pe_machine(path)
    if machine != IMAGE_FILE_MACHINE_ARM64:
        raise ValueError(
            f"{path}: PE machine 0x{machine:04X}; native ARM64 requires 0xAA64 "
            "(x64/ARM64EC/ARM64X are not accepted)"
        )
