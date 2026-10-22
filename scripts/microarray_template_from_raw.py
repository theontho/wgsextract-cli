"""Make a microarray template (head + gzipped body) from a 23andMe raw data file.

The head is the file's own comment header; the body keeps each marker's rsID,
chromosome and position and drops the genotype.

Usage: python scripts/microarray_template_from_raw.py RAW_FILE NAME
       e.g. ... user6045_file4542_yearofbirth_unknown_sex_unknown.23andme.txt.gz 23andMe_V2
Writes src/wgsextract_cli/assets/microarray/raw_file_templates/{head/NAME.txt,body/NAME.txt.gz}.
"""

import gzip
import sys
from pathlib import Path

TEMPLATES = (
    Path(__file__).resolve().parents[1]
    / "src/wgsextract_cli/assets/microarray/raw_file_templates"
)


def main() -> None:
    raw, name = Path(sys.argv[1]), sys.argv[2]
    opener = gzip.open if raw.suffix == ".gz" else open
    head, body = [], []
    with opener(raw, "rt", newline="") as f:
        for line in f:
            if line.startswith("#"):
                head.append(line)
            elif line.strip():
                rsid, chrom, pos = line.split("\t")[:3]
                body.append(f"{rsid}\t{chrom}\t{pos}\n")
    (TEMPLATES / "head").mkdir(parents=True, exist_ok=True)
    (TEMPLATES / "body").mkdir(parents=True, exist_ok=True)
    (TEMPLATES / "head" / f"{name}.txt").write_text("".join(head), newline="")
    # mtime=0: the same input always gives the same bytes.
    with (TEMPLATES / "body" / f"{name}.txt.gz").open("wb") as out:
        with gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as gz:
            gz.write("".join(body).encode())
    print(f"{name}: {len(head)} header lines, {len(body)} markers")


if __name__ == "__main__":
    main()
