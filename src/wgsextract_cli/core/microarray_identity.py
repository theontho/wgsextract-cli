"""Opt-in marker identity checks; probe-strand inference is deliberately excluded."""

import argparse
import functools
import gzip
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from wgsextract_cli.core.config import settings
from wgsextract_cli.core.utils import WGSExtractError

_RSID_MAP = (
    Path(__file__).resolve().parents[1] / "assets/microarray/rsid/dbsnp157.tsv.gz"
)


class RsidIdentity(NamedTuple):
    current: str | None
    status: str


@functools.cache
def load_rsid_map() -> dict[str, RsidIdentity]:
    records: dict[str, RsidIdentity] = {}
    try:
        with gzip.open(_RSID_MAP, "rt", encoding="utf-8") as source:
            for line in source:
                if line.startswith("#"):
                    continue
                rsid, current, status = line.rstrip("\n").split("\t")
                if (
                    not rsid.startswith("rs")
                    or not rsid[2:].isdigit()
                    or status not in {"merged", "withdrawn", "ambiguous", "unresolved"}
                    or (
                        status == "merged"
                        and (not current.startswith("rs") or not current[2:].isdigit())
                    )
                    or (status != "merged" and current != ".")
                    or rsid in records
                ):
                    raise ValueError("invalid or duplicate rsID mapping")
                records[rsid] = RsidIdentity(
                    current if status == "merged" else None, status
                )
        if not records:
            raise ValueError("empty rsID mapping")
        if any(value.current in records for value in records.values() if value.current):
            raise ValueError("rsID map must resolve to terminal identities")
    except (OSError, EOFError, ValueError, zlib.error) as error:
        raise WGSExtractError(
            f"Cannot read the packaged microarray rsID map: {error}"
        ) from error
    return records


def resolve_rsid(rsid: str) -> RsidIdentity:
    return load_rsid_map().get(rsid, RsidIdentity(rsid, "unlisted"))


def strand_ambiguous(ref: str, alt: str) -> bool:
    return len(ref) == len(alt) == 1 and ref.upper() + alt.upper() in {
        "AT",
        "TA",
        "CG",
        "GC",
    }


@dataclass(frozen=True)
class MicroarrayIdentity:
    strand_ambiguous: str = "keep"
    resolve_rsids: bool = False

    @property
    def needs_alleles(self) -> bool:
        return self.strand_ambiguous == "no-call"

    @property
    def enabled(self) -> bool:
        return self.needs_alleles or self.resolve_rsids

    def rejection(self, rsid: str, ref: str = ".", alt: str = ".") -> str | None:
        if self.resolve_rsids:
            reason = {
                "withdrawn": "retired_rsid",
                "ambiguous": "ambiguous_rsid",
                "unresolved": "unresolved_rsid",
            }.get(resolve_rsid(rsid).status)
            if reason:
                return reason
        if self.needs_alleles and strand_ambiguous(ref, alt):
            return "strand_ambiguous"
        return None


def identity_policy(args: argparse.Namespace) -> MicroarrayIdentity:
    strand = getattr(args, "strand_ambiguous", None)
    if strand is None:
        strand = settings.get("microarray_strand_ambiguous", "keep")
    resolve = getattr(args, "resolve_rsids", None)
    if resolve is None:
        resolve = settings.get("microarray_resolve_rsids", False)
    if not isinstance(strand, str) or strand not in {"keep", "no-call"}:
        raise WGSExtractError("microarray strand_ambiguous must be keep or no-call.")
    if not isinstance(resolve, bool):
        raise WGSExtractError("microarray resolve_rsids must be true or false.")
    return MicroarrayIdentity(strand, resolve)
