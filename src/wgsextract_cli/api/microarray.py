"""``microarray``: consumer microarray (23andMe, AncestryDNA, ...) and AADR
files generated from a BAM/CRAM or VCF."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from wgsextract_cli.core.messages import CLI_HELP
from wgsextract_cli.core.microarray_quirks import MODES
from wgsextract_cli.core.results import CommandResult

from ._params import Params, param
from ._runtime import CancelToken, ProgressCallback, execute
from ._shared import depth_param, input_param, outdir_param, ref_param, threads_param


@dataclass(frozen=True, kw_only=True)
class MicroarrayParams(Params):
    """Generate a combined 23andMe-style kit and the selected vendor or AADR
    formats from a BAM, CRAM or VCF.

    Fields left at ``None`` fall back to config.toml (``microarray_*``
    settings) and then to the documented default.
    """

    COMMAND = "microarray"

    input: Path = input_param("BAM, CRAM or VCF to genotype.")
    ref: Path | None = ref_param()
    outdir: Path | None = outdir_param()
    threads: int | None = threads_param()
    formats: str = param(
        "all",
        help=CLI_HELP["micro_formats_help"],
        doc="Comma-separated formats to generate, e.g. "
        '"23andme_v5,ancestry_v2" (default: "all", the combined kit only); see '
        "`wgsextract microarray --help` for the list.",
    )
    indel_manifest: tuple[str, ...] = param(
        (),
        help="Opt-in sequence-defined I/D probes from a matching GRCh37 Illumina "
        "CSV, gzip or ZIP; repeat per selected format",
        metavar="FORMAT=PATH",
        action="append",
    )
    strand_ambiguous: str | None = param(
        None,
        help="Keep/no-call biallelic A/T or C/G input SNPs; unknown probe alleles "
        "are not inferred (default: config or keep)",
        choices=("keep", "no-call"),
    )
    resolve_rsids: bool | None = param(
        None,
        help="Resolve packaged dbSNP aliases and no-call retired/ambiguous marker "
        "IDs (default: config or false)",
        action="boolean_optional",
    )
    quirks: str | None = param(
        None,
        help="corrected: forward-strand calls; vendor: reverse-strand calls where "
        "a template's vendor revision reported them so (default: config or "
        "corrected)",
        choices=MODES,
    )
    min_gq: int | None = param(
        None,
        help="Minimum sample GQ; missing GQ becomes a no-call (default: config or "
        "0, disabled)",
    )
    min_dp: int | None = param(
        None,
        help="Minimum sample DP; missing DP becomes a no-call (default: config or "
        "0, disabled)",
    )
    assume_hom_ref: bool | None = param(
        None,
        help="Assume absent VCF markers are reference on represented contigs "
        "(default: config or true)",
        action="boolean_optional",
    )
    exclude_filtered: bool | None = param(
        None,
        help="No-call VCF records failing FILTER (default: config or false)",
        action="boolean_optional",
    )
    call_reasons: bool | None = param(
        None,
        help="Also write CombinedKit_reasons.tsv with explicit reasons (default: "
        "config or false)",
        action="boolean_optional",
    )
    aadr_sample_id: str = param(
        "sample",
        help="Non-identifying sample label for AADR .fam/.ind (default: sample)",
    )
    aadr_sex: str = param(
        "U",
        help="AADR metadata sex only; does not change genotype calling (default: U)",
        choices=("U", "M", "F"),
    )
    parallel: bool = param(
        False,
        help="Enable per-chromosome parallel variant calling",
        action="store_true",
    )
    ref_vcf_tab: Path | None = param(
        None,
        help="Master tabulated list of all consumer microarray SNPs (auto-resolved "
        "from --ref if possible)",
        path="file",
    )
    ploidy_file: Path | None = param(
        None,
        help="File defining ploidy per chromosome (auto-resolved from --ref if "
        "possible)",
        path="file",
    )
    sample: str | None = param(
        None,
        help="Sample to use from a multi-sample VCF (e.g. a joint-called "
        "nf-core/sarek VCF); required when the VCF has more than one",
    )
    region: str | None = param(
        None, help=CLI_HELP["arg_region"], flags=("-r", "--region")
    )
    qc_min_depth: float | None = depth_param()


def generate(
    params: MicroarrayParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Generate the kits; each output carries its marker tallies in ``metrics``."""
    from wgsextract_cli.commands.microarray import run_microarray

    return execute(
        params, run_microarray, progress=progress, cancel=cancel, check=check
    )
