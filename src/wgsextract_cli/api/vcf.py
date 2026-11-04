"""``vcf snp`` / ``vcf indel``: small-variant calling with bcftools."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from wgsextract_cli.core.results import CommandResult

from ._params import Params, param
from ._runtime import CancelToken, ProgressCallback, execute
from ._shared import (
    depth_param,
    input_param,
    outdir_param,
    ref_param,
    threads_param,
)


@dataclass(frozen=True, kw_only=True)
class _SmallVariantParams(Params):
    input: Path = input_param("BAM or CRAM to call.")
    ref: Path | None = ref_param()
    outdir: Path | None = outdir_param()
    threads: int | None = threads_param()
    ploidy_file: Path | None = param(
        None,
        help="File defining ploidy per chromosome (auto-resolved from --ref if possible)",
        path="file",
        exclusive="ploidy",
    )
    ploidy: str | None = param(
        None, help="Predefined ploidy name or value", exclusive="ploidy"
    )
    region: str | None = param(
        None,
        help="Chromosomal region (e.g. chrM, chrY:10000-20000)",
        flags=("-r", "--region"),
    )
    parallel: bool = param(
        False,
        help="Call contigs in parallel with up to --threads workers. Faster, but "
        "calls at sites deeper than 255 reads can differ slightly from the default "
        "single stream, because bcftools subsamples them with one random stream "
        "per process.",
        action="store_true",
    )
    qc_min_depth: float | None = depth_param()

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.ploidy is not None and self.ploidy_file is not None:
            from wgsextract_cli.core.utils import UsageError

            raise UsageError("Use either --ploidy or --ploidy-file, not both.")


@dataclass(frozen=True, kw_only=True)
class VcfSnpParams(_SmallVariantParams):
    """Call SNPs from a BAM/CRAM into ``snps.vcf.gz``.

    Ploidy comes from ``ploidy`` or ``ploidy_file``, else the reference
    library's ploidy file, else bcftools' built-in GRCh37/GRCh38 table.
    """

    COMMAND = "vcf snp"


@dataclass(frozen=True, kw_only=True)
class VcfIndelParams(_SmallVariantParams):
    """Call indels from a BAM/CRAM into ``indels.vcf.gz``; ploidy as for SNPs."""

    COMMAND = "vcf indel"


def snp(
    params: VcfSnpParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Call SNPs; the ``snps`` output carries the record count in its metrics."""
    from wgsextract_cli.commands.vcf.basic import run_small_variants

    return execute(
        params, run_small_variants, progress=progress, cancel=cancel, check=check
    )


def indel(
    params: VcfIndelParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Call indels; the ``indels`` output carries the record count in its metrics."""
    from wgsextract_cli.commands.vcf.basic import run_small_variants

    return execute(
        params, run_small_variants, progress=progress, cancel=cancel, check=check
    )


__all__ = ["VcfIndelParams", "VcfSnpParams", "indel", "snp"]
