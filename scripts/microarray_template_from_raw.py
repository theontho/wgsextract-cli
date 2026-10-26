"""Make a microarray template (head + gzipped body) from a vendor raw data file.

The head is everything above the first marker: the file's comment header and,
for AncestryDNA and MyHeritage, its column-name line, with its line endings
kept. A link to the customer's own 23andMe account is replaced by the generic
download page. The body keeps each marker's ID, chromosome and position
(tab-separated) and drops the genotype; a headerless export (Nucleus) gets no
head file.

Usage: python scripts/microarray_template_from_raw.py RAW_FILE NAME
       e.g. ... user6045_file4542_yearofbirth_unknown_sex_unknown.23andme.txt.gz 23andMe_V2
Writes src/wgsextract_cli/assets/microarray/raw_file_templates/{head/NAME.txt,body/NAME.txt.gz}.
"""

import gzip
import re
import sys
from pathlib import Path

TEMPLATES = (
    Path(__file__).resolve().parents[1]
    / "src/wgsextract_cli/assets/microarray/raw_file_templates"
)
_ACCOUNT_URL = re.compile(r"https://you\.23andme\.com/p/[0-9a-f]+/")


def _marker(line: str) -> tuple[str, str, str] | None:
    fields = [f.strip().strip('"') for f in re.split(r"[\t,]", line.rstrip("\r\n"))]
    if len(fields) < 3 or not fields[2].isdigit():
        return None
    return fields[0], fields[1], fields[2]


def main() -> None:
    raw, name = Path(sys.argv[1]), sys.argv[2]
    opener = gzip.open if raw.suffix == ".gz" else open
    head: list[str] = []
    body: list[str] = []
    with opener(raw, "rt", newline="") as f:
        for line in f:
            marker = _marker(line) if not line.startswith("#") else None
            if marker:
                body.append("\t".join(marker) + "\n")
            elif not body and line.strip():
                head.append(_ACCOUNT_URL.sub("https://you.23andme.com/", line))
    (TEMPLATES / "head").mkdir(parents=True, exist_ok=True)
    (TEMPLATES / "body").mkdir(parents=True, exist_ok=True)
    if head:
        (TEMPLATES / "head" / f"{name}.txt").write_text("".join(head), newline="")
    # mtime=0: the same input always gives the same bytes.
    with (TEMPLATES / "body" / f"{name}.txt.gz").open("wb") as out:
        with gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as gz:
            gz.write("".join(body).encode())
    print(f"{name}: {len(head)} header lines, {len(body)} markers")


if __name__ == "__main__":
    main()
