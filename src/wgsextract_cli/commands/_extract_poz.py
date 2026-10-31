import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from wgsextract_cli.core.builds import build_from_chr1_length
from wgsextract_cli.core.utils import WGSExtractError

_Y_BUILDS = {59_373_566: "hg19", 57_227_415: "hg38"}
_BEDS = Path(__file__).resolve().parents[1] / "assets" / "reference" / "poz"


@contextmanager
def poz_bed(contigs: dict[str, int], chromosome: str, outdir: str) -> Iterator[str]:
    """Select only exact supported Y assemblies, then adapt the contig name."""
    build = _Y_BUILDS.get(contigs.get(chromosome, 0))
    chr1_build = build_from_chr1_length(contigs)
    if not build or (chr1_build is not None and chr1_build != build):
        raise WGSExtractError(
            "--poz supports only GRCh37/hg19 and GRCh38 Y coordinates. "
            "Unknown, inconsistent or T2T Y assemblies need an independently "
            "validated BED; use extract custom --region <bed>."
        )
    with tempfile.TemporaryDirectory(dir=outdir, prefix=".poz_") as directory:
        path = Path(directory, "targets.bed")
        rows = [
            line.split("\t")
            for line in (_BEDS / f"{build}.bed").read_text().splitlines()
        ]
        path.write_text(
            "".join(f"{chromosome}\t{start}\t{end}\n" for _, start, end in rows),
            encoding="utf-8",
            newline="\n",
        )
        yield str(path)
