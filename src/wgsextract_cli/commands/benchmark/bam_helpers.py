"""BAM file + index helpers used by the benchmark execution layer."""

from __future__ import annotations

import shutil
from pathlib import Path

from wgsextract_cli.core.utils import WGSExtractError, run_command


def _bam_index_candidates(bam_path: Path) -> list[Path]:
    candidates = [Path(str(bam_path) + ".bai"), Path(str(bam_path) + ".csi")]
    if bam_path.suffix.lower() == ".bam":
        candidates.append(bam_path.with_suffix(".bai"))
    return candidates


def _bam_index_path(bam_path: Path) -> Path | None:
    for candidate in _bam_index_candidates(bam_path):
        if candidate.exists():
            return candidate
    return None


def _copy_bam_with_index(source_bam: Path, dest_bam: Path) -> None:
    dest_bam.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_bam, dest_bam)

    source_index = _bam_index_path(source_bam)
    if source_index and source_index.exists():
        dest_index = _copied_bam_index_path(source_bam, dest_bam)
        shutil.copy2(source_index, dest_index)
    else:
        run_command(["samtools", "index", str(dest_bam)])


def _copied_bam_index_path(source_bam: Path, dest_bam: Path) -> Path:
    source_index = _bam_index_path(source_bam)
    if source_index and source_index.name == source_bam.name + source_index.suffix:
        return Path(str(dest_bam) + source_index.suffix)
    if source_index:
        return dest_bam.with_suffix(source_index.suffix)
    return Path(str(dest_bam) + ".bai")


def _assert_bam_unindexed(bam_path: Path) -> None:
    existing = [path for path in _bam_index_candidates(bam_path) if path.exists()]
    if existing:
        paths = ", ".join(str(path) for path in existing)
        raise WGSExtractError(f"BAM index still exists after unindex: {paths}")
