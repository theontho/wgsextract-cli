"""Skip markers and tiny indexed files for tests that run samtools/bcftools/htslib."""

from collections.abc import Iterable, Mapping
from pathlib import Path

import pytest

from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.utils import run_command


def needs_tools(*tools: str) -> pytest.MarkDecorator:
    """Skip unless every tool resolves."""
    missing = [tool for tool in tools if not get_tool_path(tool)]
    return pytest.mark.skipif(bool(missing), reason=f"{'/'.join(missing)} missing")


needs_htslib = needs_tools("bcftools", "bgzip", "tabix", "samtools")


def bgzip_index(plain_vcf: str | Path, out: str | Path | None = None) -> str:
    """bgzip a plain VCF to ``out`` (default ``<plain_vcf>.gz``), remove the
    plain file, as ``bgzip`` does, and tabix the result."""
    out = str(out or f"{plain_vcf}.gz")
    with open(out, "wb") as f:
        run_command([get_tool_path("bgzip"), "-c", str(plain_vcf)], stdout=f)
    Path(plain_vcf).unlink()
    run_command([get_tool_path("tabix"), "-f", "-p", "vcf", out])
    return out


def write_fasta(path: Path, seqs: Mapping[str, str]) -> Path:
    """Write a FASTA, one line per sequence, and faidx it."""
    path.write_text("".join(f">{name}\n{seq}\n" for name, seq in seqs.items()))
    run_command([get_tool_path("samtools"), "faidx", str(path)])
    return path


def sam_to_bam(lines: Iterable[str], bam_path: Path) -> str:
    """Write SAM lines (header included) beside ``bam_path``, then sort and index."""
    sam = bam_path.with_suffix(".sam")
    sam.write_text("\n".join(lines) + "\n")
    samtools = get_tool_path("samtools")
    run_command([samtools, "sort", "-o", str(bam_path), str(sam)])
    run_command([samtools, "index", str(bam_path)])
    return str(bam_path)
