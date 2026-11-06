"""``typing``: genes and repeats a VCF can't capture, typed from the reads of
a GRCh37/hg19 or GRCh38 BAM or CRAM (indexed; a CRAM needs its reference).

Each writes ``<input stem>.*`` files in ``outdir``, else beside the input:

* ``cyp2d6``: CYP2D6 star alleles, copy number and hybrids with Cyrius
  (``.cyrius.tsv``/``.json``), from the optional ``cyrius`` Pixi environment.
  Cyrius's PolyForm Strict license allows noncommercial use only.
* ``hla``: HLA class I and II alleles with T1K (``.t1k_genotype.tsv``,
  ``.t1k_allele.tsv``/``.vcf``), from the optional ``t1k`` environment.
* ``repeats`` (``typing str``): disease repeat expansions with ExpansionHunter
  (``.str.tsv``/``.vcf``/``.json``), from the optional ``expansionhunter``
  environment; needs ``ref``.

``cyp2d6`` and ``hla`` also write a PharmCAT outside-call file
(``.cyp2d6.pharmcat.tsv``, ``.hla.pharmcat.tsv``) for ``vcf pharmcat
--outside-call`` when they make a confident call. The genotypes are in the
result's metrics."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from wgsextract_cli.core.results import CommandResult

from ._params import Params, param
from ._runtime import CancelToken, ProgressCallback, execute
from ._shared import input_param, outdir_param, ref_param, threads_param

SEXES = ("male", "female")


@dataclass(frozen=True, kw_only=True)
class _TypingParams(Params):
    input: Path = input_param(
        "Indexed BAM or CRAM aligned to GRCh37/hg19 or GRCh38.",
        "--input (a BAM or CRAM file) is required.",
    )
    ref: Path | None = ref_param()
    outdir: Path | None = outdir_param()
    threads: int | None = threads_param()


@dataclass(frozen=True, kw_only=True)
class TypingCyp2d6Params(_TypingParams):
    """Call CYP2D6 star alleles, including copy-number and CYP2D6/CYP2D7
    hybrid alleles, with Cyrius; it needs about 30x whole-genome data. On a
    GRCh38 reference with chr22 ALT contigs, the locus's reads are first
    realigned to primary chr22 (needs ``ref`` and minimap2). Metrics:
    ``cyp2d6_genotype``, ``cyp2d6_filter``, ``cyp2d6_copy_number`` and
    ``median_depth``."""

    COMMAND = "typing cyp2d6"


@dataclass(frozen=True, kw_only=True)
class TypingHlaParams(_TypingParams):
    """Type the HLA class I and II genes with T1K from the reads of the MHC,
    the HLA decoys and chromosome 6 ALT contigs. Metrics: the diplotype of
    HLA-A, -B, -C, -DRB1, -DQA1, -DQB1, -DPA1 and -DPB1 (None without a
    confident call) and the IPD-IMGT/HLA release of the index
    (``ipd_imgt_hla_release``). The outside-call file holds HLA-A and HLA-B."""

    COMMAND = "typing hla"

    hla_index: Path | None = param(
        None,
        help="T1K allele index (hla_dna_seq.fa). Default: <reference library>/"
        "ref/t1k_hla, built from IPD-IMGT/HLA on first use.",
        doc="T1K allele index (hla_dna_seq.fa); None uses the reference "
        "library's ref/t1k_hla one, built from IPD-IMGT/HLA on first use.",
        metavar="FASTA",
        path="file",
    )

    def _builds_index(self) -> bool:
        return self.hla_index is None

    def implied_paths(self) -> Iterator[tuple[str, Path]]:
        # Without an index, it is built in the library ``ref`` names.
        if self._builds_index() and self.ref is not None:
            from wgsextract_cli.core.reference_assets import reference_library_of

            yield "ref", Path(reference_library_of(self.ref))

    def write_folders(self) -> Iterator[Path]:
        if self._builds_index():
            from wgsextract_cli.commands.genotyping.hla import index_folder

            folder = index_folder(self.ref)
            if folder:
                yield Path(folder)


@dataclass(frozen=True, kw_only=True)
class TypingStrParams(_TypingParams):
    """Size the disease short tandem repeats (HTT, FMR1, C9ORF72, ...) with
    ExpansionHunter and flag those outside the normal range. Short-read
    sizing is approximate and isn't a diagnosis. Metrics: ``sex``,
    ``disease_repeats`` and ``flagged_repeats``; each flagged repeat is also a
    ``repeat_outside_normal_range`` warning."""

    COMMAND = "typing str"

    catalog: Path | None = param(
        None,
        help="ExpansionHunter variant catalog. Default: the one it ships for the "
        "input's build.",
        metavar="JSON",
        path="file",
    )
    sex: str | None = param(
        None,
        help="Sample sex (the number of chrX alleles). Default: inferred from "
        "chrX/chrY coverage.",
        choices=SEXES,
    )


def cyp2d6(
    params: TypingCyp2d6Params,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Call CYP2D6 star alleles with Cyrius (see ``TypingCyp2d6Params``)."""
    from wgsextract_cli.commands.genotyping.cyp2d6 import run_cyp2d6

    return execute(params, run_cyp2d6, progress=progress, cancel=cancel, check=check)


def hla(
    params: TypingHlaParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Type HLA genes with T1K (see ``TypingHlaParams``)."""
    from wgsextract_cli.commands.genotyping.hla import run_hla

    return execute(params, run_hla, progress=progress, cancel=cancel, check=check)


def repeats(
    params: TypingStrParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Size disease repeats with ExpansionHunter (``typing str``; see
    ``TypingStrParams``)."""
    from wgsextract_cli.commands.genotyping.repeats import run_str

    return execute(params, run_str, progress=progress, cancel=cancel, check=check)


__all__ = [
    "TypingCyp2d6Params",
    "TypingHlaParams",
    "TypingStrParams",
    "cyp2d6",
    "hla",
    "repeats",
]
