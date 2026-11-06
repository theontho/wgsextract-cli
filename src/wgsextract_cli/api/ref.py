"""``ref``: the reference library: genomes, their indexes and N counts, and
the annotation data and panels other commands read.

``ref`` names a FASTA (``index``, ``count_ns``, ``verify``) or the reference
library folder, or a FASTA inside it; unset, it is config.toml's
``reference_fasta``, else the configured or default library. Installers write
into the library and record what they wrote as the result's outputs::

    api.ref.verify(api.RefVerifyParams(ref=Path("lib/genomes/hs38DH.fa.gz")))
    api.ref.status(api.RefStatusParams()).details["genomes"]
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wgsextract_cli.core.results import CommandResult
from wgsextract_cli.core.utils import UsageError

from ._params import Params, param
from ._runtime import CancelToken, ProgressCallback, execute
from ._shared import optional_input_param, ref_param, threads_param

BUILDS = ("hg38", "hg19")
GNOMAD_RELEASES = ("2.1.1", "4.1")
ALIGNER_INDEXES = ("bwa", "bwa-mem2")


def _fasta_param() -> Any:
    return param(
        None,
        doc="The reference FASTA (required; default: config reference_fasta).",
        flags=("--ref",),
        path="file",
        shared=True,
        config_key="reference_fasta",
    )


def _build_param(help: str) -> Any:
    return param("hg38", help=help, choices=BUILDS)


class _InLibrary:
    """An operation on the library ``ref`` names, or the one above a FASTA's
    ``genomes`` folder."""

    ref: Path | None

    def implied_paths(self) -> Iterator[tuple[str, Path]]:
        from wgsextract_cli.core.reference_assets import reference_library_of

        if self.ref is not None and Path(self.ref).is_file():
            yield "ref", Path(reference_library_of(self.ref))


@dataclass(frozen=True, kw_only=True)
class RefAadrParams(_InLibrary, Params):
    """Install the hash-pinned CC0 AADR v66.p1 2M SNP panel into the library."""

    COMMAND = "ref aadr"

    ref: Path | None = ref_param()


@dataclass(frozen=True, kw_only=True)
class RefYbrowseParams(_InLibrary, Params):
    """Install a hash-pinned, sites-only GRCh38 yBrowse snapshot into the
    library."""

    COMMAND = "ref ybrowse"

    snapshot: Path | None = param(
        None,
        help="Use a local GRCh38 VCF(.gz), verifying its header instead of the "
        "upstream pin.",
        path="file",
        cli_type=Path,
    )
    ref: Path | None = ref_param()


@dataclass(frozen=True, kw_only=True)
class RefDownloadParams(Params):
    """Download a reference FASTA from ``url`` to ``out``."""

    COMMAND = "ref download"
    # A server must not fetch arbitrary URLs for its clients.
    LOCAL_ONLY = True

    url: str = param(help="URL to download from")
    out: Path = param(help="Output FASTA file path", path="file")


@dataclass(frozen=True, kw_only=True)
class RefIndexParams(Params):
    """Index a FASTA: ``.fai`` (and ``.gzi``), the ``.dict`` sequence
    dictionary and the aligner's index, written beside it."""

    COMMAND = "ref index"
    WRITES_BESIDE = "ref"

    ref: Path | None = _fasta_param()
    aligner: str = param(
        "bwa",
        help="Aligner index to make. bwa-mem2's maps faster but takes 17 GB of "
        "memory for a human genome; hs38DH's is downloaded, others need ~90 GB "
        "of memory to build.",
        choices=ALIGNER_INDEXES,
    )


@dataclass(frozen=True, kw_only=True)
class RefCountNsParams(Params):
    """Count the FASTA's N runs into ``<ref>_ncnt.csv`` and ``<ref>_nbin.csv``;
    sequence, length and N totals are metrics."""

    COMMAND = "ref count-ns"
    WRITES_BESIDE = "ref"

    ref: Path | None = _fasta_param()
    outdir: Path | None = param(
        None,
        doc="Folder for the N-count files (default: beside the FASTA; config "
        "output_directory is not used).",
        flags=("--outdir", "-o"),
        path="directory",
        shared=True,
    )


