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


def vcf_calls(vcf: str | Path) -> list[tuple[str, ...]]:
    """(CHROM, POS, REF, ALT, GT) of each record of a single-sample VCF."""
    fmt = "%CHROM %POS %REF %ALT [%GT]\n"
    query = [get_tool_path("bcftools"), "query", "-f", fmt, str(vcf)]
    out = run_command(query, capture_output=True).stdout
    return [tuple(line.split()) for line in out.splitlines()]


def run_wgsextract(*argv: str) -> None:
    """Parse a command line as `wgsextract` does and run the command, letting
    its WGSExtractError propagate instead of exiting."""
    from wgsextract_cli.cli.parser import build_parser, parse_args

    args, _ = parse_args(*build_parser(), list(argv))
    args.func(args)


def write_read_pairs(
    genome: str, r1: Path, r2: Path, starts: Iterable[int], frag: int = 300
) -> None:
    """Write 100 bp FR read pairs from ``frag``-bp fragments of ``genome`` at
    ``starts``, named p0, p1, ..."""
    comp = str.maketrans("ACGT", "TGCA")
    with open(r1, "w") as f1, open(r2, "w") as f2:
        for i, start in enumerate(starts):
            piece = genome[start : start + frag]
            f1.write(f"@p{i}\n{piece[:100]}\n+\n{'I' * 100}\n")
            f2.write(f"@p{i}\n{piece[-100:].translate(comp)[::-1]}\n+\n{'I' * 100}\n")


def require_runnable(*tools: str) -> None:
    """Skip the running test unless the CLI's own dependency check passes, in
    the test's environment. Where a runtime finds a tool by name but can't run
    it (freebayes and GATK on the Windows runners' WSL), needs_tools passes and
    the command would refuse."""
    from wgsextract_cli.core.dependency_checks import verify_dependencies
    from wgsextract_cli.core.utils import WGSExtractError

    try:
        verify_dependencies(list(tools))
    except (WGSExtractError, SystemExit):
        pytest.skip(f"{'/'.join(tools)} not runnable here")
