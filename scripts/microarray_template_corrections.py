"""Write corrections.tsv: MT position fixes for the reference library's 23andMe
templates.

A few of their MT markers are at Yoruba (UCSC hg19 chrM) positions in an
otherwise rCRS list, so they miss the kit's calls. A marker is corrected only
when a 23andMe raw data export gives it another MT position and the template's
position, lifted with hg19ToHg38's chrM chain (Yoruba -> rCRS), is exactly that.

Usage: python scripts/microarray_template_corrections.py REFLIB RAW_23ANDME_FILE
Needs REFLIB/hg19ToHg38.over.chain.gz and REFLIB/microarray/raw_file_templates.
"""

import gzip
import sys
from pathlib import Path

from pyliftover import LiftOver

OUT = (
    Path(__file__).resolve().parents[1]
    / "src/wgsextract_cli/assets/microarray/raw_file_templates/corrections.tsv"
)
TEMPLATES = ["23andMe_V4", "23andMe_V5", "23andMe_V35"]
HEADER = """\
# Corrections to the reference library's microarray templates (ref bootstrap).
# These MT markers are at Yoruba (UCSC hg19 chrM) positions in an otherwise rCRS list;
# the corrected position is the rCRS one 23andMe's 2025 exports give them.
# Rebuild: python scripts/microarray_template_corrections.py REFLIB RAW_23ANDME_FILE
#template\tid\tchrom\tposition\tcorrected_position
"""


def _rows(path: Path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt") as f:
        for line in f:
            if not line.startswith("#"):
                parts = line.split()
                if len(parts) >= 3:
                    yield parts[0], parts[1], parts[2]


def main() -> None:
    reflib, raw = Path(sys.argv[1]), Path(sys.argv[2])
    lifter = LiftOver(str(reflib / "hg19ToHg38.over.chain.gz"))
    exported = {i: int(pos) for i, chrom, pos in _rows(raw) if chrom == "MT"}
    lines = []
    for name in TEMPLATES:
        body = reflib / "microarray" / "raw_file_templates" / "body"
        parts = sorted(body.glob(f"{name}.txt")) or sorted(body.glob(f"{name}_*.txt"))
        for path in parts:
            for snp_id, chrom, pos in _rows(path):
                if chrom != "MT" or exported.get(snp_id, int(pos)) == int(pos):
                    continue
                hits = lifter.convert_coordinate("chrM", int(pos) - 1)
                if hits and hits[0][1] + 1 == exported[snp_id]:
                    lines.append(f"{name}\t{snp_id}\tMT\t{pos}\t{exported[snp_id]}\n")
    OUT.write_text(HEADER + "".join(lines))
    print(f"Wrote {len(lines)} corrections to {OUT}")


if __name__ == "__main__":
    main()