@dataclass(frozen=True, kw_only=True)
class RefVerifyParams(Params):
    """Check a FASTA's compression, its ``.fai``/``.gzi`` and ``.dict`` against
    the file, and its sequence signature against the library catalog. Writes
    a missing ``.fai``/``.gzi``; ``contigs`` and ``signature`` are metrics."""

    COMMAND = "ref verify"
    WRITES_BESIDE = "ref"

    ref: Path | None = param(
        None,
        doc="The reference FASTA, or a folder holding one (required; default: "
        "config reference_fasta).",
        flags=("--ref",),
        path="file_or_directory",
        shared=True,
        config_key="reference_fasta",
    )


@dataclass(frozen=True, kw_only=True)
class RefLibraryParams(_InLibrary, Params):
    """Install a catalog genome into the library's ``genomes`` folder, trying
    each mirror that hosts it."""

    COMMAND = "ref library"

    install: str | None = param(
        None,
        help="Non-interactively install a genome by its code (e.g., hs38) or "
        "index (e.g., 9).",
        doc="The genome's catalog code (e.g. hs38) or 1-based index (required).",
    )
    ref: Path | None = ref_param()

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.install:
            raise UsageError(
                "Set install to a genome's code or index; `wgsextract ref "
                "library --list` lists them."
            )


@dataclass(frozen=True, kw_only=True)
class RefStatusParams(Params):
    """What the reference library holds, as the result's ``details``: genomes
    and their indexes, annotation data, gene maps, VEP cache, PharmCAT,
    ploidy and mappability files, and the test genome."""

    COMMAND = "ref status"

    ref: Path | None = param(
        None,
        doc="Reference library folder, or a FASTA in it (default: config "
        "reference_library, else reference_fasta's library, else the "
        "default library).",
        flags=("--ref",),
        path="file_or_directory",
        shared=True,
    )
    input: Path | None = optional_input_param(
        "A BAM/CRAM or VCF whose build picks the annotation files to report."
    )
    genome_library: Path | None = param(
        None,
        help="Genome library directory used for test genome status.",
        doc="Genome library folder for the test genome's status (default: "
        "config genome_library, else ``genomes`` beside the reference library).",
        path="directory",
    )
    annotation_vcf: Path | None = param(
        None,
        help="Custom annotation VCF path to include in readiness checks.",
        path="file",
    )

    def implied_paths(self) -> Iterator[tuple[str, Path]]:
        from wgsextract_cli.core.config import settings
        from wgsextract_cli.core.reference_assets import reference_library_of

        if self.ref is None or not Path(self.ref).exists():
            return
        library = Path(reference_library_of(self.ref))
        if library != Path(self.ref):
            yield "ref", library
        if self.genome_library is None and not settings.get("genome_library"):
            yield "ref", library.parent / "genomes"


@dataclass(frozen=True, kw_only=True)
class RefGeneMapParams(_InLibrary, Params):
    """Download the hg19 and hg38 gene maps into the library, or delete them."""

    COMMAND = "ref gene-map"

    delete: bool = param(
        False,
        help="Delete gene maps instead of downloading",
        action="store_true",
    )
    ref: Path | None = ref_param()


@dataclass(frozen=True, kw_only=True)
class _Resource(_InLibrary, Params):
    """Annotation data downloaded into the library's ``ref`` folder."""

    ref: Path | None = ref_param()


@dataclass(frozen=True, kw_only=True)
class RefClinvarParams(_Resource):
    """Download the official ClinVar VCF for hg19 and hg38."""

    COMMAND = "ref clinvar"


