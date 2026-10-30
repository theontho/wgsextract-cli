"""Build a genome-wide microarray target from the chip templates.

A microarray kit is genotyped at its chip's positions. The templates the
reference library ships (``ref bootstrap``), plus the few the CLI ships, list
every chip's markers in GRCh37 (FTDNA's Affymetrix list is NCBI36, lifted with
UCSC's hg18ToHg19 chain), so their union is the genome-wide target; for GRCh38
its positions are lifted with UCSC's hg19ToHg38 chain, except chrM: GRCh37's MT
and hg38's chrM are both the rCRS. The result is saved in the library as
``microarray/All_SNPs_<build>_ref.tab.gz`` and rebuilt when it was built by an
older version of this builder.
"""

import contextlib
import gzip
import logging
import os
import subprocess
import tempfile
import time
from collections.abc import Iterator, Sequence
from pathlib import Path

from pyliftover import LiftOver

from wgsextract_cli.core.builds import canonical_build
from wgsextract_cli.core.microarray_utils import (
    _chrom_for_liftover,
    _resolve_templates_roots,
    _template_body_paths,
    chr_to_int,
    get_template_format,
    ncbi36_to_grch37,
    template_rows,
)
from wgsextract_cli.core.ref_library.downloads import download_file
from wgsextract_cli.core.utils import WGSExtractError, run_command

HG19_TO_HG38_CHAIN_URL = (
    "https://hgdownload.soe.ucsc.edu/goldenPath/hg19/liftOver/hg19ToHg38.over.chain.gz"
)
HG18_TO_HG19_CHAIN_URL = (
    "https://hgdownload.soe.ucsc.edu/goldenPath/hg18/liftOver/hg18ToHg19.over.chain.gz"
)
# Bump when the builder's output changes, so libraries rebuild their target.
TARGET_VERSION = 3
_VERSION_LINE = f"##wgsextract_microarray_target_version={TARGET_VERSION}\n"
# In order of precedence for a position's ID.
_FORMATS = [
    "23andMe_V2",
    "23andMe_V3",
    "23andMe_V35",
    "23andMe_V4",
    "23andMe_V5",
    "23andMe_V5_2025",
    "23andMe_SNPs_API",
    "Ancestry_V1",
    "Ancestry_V2",
    "Ancestry_V2_2024",
    "FTDNA_V2",
    "FTDNA_V3",
    "LDNA_V1",
    "LDNA_V2",
    "MyHeritage_V1",
    "MyHeritage_V2",
    "Nucleus_V1",
    "FTDNA_V1_Affy",
    "Ancestry_V2_2021",
    "MyHeritage_GSA_2025",
    "MyHeritage_GSA_2025_609424",
]


def genome_wide_target_path(reflib_dir: str, build: str) -> str:
    return str(Path(reflib_dir, "microarray", f"All_SNPs_{build}_ref.tab.gz"))


def is_genome_wide_target(path: str, reflib_dir: str, build: str) -> bool:
    """Whether path is the library's own target for build (which
    build_genome_wide_target keeps current), not one supplied separately."""
    target_build = canonical_build(build)
    if target_build not in ("hg19", "hg38"):
        return False
    own = genome_wide_target_path(reflib_dir, target_build)
    try:
        return Path(path).samefile(own)
    except OSError:
        return False


def _is_current(target: str) -> bool:
    if not (Path(target).is_file() and Path(target + ".tbi").is_file()):
        return False
    try:
        with gzip.open(target, "rt") as f:
            return f.readline() == _VERSION_LINE
    except (OSError, EOFError):
        return False


def _template_paths(roots: list[str]) -> Iterator[tuple[str, bool, list[str]]]:
    """(format, whether it's NCBI36, body paths) of each template found, from
    the first root holding all of its parts."""
    for name in _FORMATS:
        fmt = get_template_format(name)
        if not fmt:
            continue
        for root in roots:
            paths = _template_body_paths(root, name, fmt)
            if all(Path(p).exists() for p in paths):
                yield name, fmt.get("ncbi36", False), paths
                break


def collect_template_markers(
    template_roots: Sequence[str | None], ncbi36_chain: str | None = None
) -> dict[tuple[str, int], str]:
    """The union of the chip templates' markers on GRCh37: {(chrNN, pos): ID}.

    NCBI36 templates are included only when ncbi36_chain is given."""
    roots = _resolve_templates_roots(list(template_roots))
    markers: dict[tuple[str, int], str] = {}
    for name, ncbi36, paths in _template_paths(roots):
        if ncbi36 and not ncbi36_chain:
            continue
        lifter = LiftOver(ncbi36_chain) if ncbi36 and ncbi36_chain else None
        for path in paths:
            for rsid, chrom, pos in template_rows(name, path):
                if lifter:
                    lifted = ncbi36_to_grch37(lifter, chrom, pos)
                    if not lifted:
                        continue
                    chrom, pos, _inverted = lifted
                if pos.isdigit() and chrom:
                    position = int(pos)
                    if position > 0:
                        markers.setdefault((_chrom_for_liftover(chrom), position), rsid)
    return markers


