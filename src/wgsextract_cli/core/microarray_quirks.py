"""Known vendor strand quirks: write a chip revision's markers as the vendor did.

Microarray outputs are written on the GRCh37 forward strand ("corrected", the
default). Some public vendor exports reported certain markers on the reverse
strand instead, and some later revisions fixed them. ``vendor`` mode reproduces
those reports for the templates they apply to. The packaged table and its
sources are described in ``assets/microarray/revisions/README.md``.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from wgsextract_cli.core.config import settings
from wgsextract_cli.core.utils import WGSExtractError

if TYPE_CHECKING:
    from wgsextract_cli.api.microarray import MicroarrayParams

STRAND_TABLE = (
    Path(__file__).resolve().parents[1] / "assets/microarray/revisions/strand.tsv"
)
MODES = ("corrected", "vendor")
_COLUMNS = (
    "revision",
    "id",
    "chrom",
    "position",
    "reported",
    "forward",
    "fixed_in",
    "templates",
    "exports",
    "confirmed_by",
)
_COMPLEMENT = str.maketrans("ACGT", "TGCA")


@dataclass(frozen=True)
class StrandQuirk:
    """A marker a revision reported on the reverse strand."""

    revision: str
    marker: str
    chrom: str
    position: str
    reported: str
    forward: str
    fixed_in: str | None
    templates: tuple[str, ...]
    exports: int
    confirmed_by: tuple[str, ...]


def _optional(value: str) -> str | None:
    return None if value == "." else value


def _names(value: str) -> tuple[str, ...]:
    return () if value == "." else tuple(value.split(","))


def _valid_alleles(reported: str, forward: str) -> bool:
    return (
        len(reported) == 2
        and reported[0] != reported[1]
        and set(reported) <= set("ACGT")
        # A/T and C/G pairs read the same on both strands.
        and reported.translate(_COMPLEMENT) not in {reported, reported[::-1]}
        and "".join(sorted(reported.translate(_COMPLEMENT))) == forward
        and "".join(sorted(reported)) == reported
    )


@functools.cache
def load_strand_quirks(path: Path | None = None) -> tuple[StrandQuirk, ...]:
    path = path or STRAND_TABLE
    from wgsextract_cli.core.microarray_revisions import load_catalogue
    from wgsextract_cli.core.microarray_utils import (
        _template_corrections,
        get_template_format,
    )

    revisions = {r.id: r for r in load_catalogue()}
    corrected = {(template, marker) for template, marker, _ in _template_corrections()}
    quirks: list[StrandQuirk] = []
    seen: set[tuple[str, str, str, str]] = set()
    try:
        with path.open(encoding="utf-8") as source:
            for number, line in enumerate(source, 1):
                if line.startswith("#") or not line.strip():
                    continue
                fields = line.rstrip("\n").split("\t")
                if len(fields) != len(_COLUMNS):
                    raise ValueError(f"line {number} needs {len(_COLUMNS)} columns")
                quirk = StrandQuirk(
                    fields[0],
                    fields[1],
                    fields[2],
                    fields[3],
                    fields[4],
                    fields[5],
                    _optional(fields[6]),
                    _names(fields[7]),
                    int(fields[8]),
                    _names(fields[9]),
                )
                key = (quirk.revision, quirk.marker, quirk.chrom, quirk.position)
                revision = revisions.get(quirk.revision)
                if (
                    revision is None
                    or revision.build != "GRCh37"
                    or key in seen
                    or not quirk.position.isdigit()
                    or not _valid_alleles(quirk.reported, quirk.forward)
                    or (quirk.fixed_in is not None and quirk.fixed_in not in revisions)
                    or quirk.exports <= 0
                    or not quirk.confirmed_by
                    or quirk.revision in quirk.confirmed_by
                    or any(name not in revisions for name in quirk.confirmed_by)
                ):
                    raise ValueError(f"line {number} is not a valid strand quirk")
                for template in quirk.templates:
                    info = get_template_format(template)
                    # NCBI36 templates are already written in their own build's
                    # orientation; corrections.tsv markers are relocated, so a
                    # strand entry for them could apply on top of a position fix.
                    if info is None or info.get("ncbi36"):
                        raise ValueError(f"line {number}: unusable template {template}")
                    if (template, quirk.marker) in corrected:
                        raise ValueError(
                            f"line {number}: {template} {quirk.marker} is already "
                            "relocated by corrections.tsv"
                        )
                seen.add(key)
                quirks.append(quirk)
    except (OSError, ValueError) as error:
        raise WGSExtractError(
            f"Cannot read the packaged microarray strand-quirk table: {error}"
        ) from error
    return tuple(quirks)


@functools.cache
def vendor_strand_flips(format_name: str) -> frozenset[tuple[str, str, str]]:
    """(ID, chromosome, position) of the template's markers its vendor reported
    on the reverse strand."""
    return frozenset(
        (quirk.marker, quirk.chrom, quirk.position)
        for quirk in load_strand_quirks()
        if format_name in quirk.templates
    )


def quirks_mode(params: MicroarrayParams) -> str:
    mode = params.quirks
    if mode is None:
        mode = settings.get("microarray_quirks", "corrected")
    if not isinstance(mode, str) or mode not in MODES:
        raise WGSExtractError("microarray quirks must be corrected or vendor.")
    return mode