@dataclass(frozen=True, kw_only=True)
class RefRevelParams(_Resource):
    """Download REVEL pathogenicity scores for hg19 and hg38."""

    COMMAND = "ref revel"


@dataclass(frozen=True, kw_only=True)
class RefPhylopParams(_Resource):
    """Download UCSC's PhyloP 100-way conservation scores (~10 GB bigWig)."""

    COMMAND = "ref phylop"

    build: str = _build_param("Genome build to download (default: hg38).")


@dataclass(frozen=True, kw_only=True)
class RefGnomadParams(_Resource):
    """Download gnomAD allele frequencies: the 2.1.1 exome sites VCFs, or
    build the 4.1 frequency table."""

    COMMAND = "ref gnomad"

    release: str = param(
        "2.1.1",
        help=(
            "2.1.1: exome sites VCFs for hg19 and hg38 (hg38 lifted over). "
            "4.1: native hg38 exomes+genomes, streamed (~900 GB, hours) into an "
            "allele-frequency-only table of ~5 GB; --threads sets parallel streams."
        ),
        choices=GNOMAD_RELEASES,
    )
    threads: int | None = threads_param()


@dataclass(frozen=True, kw_only=True)
class RefSpliceaiParams(_Resource):
    """Download SpliceAI scores for hg38 (Ensembl, MANE SNVs), or index a
    manual hg19 download."""

    COMMAND = "ref spliceai"


@dataclass(frozen=True, kw_only=True)
class RefAlphamissenseParams(_Resource):
    """Download AlphaMissense scores for hg19 and hg38."""

    COMMAND = "ref alphamissense"


@dataclass(frozen=True, kw_only=True)
class RefPharmgkbParams(_Resource):
    """Download PharmGKB annotations."""

    COMMAND = "ref pharmgkb"


@dataclass(frozen=True, kw_only=True)
class RefCaddParams(_Resource):
    """Download CADD v1.7 deleteriousness scores (~85 GB per build)."""

    COMMAND = "ref cadd"

    build: str = _build_param(
        "Genome build to download (default: hg38). Each is about 85 GB."
    )


@dataclass(frozen=True, kw_only=True)
class RefGerpParams(_Resource):
    """Download GERP++ conservation scores as a bigWig."""

    COMMAND = "ref gerp"

    build: str = _build_param("Genome build to download (default: hg38).")


@dataclass(frozen=True, kw_only=True)
class RefBootstrapParams(_InLibrary, Params):
    """Download the reference library bootstrap (VCFs, chains, N counts and
    other support files). Saves the library's path as config.toml's
    ``reference_library`` when that is unset."""

    COMMAND = "ref bootstrap"
    # It may save config.toml, which a server request must not change.
    LOCAL_ONLY = True

    ref: Path | None = ref_param()


