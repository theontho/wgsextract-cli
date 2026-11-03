"""Repeat-length measurement from reads that span a Y-STR tract.

Each tract (the repeat plus any invariant sequence counted with it) is bounded
by two short reference flanks. A read spans the tract when both flanks are
found in its sequence, left before right; the bases between them are the
read's tract allele. This is the flank-anchored approach of lobSTR and of
forensic sequence-based STR typing (e.g. STRait Razor): it reads the length
straight from the sequence, so it doesn't depend on how the aligner placed an
indel or soft-clipped a read that ends in the repeat.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field


def _find_all(seq: str, pattern: str) -> list[int]:
    hits = []
    start = seq.find(pattern)
    while start != -1:
        hits.append(start)
        start = seq.find(pattern, start + 1)
    return hits


def _mismatches(seq: str, pattern: str, pos: int, limit: int) -> int:
    count = 0
    for a, b in zip(seq[pos : pos + len(pattern)], pattern, strict=True):
        if a != b:
            count += 1
            if count > limit:
                break
    return count


def find_flank(seq: str, flank: str) -> int | None:
    """The one position of ``flank`` in ``seq``, allowing one mismatch.

    An exact match wins; failing that, a single one-mismatch match (a
    sequencing error in the flank). None when absent or ambiguous.
    """
    exact = _find_all(seq, flank)
    if exact:
        return exact[0] if len(exact) == 1 else None
    # A one-mismatch match has an exact half (pigeonhole), so only positions
    # where either half matches need comparing.
    half = len(flank) // 2
    candidates = set(_find_all(seq, flank[:half]))
    candidates |= {pos - half for pos in _find_all(seq, flank[half:])}
    hits = [
        pos
        for pos in sorted(candidates)
        if 0 <= pos <= len(seq) - len(flank) and _mismatches(seq, flank, pos, 1) <= 1
    ]
    return hits[0] if len(hits) == 1 else None


def _near_match(seq: str, flank: str, limit: int = 2) -> bool:
    """Whether ``flank`` occurs in ``seq`` with at most ``limit`` mismatches."""
    # With at most `limit` mismatches one of `limit + 1` pieces matches exactly.
    size = len(flank) // (limit + 1)
    for i in range(limit + 1):
        offset = i * size
        piece = flank[offset : offset + size]
        for pos in _find_all(seq, piece):
            start = pos - offset
            if (
                0 <= start <= len(seq) - len(flank)
                and _mismatches(seq, flank, start, limit) <= limit
            ):
                return True
    return False


def tract_length(seq: str, left: str, right: str) -> int | None:
    """Bases between ``left`` and ``right`` in a read, or None if not spanned."""
    measured, _bound = measure(seq, left, right)
    return measured


def measure(seq: str, left: str, right: str) -> tuple[int | None, int | None]:
    """A read's tract length, or failing that a lower bound on it.

    A read with only one flank shows at least the bases between that flank
    and the read's far end, less a flank (it may end inside the other one).
    No bound is given when the other flank is there but unusable (repeated or
    with sequencing errors).
    """
    left_pos = find_flank(seq, left)
    if left_pos is not None:
        tract_start = left_pos + len(left)
        rest = seq[tract_start:]
        right_pos = find_flank(rest, right)
        if right_pos is not None:
            return right_pos, None
        if _near_match(rest, right):
            return None, None
        return None, len(rest) - len(right) + 1
    right_pos = find_flank(seq, right)
    if right_pos is not None and not _near_match(seq[:right_pos], left):
        return None, right_pos - len(left) + 1
    return None, None


@dataclass
class TractReads:
    """Tract allele lengths observed in the reads of one tract."""

    lengths: Counter[int] = field(default_factory=Counter)
    # Lower bounds from reads with one flank (see measure()).
    bounds: Counter[int] = field(default_factory=Counter)
    overlapping: int = 0
    longest_read: int = 0

    def add(self, seq: str, left: str, right: str, *, aligned: bool = True) -> None:
        """Measure one read; ``aligned`` when its alignment overlaps the tract.

        Bounds are kept only from aligned reads: a read placed elsewhere can
        hold a copy of one flank (as DYS389's two blocks share sequence).
        """
        if aligned:
            self.overlapping += 1
            self.longest_read = max(self.longest_read, len(seq))
        length, bound = measure(seq, left, right)
        if length is not None:
            self.lengths[length] += 1
        elif bound is not None and aligned:
            self.bounds[bound] += 1

    def longer_than(self, length: int) -> int:
        """Reads with one flank that show a tract longer than ``length``."""
        return sum(n for bound, n in self.bounds.items() if bound > length)

    @property
    def spanning(self) -> int:
        return sum(self.lengths.values())

    def mode(self) -> tuple[int, int] | None:
        """The most-supported length and its read count; None without reads or
        when two lengths tie."""
        if not self.lengths:
            return None
        ranked = self.lengths.most_common(2)
        if len(ranked) == 2 and ranked[0][1] == ranked[1][1]:
            return None
        return ranked[0]


def collect(
    reads: Iterable[tuple[str, int, str]],
    tracts: dict[str, tuple[str, int, int, str, str]],
) -> dict[str, TractReads]:
    """Measure every tract in reads given as (contig, aligned start, sequence).

    ``tracts`` maps a tract key to (contig, start, end, left flank, right flank),
    1-based inclusive. A read is examined for each tract near its alignment:
    soft-clipping or a long allele can shift a spanning read's start by up to
    a read length.
    """
    by_contig: dict[str, list[tuple[int, int, str]]] = {}
    for key, (contig, start, end, _left, _right) in tracts.items():
        by_contig.setdefault(contig, []).append((start, end, key))
    result = {key: TractReads() for key in tracts}
    for contig, pos, seq in reads:
        size = len(seq)
        for t_start, t_end, key in by_contig.get(contig, ()):
            if pos - size <= t_end and pos + 2 * size >= t_start:
                _c, _s, _e, left, right = tracts[key]
                aligned = pos <= t_end and pos + size > t_start
                result[key].add(seq, left, right, aligned=aligned)
    return result
