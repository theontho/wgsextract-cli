"""``qc``: quality reports for reads and variants, and fake test data.

``fastp`` trims reads; ``fastqc``, ``vcf`` and ``multiqc`` report on reads,
variants and earlier results. Outputs go to ``outdir``, else beside the
input."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wgsextract_cli.core.builds import BUILD_CHOICES
from wgsextract_cli.core.messages import CLI_HELP
from wgsextract_cli.core.results import CommandResult
from wgsextract_cli.core.utils import UsageError

from ._params import Params, param
from ._runtime import CancelToken, ProgressCallback, execute
from ._shared import (
    input_param,
    optional_input_param,
    outdir_param,
    ref_param,
    threads_param,
)


@dataclass(frozen=True, kw_only=True)
class FastpParams(Params):
    """Trim and filter reads with fastp into ``<reads>_fp_1.fastq.gz`` (and
    ``_fp_2`` for pairs) plus JSON and HTML reports. The read counts, Q30
    rate and duplication rate are metrics."""

    COMMAND = "qc fastp"

    r1: Path | None = param(
        None, help=CLI_HELP["arg_r1"], path="file", doc="Read 1 FASTQ (required)."
    )
    r2: Path | None = param(
        None, help=CLI_HELP["arg_r2"], path="file", doc="Read 2 FASTQ, for pairs."
    )
    outdir: Path | None = outdir_param("the R1 file's folder")
    threads: int | None = threads_param()

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.r1 is None:
            # Optional on the command line, where --genome may find the reads.
            raise UsageError("--r1 is required unless --genome resolves FASTQ inputs.")


@dataclass(frozen=True, kw_only=True)
class FastqcParams(Params):
    """A FastQC report on a FASTQ, BAM or SAM file as ``<input>_fastqc.html``
    and ``.zip``. Module PASS/WARN/FAIL counts and the read count are metrics."""

    COMMAND = "qc fastqc"

    input: Path = input_param("FASTQ, BAM or SAM file.")
    outdir: Path | None = outdir_param()
    threads: int | None = threads_param()


def _reports_input(doc: str, required_message: str) -> Any:
    return param(
        doc=doc,
        flags=("--input", "-i"),
        path="file_or_directory",
        shared=True,
        required_message=required_message,
    )


@dataclass(frozen=True, kw_only=True)
class MultiqcParams(Params):
    """One MultiQC HTML report, ``multiqc_report.html``, from FastQC reports,
    ``bcftools stats`` files and WGS Extract result manifests. Runs offline,
    with MultiQC's AI summaries off."""

    COMMAND = "qc multiqc"

    input: Path = _reports_input(
        "A report or result manifest, or a folder searched for them.",
        "--input is required: choose FastQC reports, bcftools stats or result "
        "manifests.",
    )
    outdir: Path | None = outdir_param("the input folder, or the input's folder")
    force: bool = param(
        False,
        help="Replace an existing MultiQC report.",
        action="store_true",
        doc="Replace an existing report instead of failing.",
    )


@dataclass(frozen=True, kw_only=True)
class MultiqcMetricsParams(Params):
    """MultiQC custom-content files (``wgsextract_*_mqc.json``) from the
    metrics in WGS Extract result manifests; MultiQC isn't needed."""

    COMMAND = "qc multiqc-metrics"

    input: Path = _reports_input(
        "A result manifest, or a folder searched for them.",
        "--input is required: choose result manifests or a directory of them.",
    )
    outdir: Path | None = outdir_param("the input folder, or the input's folder")


@dataclass(frozen=True, kw_only=True)
class QcVcfParams(Params):
    """``bcftools stats`` on a VCF as ``<vcf>.vcfstats.txt``; record, SNP and
    indel counts and Ts/Tv are metrics. Set ``input`` or ``vcf_input``; the
    latter wins."""

    COMMAND = "qc vcf"

    input: Path | None = optional_input_param("VCF file.")
    vcf_input: Path | None = param(
        None, help=CLI_HELP["arg_vcf_input"], path="file", doc="VCF file."
    )
    outdir: Path | None = outdir_param()

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.input is None and self.vcf_input is None:
            raise UsageError("--input is required.")


@dataclass(frozen=True, kw_only=True)
class FakeDataParams(Params):
    """Generate a small fake genome for tests: reference, reads and calls
    named ``fake*``, reproducible from ``seed``. Uses the build's installed
    reference when there is one."""

    COMMAND = "qc fake-data"

    coverage: float = param(1.0, help="Coverage depth (e.g. 30.0)")
    build: str = param(
        "hg38",
        help="Human genome build naming convention.",
        choices=tuple(BUILD_CHOICES),
    )
    types: str = param(
        "cram",
        help="Comma-separated list of types to generate (vcf, cram, bam, fastq, "
        "all). Default: cram",
        flags=("--type",),
        dest="type",
    )
    full_size: bool = param(
        False,
        help="Use real human chromosome lengths. The default scaled mode uses "
        "shorter chromosomes.",
        action="store_true",
    )
    legacy_bam: bool = param(
        False,
        help="Use the older scaled fake BAM generator. Slower and unavailable "
        "with --full-size, but includes randomized placement and indel CIGARs.",
        action="store_true",
    )
    seed: int = param(42, help="Random seed for reproducibility")
    ref: Path | None = ref_param()
    outdir: Path | None = outdir_param("the current folder")


def fastp(
    params: FastpParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Trim the reads (output roles ``trimmed_fastq_r1``, ``trimmed_fastq_r2``,
    ``fastp_report_json`` and ``fastp_report_html``)."""
    from wgsextract_cli.commands.qc.commands import run_fastp

    return execute(params, run_fastp, progress=progress, cancel=cancel, check=check)


def fastqc(
    params: FastqcParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Run FastQC (output roles ``fastqc_report_html`` and ``fastqc_report_zip``)."""
    from wgsextract_cli.commands.qc.commands import run_fastqc

    return execute(params, run_fastqc, progress=progress, cancel=cancel, check=check)


def multiqc(
    params: MultiqcParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Write the MultiQC report (output role ``multiqc_report``)."""
    from wgsextract_cli.commands.qc.commands import run_multiqc

    return execute(params, run_multiqc, progress=progress, cancel=cancel, check=check)


def multiqc_metrics(
    params: MultiqcMetricsParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Write the custom-content files (output role ``multiqc_custom_content``)."""
    from wgsextract_cli.commands.qc.commands import run_multiqc_metrics

    return execute(
        params, run_multiqc_metrics, progress=progress, cancel=cancel, check=check
    )


def vcf(
    params: QcVcfParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Write the VCF statistics (output role ``vcf_stats``)."""
    from wgsextract_cli.commands.qc.commands import run_vcf_qc

    if params.vcf_input is not None:
        # Only the chosen VCF is read, recorded and checked.
        params = dataclasses.replace(params, input=params.vcf_input, vcf_input=None)
    return execute(params, run_vcf_qc, progress=progress, cancel=cancel, check=check)


def fake_data(
    params: FakeDataParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Generate the fake data (output roles ``reference``, ``alignment``,
    ``variants``, ``fastq_r1`` and ``fastq_r2``, by type)."""
    from wgsextract_cli.commands.qc import run_fake_data

    return execute(params, run_fake_data, progress=progress, cancel=cancel, check=check)


__all__ = [
    "FakeDataParams",
    "FastpParams",
    "FastqcParams",
    "MultiqcMetricsParams",
    "MultiqcParams",
    "QcVcfParams",
    "fake_data",
    "fastp",
    "fastqc",
    "multiqc",
    "multiqc_metrics",
    "vcf",
]