def aadr(
    params: RefAadrParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Install the panel; output role ``reference_resource``."""
    from wgsextract_cli.commands.ref.library_commands import run_aadr

    return execute(params, run_aadr, progress=progress, cancel=cancel, check=check)


def ybrowse(
    params: RefYbrowseParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Install the snapshot; output role ``reference_resource``."""
    from wgsextract_cli.commands.ref.library_commands import run_ybrowse

    return execute(params, run_ybrowse, progress=progress, cancel=cancel, check=check)


def download(
    params: RefDownloadParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Download the file; output role ``download``. Not offered by the server."""
    from wgsextract_cli.commands.ref.core_commands import run_download

    return execute(params, run_download, progress=progress, cancel=cancel, check=check)


def index(
    params: RefIndexParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Index the FASTA; output role ``reference_index`` for each file written."""
    from wgsextract_cli.commands.ref.core_commands import run_index

    return execute(params, run_index, progress=progress, cancel=cancel, check=check)


def count_ns(
    params: RefCountNsParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Count the Ns; output roles ``n_counts`` and ``n_bins``."""
    from wgsextract_cli.commands.ref.core_commands import run_count_ns

    return execute(params, run_count_ns, progress=progress, cancel=cancel, check=check)


def verify(
    params: RefVerifyParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Verify the FASTA; a mismatch fails, a missing ``.dict`` is a warning."""
    from wgsextract_cli.commands.ref.core_commands import run_verify

    return execute(params, run_verify, progress=progress, cancel=cancel, check=check)


def library(
    params: RefLibraryParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Install ``params.install``; output roles ``reference`` (the FASTA) and
    ``reference_sidecar``. The CLI's listing and menu aren't offered: use
    ``status`` for what is installed."""
    from wgsextract_cli.commands.ref.library_commands import run_library

    return execute(params, run_library, progress=progress, cancel=cancel, check=check)


def status(
    params: RefStatusParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Report the library; the report is the result's ``details``."""
    from wgsextract_cli.commands.ref.status import run_status

    return execute(params, run_status, progress=progress, cancel=cancel, check=check)


def gene_map(
    params: RefGeneMapParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Download the gene maps (output role ``reference_resource``) or delete them."""
    from wgsextract_cli.commands.ref.library_commands import run_gene_map

    return execute(params, run_gene_map, progress=progress, cancel=cancel, check=check)


def clinvar(
    params: RefClinvarParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Download ClinVar; output role ``reference_resource`` for each file."""
    from wgsextract_cli.commands.ref.library_commands import run_clinvar

    return execute(params, run_clinvar, progress=progress, cancel=cancel, check=check)


def revel(
    params: RefRevelParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Download REVEL; output role ``reference_resource`` for each file."""
    from wgsextract_cli.commands.ref.library_commands import run_revel

    return execute(params, run_revel, progress=progress, cancel=cancel, check=check)


def phylop(
    params: RefPhylopParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Download PhyloP; output role ``reference_resource``."""
    from wgsextract_cli.commands.ref.library_commands import run_phylop

    return execute(params, run_phylop, progress=progress, cancel=cancel, check=check)


def gnomad(
    params: RefGnomadParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Download or build gnomAD; output role ``reference_resource`` for each file."""
    from wgsextract_cli.commands.ref.library_commands import run_gnomad

    return execute(params, run_gnomad, progress=progress, cancel=cancel, check=check)


def spliceai(
    params: RefSpliceaiParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Download SpliceAI; output role ``reference_resource`` for each file."""
    from wgsextract_cli.commands.ref.library_commands import run_spliceai

    return execute(params, run_spliceai, progress=progress, cancel=cancel, check=check)


def alphamissense(
    params: RefAlphamissenseParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Download AlphaMissense; output role ``reference_resource`` for each file."""
    from wgsextract_cli.commands.ref.library_commands import run_alphamissense

    return execute(
        params, run_alphamissense, progress=progress, cancel=cancel, check=check
    )


def pharmgkb(
    params: RefPharmgkbParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Download PharmGKB; output role ``reference_resource`` for each file."""
    from wgsextract_cli.commands.ref.library_commands import run_pharmgkb

    return execute(params, run_pharmgkb, progress=progress, cancel=cancel, check=check)


def cadd(
    params: RefCaddParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Download CADD; output role ``reference_resource`` for each file."""
    from wgsextract_cli.commands.ref.library_commands import run_cadd

    return execute(params, run_cadd, progress=progress, cancel=cancel, check=check)


def gerp(
    params: RefGerpParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Download GERP; output role ``reference_resource``."""
    from wgsextract_cli.commands.ref.library_commands import run_gerp

    return execute(params, run_gerp, progress=progress, cancel=cancel, check=check)


def bootstrap(
    params: RefBootstrapParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Install the bootstrap; output role ``reference_resource`` for each file.
    Not offered by the server."""
    from wgsextract_cli.commands.ref.library_commands import run_bootstrap

    return execute(params, run_bootstrap, progress=progress, cancel=cancel, check=check)
