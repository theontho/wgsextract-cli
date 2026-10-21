"""PharmGKB (now ClinPGx) clinical annotations as an hg38 table by allele.

ClinPGx publishes its clinical annotations by rsID, with no coordinates, and
each variant's GRCh38 location and alleles (as HGVS synonyms) in a separate
file. `ref pharmgkb` joins the two into ref/pharmgkb_hg38.tsv.gz (#CHROM, POS,
REF, ALT, PHARMGKB; bgzipped and tabix-indexed) for `vcf pharmgkb`, which
annotates the records carrying one of those alleles; matching by allele keeps a
gVCF reference block that merely spans a site from being labelled with it.
Left out: star alleles and haplotypes (CYP2D6*4, HLA-B*15:02), which have no
single position, and the few indels, whose HGVS doesn't give VCF alleles.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import tempfile
import zipfile
from collections import defaultdict
from collections.abc import Iterable, Iterator
from pathlib import Path

from wgsextract_cli.core.download_progress import CancelEvent, ProgressCallback
from wgsextract_cli.core.utils import run_command

from .ref_library import download_file

CLINICAL_URL = "https://api.clinpgx.org/v1/download/file/data/clinicalAnnotations.zip"
VARIANTS_URL = "https://api.clinpgx.org/v1/download/file/data/variants.zip"
DEST_NAME = "pharmgkb_hg38.tsv.gz"
INFO_HEADER = (
    '##INFO=<ID=PHARMGKB,Number=.,Type=String,Description="ClinPGx (PharmGKB) '
    'clinical annotations: rsID|gene|level of evidence|phenotype category|drugs">'
)

# GRCh38's RefSeq accessions (a location on any other version is another build).
_GRCH38_VERSIONS = "11 12 12 12 10 12 14 11 12 11 10 12 11 9 10 10 11 10 10 11 9 11"
_GRCH38_ACCESSIONS = {
    **{
        f"NC_{n:06d}.{version}": f"chr{n}"
        for n, version in enumerate(_GRCH38_VERSIONS.split(), start=1)
    },
    "NC_000023.11": "chrX",
    "NC_000024.10": "chrY",
    "NC_012920.1": "chrM",
}
_CHROM_ORDER = {chrom: i for i, chrom in enumerate(_GRCH38_ACCESSIONS.values())}
_RSID = re.compile(r"^rs\d+$")
# INFO values can't hold whitespace, ';', '=' or ',' (the value separator).
_UNSAFE = str.maketrans({" ": "_", ";": "/", ",": "/", "=": ":", "\t": "_"})


def _rows(text: str) -> Iterator[dict[str, str]]:
    yield from csv.DictReader(io.StringIO(text), delimiter="\t")


def _snv_alleles(
    row: dict[str, str], accession: str, pos: str
) -> list[tuple[str, str]]:
    """(REF, ALT) pairs from the variant's HGVS synonyms at its location."""
    snv = re.compile(rf"^{re.escape(accession)}:g\.{pos}([ACGT])>([ACGT])$")
    return sorted(
        {
            (match[1], match[2])
            for synonym in (row.get("Synonyms") or "").split(",")
            if (match := snv.match(synonym.strip()))
        }
    )


def build_table(
    clinical_tsv: str, variants_tsv: str
) -> list[tuple[str, int, str, str, str]]:
    """(chrom, pos, ref, alt, PHARMGKB value) per hg38 SNV allele, sorted for tabix."""
    alleles: dict[str, tuple[str, int, list[tuple[str, str]]]] = {}
    other_build = 0
    for row in _rows(variants_tsv):
        accession, _, pos = (row.get("Location") or "").partition(":")
        if not pos.isdigit():
            continue
        chrom = _GRCH38_ACCESSIONS.get(accession)
        if chrom is None:
            other_build += 1
            continue
        snvs = _snv_alleles(row, accession, pos)
        if snvs:
            alleles[row["Variant Name"]] = (chrom, int(pos), snvs)
    if other_build:
        logging.info(f"PharmGKB: skipped {other_build} variants not on GRCh38.")

    by_site: dict[str, list[str]] = defaultdict(list)
    unplaced = 0
    for row in _rows(clinical_tsv):
        rsid = row["Variant/Haplotypes"].strip()
        if not _RSID.match(rsid) or rsid not in alleles:
            unplaced += 1
            continue
        fields = (rsid, row["Gene"], row["Level of Evidence"],
                  row["Phenotype Category"], row["Drug(s)"])  # fmt: skip
        by_site[rsid].append("|".join(f.strip().translate(_UNSAFE) for f in fields))
    logging.info(
        f"PharmGKB: {sum(map(len, by_site.values()))} clinical annotations at "
        f"{len(by_site)} sites; {unplaced} on haplotypes, indels or unplaced "
        "variants skipped."
    )
    return sorted(
        (
            (chrom, pos, ref, alt, ",".join(values))
            for rsid, values in by_site.items()
            for chrom, pos, snvs in [alleles[rsid]]
            for ref, alt in snvs
        ),
        key=lambda row: (_CHROM_ORDER[row[0]], row[1], row[3]),
    )


def _member(zip_path: str, suffix: str) -> str:
    with zipfile.ZipFile(zip_path) as archive:
        (name,) = [n for n in archive.namelist() if n.endswith(suffix)]
        return archive.read(name).decode("utf-8")


def write_table(rows: Iterable[tuple[str, int, str, str, str]], dest: str) -> None:
    """Write, bgzip and tabix-index the table at dest (a .tsv.gz path)."""
    plain = dest[: -len(".gz")]
    with open(plain, "w") as out:
        out.write("#CHROM\tPOS\tREF\tALT\tPHARMGKB\n")
        out.writelines("\t".join(map(str, row)) + "\n" for row in rows)
    run_command(["bgzip", "-f", plain], capture_output=True)
    run_command(["tabix", "-f", "-s", "1", "-b", "2", "-e", "2", dest],
                capture_output=True)  # fmt: skip


def download_pharmgkb(
    reflib_dir: str,
    cancel_event: CancelEvent | None = None,
    progress_callback: ProgressCallback | None = None,
) -> bool:
    """Download ClinPGx's clinical annotations and variant locations and build
    ref/pharmgkb_hg38.tsv.gz from them."""
    target_dir = str(Path(reflib_dir, "ref"))
    Path(target_dir).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target_dir, prefix=".pharmgkb_") as work:
        downloads = {}
        for name, url in (("clinical", CLINICAL_URL), ("variants", VARIANTS_URL)):
            logging.info(f"Downloading PharmGKB {name} from ClinPGx...")
            path = str(Path(work, f"{name}.zip"))
            if not download_file(url, path, progress_callback, cancel_event):
                return False
            downloads[name] = path
        sites = build_table(
            _member(downloads["clinical"], "clinical_annotations.tsv"),
            _member(downloads["variants"], "variants.tsv"),
        )
        if not sites:
            logging.error("PharmGKB: no clinical annotation could be placed on hg38.")
            return False
        dest = str(Path(target_dir, DEST_NAME))
        write_table(sites, dest)
    # Earlier versions saved ClinPGx's archive unconverted; nothing reads it.
    stale = str(Path(target_dir, "pharmgkb_hg38.zip"))
    if Path(stale).exists():
        Path(stale).unlink()
    logging.info(f"PharmGKB table ready: {dest}")
    return True
