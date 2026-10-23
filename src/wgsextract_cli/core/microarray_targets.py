"""Build a genome-wide microarray target from the chip templates.

A microarray kit is genotyped at its chip's positions. The templates the
reference library ships (``ref bootstrap``) list every chip's markers in
GRCh37, so their union is the genome-wide target; for GRCh38 its positions are
lifted with UCSC's hg19ToHg38 chain. The result is saved in the library as
``microarray/All_SNPs_<build>_ref.tab.gz`` and reused.
"""

import logging
import subprocess
from collections.abc import Iterator, Sequence
from pathlib import Path

from wgsextract_cli.core.builds import canonical_build
from wgsextract_cli.core.microarray_utils import (
    _chrom_for_liftover,
    _open_template,
    _resolve_templates_roots,
    _template_body_paths,
    chr_to_int,
    get_template_format,
)
from wgsextract_cli.core.ref_library.downloads import download_file
from wgsextract_cli.core.utils import WGSExtractError, run_command

HG19_TO_HG38_CHAIN_URL = (
    "https://hgdownload.soe.ucsc.edu/goldenPath/hg19/liftOver/hg19ToHg38.over.chain.gz"
)
# Chips whose templates aren't GRCh37 positions (FTDNA's Affymetrix export is
# NCBI36, matched by rsID instead).
_NOT_GRCH37 = {"FTDNA_V1_Affy"}
_FORMATS = [
    "23andMe_V2",
    "23andMe_V3",
    "23andMe_V35",
    "23andMe_V4",
    "23andMe_V5",
    "23andMe_SNPs_API",
    "Ancestry_V1",
    "Ancestry_V2",
    "FTDNA_V2",
    "FTDNA_V3",
    "LDNA_V1",
    "LDNA_V2",
    "MyHeritage_V1",
    "MyHeritage_V2",
]


def genome_wide_target_path(reflib_dir: str, build: str) -> str:
    return str(Path(reflib_dir, "microarray", f"All_SNPs_{build}_ref.tab.gz"))


def _template_markers(path: str) -> Iterator[tuple[str, int, str]]:
    """(chrom, GRCh37 position, rsID) of each marker in a template body."""
    with _open_template(path) as f:
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            sep = "," if line.count(",") > line.count("\t") else "\t"
            parts = [p.strip().strip('"') for p in line.split(sep)]
            if len(parts) < 3:
                continue
            # rsID first, except 23andMe's API list (chrom, pos, rsID).
            rsid, chrom, pos = (
                (parts[2], parts[0], parts[1])
                if parts[1].isdigit() and not parts[2].isdigit()
                else (parts[0], parts[1], parts[2])
            )
            if pos.isdigit() and chrom:
                yield chrom, int(pos), rsid


def collect_template_markers(
    template_roots: Sequence[str | None],
) -> dict[tuple[str, int], str]:
    """The union of the GRCh37 chip templates' markers: {(chrNN, pos): rsID}."""
    roots = _resolve_templates_roots(list(template_roots))
    markers: dict[tuple[str, int], str] = {}
    for name in _FORMATS:
        fmt = get_template_format(name)
        if not fmt or name in _NOT_GRCH37:
            continue
        for root in roots:
            paths = _template_body_paths(root, name, fmt)
            if all(Path(p).exists() for p in paths):
                for path in paths:
                    for chrom, pos, rsid in _template_markers(path):
                        markers.setdefault((_chrom_for_liftover(chrom), pos), rsid)
                break
    return markers


def _lift_to_hg38(
    markers: dict[tuple[str, int], str], chain: str
) -> dict[tuple[str, int], str]:
    from pyliftover import LiftOver

    lifter = LiftOver(chain)
    lifted: dict[tuple[str, int], str] = {}
    lost = 0
    for (chrom, pos), rsid in markers.items():
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
    """Write and index microarray/All_SNPs_<build>_ref.tab.gz; return its path."""
    target_build = canonical_build(build)
    if target_build not in ("hg19", "hg38"):
        raise WGSExtractError(
            f"Microarray targets can only be built for hg19 or hg38, not {build}."
        )
    markers = collect_template_markers(template_roots)
    if not markers:
        raise WGSExtractError(
            "No microarray templates found to build the genome-wide target from; "
            "run 'wgsextract ref bootstrap' to install them."
        )
    logging.info(
        f"Building the {target_build} microarray target from {len(markers):,} chip "
        "template markers (once; it's saved in the reference library)."
    )
    if target_build == "hg38":
        chain = str(Path(reflib_dir, "hg19ToHg38.over.chain.gz"))
        if not Path(chain).is_file() and not download_file(
            HG19_TO_HG38_CHAIN_URL, chain
        ):
            raise WGSExtractError(
                f"Could not download {HG19_TO_HG38_CHAIN_URL}, needed to place the "
                "chip markers on hg38."
            )
        markers = _lift_to_hg38(markers, chain)

    out = genome_wide_target_path(reflib_dir, target_build)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    plain = out.removesuffix(".gz") + ".partial"
    with open(plain, "w") as f:
        f.write("#CHROM\tPOS\tID\n")
        for (chrom, pos), rsid in sorted(
            markers.items(), key=lambda m: (chr_to_int(m[0][0]), m[0][1])
        ):
            f.write(f"{chrom}\t{pos}\t{rsid}\n")
    try:
        with open(out + ".partial", "wb") as gz:
            run_command(["bgzip", "-c", plain], stdout=gz)
        Path(out + ".partial").replace(out)
        run_command(["tabix", "-f", "-s", "1", "-b", "2", "-e", "2", out])
    except (OSError, subprocess.SubprocessError) as e:
        Path(out).unlink(missing_ok=True)
        raise WGSExtractError(
            f"Could not write the microarray target {out}: {e}"
        ) from e
    finally:
        Path(plain).unlink(missing_ok=True)
        Path(out + ".partial").unlink(missing_ok=True)
    logging.info(f"Saved the {target_build} microarray target: {out}")
    return out
