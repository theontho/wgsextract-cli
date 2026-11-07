"""``vep``: annotate variants with the Ensembl Variant Effect Predictor (VEP).

``run`` annotates a VCF, BCF, BAM or CRAM, or every such file in a folder.
A BAM or CRAM is first called with bcftools against ``ref``. Each input
gets ``<input stem>_vep.vcf.gz`` (or ``.txt``/``.json`` with ``format``) and
VEP's summary in ``outdir``, else beside the input; an input whose output
already exists is skipped unless ``force``. The result's metrics count the
inputs and how many succeeded, were skipped or failed; any failure fails the
run. Downloading and checking the VEP cache (``vep download``/``verify``)
stay CLI-only.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.results import CommandResult

from ._params import Params, param
from ._runtime import CancelToken, ProgressCallback, execute
from ._shared import outdir_param, ref_param, threads_param


@dataclass(frozen=True, kw_only=True)
class VepRunParams(Params):
    """Annotate variants with VEP (see the module docstring)."""

    COMMAND = "vep run"

    input: Path = param(
        doc="VCF, BCF, BAM or CRAM to annotate, or a folder: every such file "
        "in it except earlier VEP outputs.",
        flags=("--input", "-i"),
        path="file_or_directory",
        shared=True,
        required_message=LOG_MESSAGES["input_required"],
    )
    ref: Path | None = ref_param()
    outdir: Path | None = outdir_param()
    threads: int | None = threads_param()
    vep_cache: Path | None = param(
        None,
        help="Path to VEP cache directory (e.g., $HOME/.vep)",
        doc="VEP cache folder (default: config vep_cache_directory, else the "
        "reference library's, else ~/.vep). VEP runs offline when it exists, "
        "else queries Ensembl's database.",
        path="directory",
        config_key="vep_cache_directory",
    )
    vep_assembly: str | None = param(
        None,
        help="Reference assembly for VEP (GRCh37 or GRCh38)",
        doc="Assembly VEP annotates against (default: the reference's build); "
        "a resolved FASTA of the other build is not used.",
        flags=("--assembly",),
        hidden_flags=("--vep-assembly",),
        choices=("GRCh37", "GRCh38"),
    )
    vep_cache_version: str = param("116", help="VEP cache version (default: 116)")
    vcf_type: str = param(
        "auto",
        help="Type of variants (affects VEP args)",
        doc="Variant type: 'sv' and 'cnv' add VEP's structural-variant options; "
        "'auto' detects it from the input.",
        choices=("auto", "snp-indel", "sv", "cnv"),
    )
    add_chr: bool = param(
        False,
        help="Add 'chr' prefix to chromosomes",
        doc="Give VEP the input with a 'chr' prefix on chromosome names.",
        action="store_true",
    )
    force: bool = param(
        False, help="Force re-run even if output exists", action="store_true"
    )
    preset: str = param(
        "everything",
        help="What VEP annotates: 'everything' (default), or 'fast' (about twice as "
        "fast: consequences, genes, MANE transcripts, SIFT and PolyPhen; no "
        "population frequencies, for which use 'vcf gnomad', and no HGVS)",
        choices=("everything", "fast"),
    )
    vep_args: str | None = param(
        None,
        help="Additional raw arguments to pass to VEP (e.g., '--everything --pick')",
        local_only=True,
    )
    format: str = param(
        "vcf", help="Output format (default: vcf)", choices=("vcf", "tab", "json")
    )
    ploidy_file: Path | None = param(
        None,
        help="File defining ploidy per chromosome (auto-resolved if possible)",
        doc="Ploidy file for calling a BAM or CRAM (default: the reference "
        "library's); wins over ploidy.",
        path="file",
    )
    ploidy: str | None = param(
        None,
        help="Predefined ploidy name or value (e.g., 'human')",
        doc="bcftools ploidy name or value for calling a BAM or CRAM "
        "(default: the build's).",
    )
    region: str | None = param(
        None,
        help="Chromosomal region (e.g. chrM, chrY:10000-20000)",
        flags=("-r", "--region"),
    )

    def write_folders(self) -> Iterator[Path]:
        # An unindexed .vcf.gz input is indexed beside itself.
        path = self.input.expanduser()
        yield path if path.is_dir() else path.parent


def run(
    params: VepRunParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Annotate variants with VEP (see ``VepRunParams``)."""
    from wgsextract_cli.commands._vep_run import run_vep

    return execute(params, run_vep, progress=progress, cancel=cancel, check=check)


__all__ = ["VepRunParams", "run"]
