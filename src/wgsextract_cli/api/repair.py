"""``repair``: fix the formatting errors in Family Tree DNA (FTDNA) files that
make standard tools reject them.

``ftdna_bam`` replaces the spaces in read names with ``:``; ``ftdna_vcf``
drops the characters VCF forbids from the FILTER column. Each writes
``<input stem>_repaired.bam`` (or ``.vcf``) in ``outdir``, else beside the
input, or the ``output`` path. The result's metrics count the records read
(``records``) and changed (``repaired_records``). The CLI also repairs a
stream from stdin to stdout when ``--input`` is not given; the API reads and
writes files only."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wgsextract_cli.core.results import CommandResult

from ._params import Params, param
from ._runtime import CancelToken, ProgressCallback, execute
from ._shared import input_param, outdir_param, ref_param


def _output_param(kind: str, extension: str) -> Any:
    return param(
        None,
        help=f"Output repaired {kind} path. Defaults to "
        f"<outdir>/<input>_repaired{extension}.",
        doc=f"Repaired {kind} to write (default: "
        f"<outdir>/<input stem>_repaired{extension}); not the input itself.",
        path="file",
    )


class _RepairParams(Params):
    output: Path | None

    def write_folders(self) -> Iterator[Path]:
        if self.output is not None:
            yield self.output.parent


@dataclass(frozen=True, kw_only=True)
class RepairFtdnaBamParams(_RepairParams):
    """Copy an FTDNA BAM or CRAM to a BAM whose read names have no spaces
    (``FTDNA read 7`` becomes ``FTDNA:read:7``), so GATK and other tools
    accept it. A CRAM is decoded with ``ref``, else the reference it names."""

    COMMAND = "repair ftdna-bam"

    input: Path = input_param("The FTDNA BAM or CRAM to repair.")
    output: Path | None = _output_param("BAM", ".bam")
    ref: Path | None = ref_param()
    outdir: Path | None = outdir_param()


@dataclass(frozen=True, kw_only=True)
class RepairFtdnaVcfParams(_RepairParams):
    """Copy an FTDNA VCF (plain, gzipped or BCF) to a plain VCF whose FILTER
    values hold only the characters VCF allows (``Low Qual;q<10`` becomes
    ``LowQual;q10``), so bcftools and VEP accept it."""

    COMMAND = "repair ftdna-vcf"

    input: Path = input_param("The FTDNA VCF, .vcf.gz or BCF to repair.")
    output: Path | None = _output_param("VCF", ".vcf")
    outdir: Path | None = outdir_param()


def ftdna_bam(
    params: RepairFtdnaBamParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Repair an FTDNA BAM or CRAM's read names (see ``RepairFtdnaBamParams``)."""
    from wgsextract_cli.commands.repair import repair_bam_file

    return execute(
        params, repair_bam_file, progress=progress, cancel=cancel, check=check
    )


def ftdna_vcf(
    params: RepairFtdnaVcfParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Repair an FTDNA VCF's FILTER column (see ``RepairFtdnaVcfParams``)."""
    from wgsextract_cli.commands.repair import repair_vcf_file

    return execute(
        params, repair_vcf_file, progress=progress, cancel=cancel, check=check
    )


__all__ = [
    "RepairFtdnaBamParams",
    "RepairFtdnaVcfParams",
    "ftdna_bam",
    "ftdna_vcf",
]