def needs_ncbi36_chain(template_roots: Sequence[str | None]) -> bool:
    roots = _resolve_templates_roots(list(template_roots))
    return any(ncbi36 for _name, ncbi36, _paths in _template_paths(roots))


def _lift_to_hg38(
    markers: dict[tuple[str, int], str], chain: str
) -> dict[tuple[str, int], str]:
    lifter = LiftOver(chain)
    lifted: dict[tuple[str, int], str] = {}
    lost = 0
    for (chrom, pos), rsid in markers.items():
        if chrom == "chrM":
            # UCSC's hg19 chrM is the Yoruba sequence; the templates' MT, like
            # hg38's chrM, is the rCRS.
            lifted.setdefault((chrom, pos), rsid)
            continue
        hits = lifter.convert_coordinate(chrom, pos - 1)
        # One-to-one onto a primary chromosome only.
        if not hits or "_" in hits[0][0]:
            lost += 1
            continue
        lifted.setdefault((hits[0][0], hits[0][1] + 1), rsid)
    logging.info(f"Lifted {len(lifted):,} chip markers to hg38 ({lost:,} didn't lift).")
    return lifted


def build_genome_wide_target(
    reflib_dir: str, build: str, template_roots: Sequence[str | None]
) -> str:
    """Write and index microarray/All_SNPs_<build>_ref.tab.gz unless it's
    current; return its path.

    Runs that start together on one library take turns (a lock file), and a run
    that finds the target already built by another uses it."""
    target_build = canonical_build(build)
    if target_build not in ("hg19", "hg38"):
        raise WGSExtractError(
            f"Microarray targets can only be built for hg19 or hg38, not {build}."
        )
    out = genome_wide_target_path(reflib_dir, target_build)
    # Before taking the lock, so a current target in a read-only library is used.
    if _is_current(out):
        return out
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    with _build_lock(out + ".lock"):
        if _is_current(out):
            return out
        chain36 = (
            ncbi36_chain(reflib_dir) if needs_ncbi36_chain(template_roots) else None
        )
        markers = collect_template_markers(template_roots, chain36)
        if not markers:
            if Path(out).is_file() and Path(out + ".tbi").is_file():
                logging.warning(
                    f"No microarray templates found to rebuild {out} from; using it "
                    "as it is."
                )
                return out
            raise WGSExtractError(
                "No microarray templates found to build the genome-wide target from; "
                "run 'wgsextract ref bootstrap' to install them."
            )
        logging.info(
            f"Building the {target_build} microarray target from {len(markers):,} "
            "chip template markers (once; it's saved in the reference library)."
        )
        if target_build == "hg38":
            markers = _lift_to_hg38(
                markers,
                _chain(reflib_dir, "hg19ToHg38.over.chain.gz", HG19_TO_HG38_CHAIN_URL),
            )
        _write_target(markers, out)
    logging.info(f"Saved the {target_build} microarray target: {out}")
    return out


def ncbi36_chain(reflib_dir: str) -> str:
    """The library's hg18ToHg19 chain, downloaded when it's missing."""
    return _chain(reflib_dir, "hg18ToHg19.over.chain.gz", HG18_TO_HG19_CHAIN_URL)


def _chain(reflib_dir: str, name: str, url: str) -> str:
    chain = str(Path(reflib_dir, name))
    if not Path(chain).is_file() and not download_file(url, chain):
        raise WGSExtractError(
            f"Could not download {url}, needed to place the chip markers on "
            "another genome build."
        )
    return chain


def _write_target(markers: dict[tuple[str, int], str], out: str) -> None:
    """Write and index out through private temporary files, then rename both into
    place; only this run's temporary files are ever removed."""
    fd, plain = tempfile.mkstemp(
        prefix=".targets_", suffix=".tab", dir=Path(out).parent
    )
    staged = plain + ".gz"
    try:
        with os.fdopen(fd, "w", newline="\n") as f:
            f.write(_VERSION_LINE + "#CHROM\tPOS\tID\n")
            for (chrom, pos), rsid in sorted(
                markers.items(), key=lambda m: (chr_to_int(m[0][0]), m[0][1])
            ):
                f.write(f"{chrom}\t{pos}\t{rsid}\n")
        with open(staged, "wb") as gz:
            run_command(["bgzip", "-c", plain], stdout=gz)
        run_command(["tabix", "-f", "-s", "1", "-b", "2", "-e", "2", staged])
        Path(staged).replace(out)
        Path(staged + ".tbi").replace(out + ".tbi")
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(
            f"Could not write the microarray target {out}: {e}"
        ) from e
    finally:
        for path in (plain, staged, staged + ".tbi"):
            Path(path).unlink(missing_ok=True)


@contextlib.contextmanager
def _build_lock(lock: str, timeout: float = 1800) -> Iterator[None]:
    """An exclusive lock file; a lock older than timeout is taken as abandoned."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                stale = time.time() - Path(lock).stat().st_mtime > timeout
            except FileNotFoundError:
                continue
            if stale or time.monotonic() > deadline:
                Path(lock).unlink(missing_ok=True)
                continue
            logging.info(f"Waiting for another run to finish building {lock[:-5]}...")
            time.sleep(5)
    try:
        os.close(fd)
        yield
    finally:
        Path(lock).unlink(missing_ok=True)
