"""Extract marker-only templates from vendor exports.

Source comments are never published: headers use generic vendor boilerplate.
Bodies contain only marker ID, chromosome and position, including no-call rows.

Usage: python scripts/microarray_template_from_raw.py RAW_FILE TEMPLATE_NAME
Use --output-dir for an alternate template root, or --overwrite to rebuild.
"""

import argparse
import csv
import gzip
import re
from pathlib import Path

from wgsextract_cli.core.microarray_utils import get_template_format

TEMPLATES = (
    Path(__file__).resolve().parents[1]
    / "src/wgsextract_cli/assets/microarray/raw_file_templates"
)
_MARKER_ID = re.compile(r"(?:rs\d+|i\d+|kgp\d+|SNP_A-\d+|VG[0-9A-Za-z]+)", re.I)
_CHROMS = {*map(str, range(27)), "X", "Y", "XY", "M", "MT"}


def _header(name: str, eol: str) -> str:
    if name.startswith("MyHeritage_"):
        lines = [
            "##fileformat=MyHeritage",
            "##format=MHv1.0",
            *(["##chip=GSA"] if "_GSA_" in name else []),
            "##reference=build37",
            "# MyHeritage DNA raw data.",
            "RSID,CHROMOSOME,POSITION,RESULT",
        ]
    elif name.startswith("Ancestry_"):
        lines = [
            "#AncestryDNA raw data download",
            "#Data was collected using AncestryDNA array version: V2.0"
            if "_V2" in name
            else "#AncestryDNA array data",
            "#Human reference assembly build 37; forward (+) strand.",
            "rsid\tchromosome\tposition\tallele1\tallele2",
        ]
    elif name.startswith("FTDNA_"):
        lines = ["RSID,CHROMOSOME,POSITION,RESULT"]
    elif name.startswith("23andMe_"):
        lines = [
            "# This is a generic 23andMe-format raw data export.",
            "# Human reference assembly build 37; plus strand.",
            "# rsid\tchromosome\tposition\tgenotype",
        ]
    elif name.startswith("LDNA_"):
        lines = ["# rsid\tchromosome\tposition\tgenotype"]
    else:
        return ""
    return eol.join(lines) + eol


def build_template(
    raw: Path, name: str, root: Path = TEMPLATES, *, overwrite: bool = False
) -> int:
    fmt = get_template_format(name)
    if fmt is None or fmt["parts"] != 1:
        raise ValueError("Register a single-part template format before extracting it")
    head_path = root / "head" / f"{name}{fmt['suffix']}"
    body_path = root / "body" / f"{name}{fmt['suffix']}.gz"
    if not overwrite and any(
        path.exists() for path in (head_path, body_path, body_path.with_suffix(""))
    ):
        raise FileExistsError("Template already exists; choose a new ID or --overwrite")

    opener = gzip.open if raw.suffix == ".gz" else open
    body: list[str] = []
    eol = "\n"
    expected_columns = 5 if name.startswith("Ancestry_") else 4
    expected_build = "36" if fmt.get("ncbi36") else "37"
    schema_seen = name.startswith(("23andMe_", "Nucleus_", "LDNA_"))
    with opener(raw, "rt", encoding="utf-8-sig", newline="") as stream:
        for number, line in enumerate(stream, 1):
            if number == 1 and line.endswith("\r\n"):
                eol = "\r\n"
            if not line.strip():
                continue
            if line.startswith("#"):
                builds = re.findall(r"(?:build|GRCh|NCBI)\s*(3[678])\b", line, re.I)
                if any(build != expected_build for build in builds):
                    raise ValueError(f"Unexpected reference build at line {number}")
                continue
            fields = next(
                csv.reader([line], delimiter="\t" if "\t" in line else ",", strict=True)
            )
            fields = [field.strip() for field in fields]
            if [field.lower() for field in fields[:3]] == [
                "rsid",
                "chromosome",
                "position",
            ]:
                if body:
                    raise ValueError(f"Unexpected column header at line {number}")
                if len(fields) != expected_columns:
                    raise ValueError(f"Unexpected column count at line {number}")
                schema_seen = True
                continue
            if (
                not schema_seen
                or len(fields) != expected_columns
                or not _MARKER_ID.fullmatch(fields[0])
                or fields[1] not in _CHROMS
                or not fields[2].isdigit()
            ):
                raise ValueError(f"Invalid marker schema at line {number}")
            body.append("\t".join(fields[:3]) + "\n")
    if not body:
        raise ValueError("Export contains no markers")

    head = _header(name, eol)
    head_path.parent.mkdir(parents=True, exist_ok=True)
    body_path.parent.mkdir(parents=True, exist_ok=True)
    with body_path.open("wb" if overwrite else "xb") as output:
        with gzip.GzipFile(
            filename="", fileobj=output, mode="wb", mtime=0
        ) as compressed:
            compressed.write("".join(body).encode())
    if head:
        with head_path.open("w" if overwrite else "x", newline="") as output:
            output.write(head)
    elif overwrite:
        head_path.unlink(missing_ok=True)
    return len(body)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw", type=Path)
    parser.add_argument("name")
    parser.add_argument("--output-dir", type=Path, default=TEMPLATES)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    count = build_template(
        args.raw, args.name, args.output_dir, overwrite=args.overwrite
    )
    print(f"{args.name}: {count} markers; generic header, no source metadata")


if __name__ == "__main__":
    main()
