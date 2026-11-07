"""``vep``: annotate variants with the Ensembl Variant Effect Predictor (VEP).

``run`` annotates a VCF, BCF, BAM or CRAM, or every such file in a folder.
A BAM or CRAM is first called with bcftools against ``ref``. Each input
gets ``<input stem>_vep.vcf.gz`` (or ``.txt``/``.json`` with ``format``) and
VEP's summary in ``outdir``, else beside the input; an input whose output
already exists is skipped unless ``force``. The result's metrics count the
inputs and how many succeeded, were skipped or failed; any failure fails the
run.

``download`` fetches Ensembl's indexed cache for ``species``, ``vep_version``
and ``assembly``, checks it against Ensembl's CHECKSUMS and unpacks it into
the cache folder; ``verify`` checks that an unpacked cache has its
``info.txt`` and chromosome folders. The cache folder is ``vep_cache``, else
config ``vep_cache_directory``, else the ``vep`` folder of the reference
library ``ref`` names, else ``~/.vep``.
"""

from __future__ import annotations

import re
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


@dataclass(frozen=True, kw_only=True)
class _VepCacheParams(Params):
    species: str = param("homo_sapiens", help="Species name (default: homo_sapiens)")
    assembly: str = param(
        "GRCh38", help="Assembly (default: GRCh38)", choices=("GRCh37", "GRCh38")
    )
    vep_version: str = param("116", help="Ensembl release version (default: 116)")
    vep_cache: Path | None = param(
        None,
        help="Path to VEP cache directory",
        doc="VEP cache folder (default: config vep_cache_directory, else the "
        "vep folder of the reference library ref names, else ~/.vep).",
        path="directory",
        config_key="vep_cache_directory",
    )
    ref: Path | None = param(
        None,
        doc="Reference library folder, or a FASTA in one, whose vep folder "
        "holds the cache when vep_cache isn't set (default: config "
        "reference_library).",
        flags=("--ref",),
        path="file_or_directory",
        shared=True,
    )

    def __post_init__(self) -> None:
        super().__post_init__()
        from wgsextract_cli.core.utils import UsageError

        # They name the cache's file and folders, which must stay in it.
        if not re.fullmatch(r"[A-Za-z0-9_]+", self.species):
            raise UsageError(f"Invalid --species {self.species!r}.")
        if not re.fullmatch(r"[0-9]+", self.vep_version):
            raise UsageError(f"Invalid --vep-version {self.vep_version!r}.")

    def cache_root(self) -> Path:
        """The cache folder (see the module docstring), ``~`` expanded."""
        from wgsextract_cli.commands._vep_resources import vep_cache_root

        return Path(vep_cache_root(self.vep_cache, self.ref))

    def version_dir(self) -> Path:
        """The folder of this species, release and assembly in the cache."""
        return self.cache_root() / self.species / f"{self.vep_version}_{self.assembly}"

    def implied_paths(self) -> Iterator[tuple[str, Path]]:
        yield "vep_cache", self.cache_root()


@dataclass(frozen=True, kw_only=True)
class VepDownloadParams(_VepCacheParams):
    """Download and unpack Ensembl's VEP cache (see the module docstring).

    The download resumes after an interruption or cancellation; a checksum
    mismatch deletes it. A cancelled run removes the tarball and what it
    unpacked; an unpacked cache replaces the same release's old one."""

    COMMAND = "vep download"

    mirror: str = param(
        "uk",
        help="Ensembl FTP mirror to use (default: uk)",
        choices=("ebi", "uk"),
    )

    def write_folders(self) -> Iterator[Path]:
        yield self.cache_root()


@dataclass(frozen=True, kw_only=True)
class VepVerifyParams(_VepCacheParams):
    """Check an unpacked VEP cache (see the module docstring). Warns about a
    missing ``info.txt`` or chromosome folder (metric ``missing_chromosomes``)
    and fails when the folder, its ``info.txt`` or every chromosome is
    missing."""

    COMMAND = "vep verify"


def download(
    params: VepDownloadParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Download and unpack the VEP cache (see ``VepDownloadParams``)."""
    from wgsextract_cli.commands._vep_resources import run_vep_download

    return execute(
        params, run_vep_download, progress=progress, cancel=cancel, check=check
    )


def verify(
    params: VepVerifyParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Check an unpacked VEP cache (see ``VepVerifyParams``)."""
    from wgsextract_cli.commands._vep_resources import run_vep_verify

    return execute(
        params, run_vep_verify, progress=progress, cancel=cancel, check=check
    )


__all__ = [
    "VepDownloadParams",
    "VepRunParams",
    "VepVerifyParams",
    "download",
    "run",
    "verify",
]
