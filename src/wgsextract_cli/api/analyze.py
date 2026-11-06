"""``analyze``: whole-sample workflows that chain other commands.

``comprehensive`` runs a BAM/CRAM's info and lineage steps, calls or takes its
VCFs, annotates them and keeps the significant variants; a batch file runs
it for many samples. ``batch_gen`` writes such a
batch file from a folder of genomes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from wgsextract_cli.core.messages import CLI_HELP
from wgsextract_cli.core.results import CommandResult

from ._params import Params, param
from ._runtime import CancelToken, ProgressCallback, execute
from ._shared import (
    check_confidence,
    confidence_depth_param,
    confidence_gq_param,
    dry_run_param,
    force_steps_param,
    optional_input_param,
    outdir_param,
    ref_param,
    restart_param,
    threads_param,
)

ANALYZE_STEPS = (
    "info", "mt-haplogroup", "y-haplogroup", "snp", "indel", "annotate", "discovery",
)  # fmt: skip


@dataclass(frozen=True, kw_only=True)
class AnalyzeComprehensiveParams(Params):
    """Analyze a sample from end to end into ``outdir``: the BAM/CRAM
    ``input``'s info, mtDNA and Y haplogroups, and its ``vcf_inputs`` (else
    SNPs and InDels called from ``input``), each annotated and filtered to
    its significant variants (``significant_<type>_<name>.vcf.gz``; the
    result's ``significant_variants`` outputs). Completed steps are recorded
    in ``analyze.steps.json`` and skipped on a rerun whose inputs, settings
    and tools are unchanged.

    With neither ``input`` nor ``vcf_inputs``, config ``batch_file_path``
    or ``vcf_input_paths`` supply them."""

    COMMAND = "analyze comprehensive"
    # With no outdir it saves in the working folder, not beside its input.
    SAVES_IN_WORKING_FOLDER = True

    vcf_inputs: tuple[Path, ...] | None = param(
        None,
        help=CLI_HELP["arg_vcf_inputs"],
        doc="VCFs of the sample (SNV/InDel, CNV, SV; the type is read from "
        "the file name) to annotate instead of calling variants.",
        path="file",
    )
    skip_calling: bool = param(
        False,
        help="Without --vcf-inputs, don't call variants from --input; run only "
        "the BAM/CRAM metrics and lineage steps.",
        action="store_true",
    )
    batch: Path | None = param(
        None,
        help=CLI_HELP["arg_batch"],
        doc="CSV/TSV of samples (name, input, vcf columns) to analyze, each "
        "into its own folder in outdir.",
        path="file",
        # The paths it lists aren't checked by a server's path guard.
        local_only=True,
    )
    confidence_depth: tuple[float, float] | None = confidence_depth_param()
    confidence_gq: tuple[float, float] | None = confidence_gq_param()
    restart: bool = restart_param()
    force_steps: tuple[str, ...] | None = force_steps_param(ANALYZE_STEPS)
    dry_run: bool = dry_run_param()
    input: Path | None = optional_input_param("The sample's BAM or CRAM.")
    ref: Path | None = ref_param()
    outdir: Path | None = outdir_param("the current folder")
    threads: int | None = threads_param()

    def __post_init__(self) -> None:
        super().__post_init__()
        check_confidence(self)


@dataclass(frozen=True, kw_only=True)
class AnalyzeBatchGenParams(Params):
    """Write a batch file for ``comprehensive`` from the BAMs, CRAMs and
    VCFs under ``directory``, grouping files into samples by name
    (``sample1.bam`` and ``sample1_sv.vcf.gz`` are sample ``sample1``)."""

    COMMAND = "analyze batch-gen"

    directory: Path = param(
        help=CLI_HELP["arg_directory"],
        doc="Folder to scan, with its subfolders.",
        path="directory",
    )
    output: Path = param(
        Path("batch.csv"),
        help="Output batch file path.",
        doc="Batch file to write (CSV: name, input, vcf).",
        path="file",
    )


def comprehensive(
    params: AnalyzeComprehensiveParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Run the full-chain analysis (see ``AnalyzeComprehensiveParams``)."""
    from wgsextract_cli.commands.analyze import run_comprehensive

    return execute(
        params, run_comprehensive, progress=progress, cancel=cancel, check=check
    )


def batch_gen(
    params: AnalyzeBatchGenParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Write a batch file from a folder (see ``AnalyzeBatchGenParams``)."""
    from wgsextract_cli.commands.analyze import run_batch_gen

    return execute(params, run_batch_gen, progress=progress, cancel=cancel, check=check)
