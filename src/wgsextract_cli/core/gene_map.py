import csv
import gzip
import logging
from pathlib import Path

from wgsextract_cli.core.annotation_resources import CancelEvent
from wgsextract_cli.core.builds import canonical_build
from wgsextract_cli.core.ref_library.downloads import download_file
from wgsextract_cli.core.utils import WGSExtractError

# Official UCSC RefGene database URLs
# These files are approx 4-5MB compressed and contain all RefSeq gene models.
GENE_MAP_URLS = {
    "hg38": "https://hgdownload.soe.ucsc.edu/goldenPath/hg38/database/refGene.txt.gz",
    "hg19": "https://hgdownload.soe.ucsc.edu/goldenPath/hg19/database/refGene.txt.gz",
}


# Gene maps built by older versions merged a symbol's transcripts across contigs
# into one bogus span. No human gene spans more than ~2.5 Mb, so longer rows are
# ignored.
_MAX_GENE_SPAN = 3_000_000


def _is_primary_contig(chrom: str) -> bool:
    return "_" not in chrom


def gene_map_build(build: str | None) -> str:
    """Map a reference build to the gene-map build key, refusing unsupported builds."""
    key = canonical_build(build)
    if key in GENE_MAP_URLS:
        return key
    raise WGSExtractError(
        f"Gene name lookup is only available for hg19/hg38 references; "
        f"no gene map exists for build {build or 'unknown'}. Pass --region instead."
    )


class GeneMap:
    def __init__(self, reflib_dir: str) -> None:
        self.reflib_dir = reflib_dir
        self.maps: dict[str, dict[str, str]] = {}

    def load(self, build: str = "hg38") -> bool:
        build_key = gene_map_build(build)
        if build_key in self.maps:
            return True

        # Look for local file in reflib (ref/genes_hg38.tsv)
        map_file = str(Path(self.reflib_dir, "ref", f"genes_{build_key}.tsv"))
        logging.debug(f"GeneMap: Searching for {map_file}")
        if not Path(map_file).exists():
            # Try root/microarray as fallback
            map_file = str(
                Path(self.reflib_dir, "microarray", f"genes_{build_key}.tsv")
            )
            logging.debug(f"GeneMap: Searching for {map_file}")

        if not Path(map_file).exists():
            logging.debug("GeneMap: No gene map file found.")
            return False

        try:
            ranges: dict[str, tuple[str, int, int]] = {}
            stale_rows = 0
            with open(map_file) as f:
                for row in csv.DictReader(f, delimiter="\t"):
                    symbol = (row.get("symbol") or "").upper()
                    chrom = row.get("chrom") or ""
                    if not (symbol and chrom and row.get("start") and row.get("end")):
                        continue
                    start, end = int(row["start"]), int(row["end"])
                    if end - start > _MAX_GENE_SPAN:
                        stale_rows += 1
                        continue
                    ranges[symbol] = _merge_gene_range(
                        ranges.get(symbol), chrom, start, end
                    )
            if stale_rows:
                logging.warning(
                    f"Ignored {stale_rows} gene map rows spanning more than "
                    f"{_MAX_GENE_SPAN:,} bp in {map_file}; "
                    "run 'wgsextract ref gene-map' to rebuild the gene maps."
                )
            self.maps[build_key] = {
                symbol: f"{chrom}:{start}-{end}"
                for symbol, (chrom, start, end) in ranges.items()
            }
            return True
        except (OSError, csv.Error, ValueError) as e:
            logging.error(f"Failed to load gene map {map_file}: {e}")
            return False

    def get_coords(self, gene_symbol: str, build: str = "hg38") -> str | None:
        build_key = gene_map_build(build)
        if build_key not in self.maps:
            if not self.load(build):
                return None
        return self.maps[build_key].get(gene_symbol.upper())


def gene_coords(reflib_dir: str, gene_symbol: str, build: str = "hg38") -> str:
    """A gene's chrom:start-end, or WGSExtractError saying why it can't be found."""
    build_key = gene_map_build(build)
    if not gene_map_exists(reflib_dir, build_key):
        raise WGSExtractError(
            f"No {build_key} gene map in {reflib_dir}, so --gene {gene_symbol} can't "
            "be resolved. "
            "Run 'wgsextract ref gene-map' to download it, or pass --region."
        )
    coords = GeneMap(reflib_dir).get_coords(gene_symbol, build_key)
    if not coords:
        raise WGSExtractError(
            f"Gene {gene_symbol} isn't in the {build_key} gene map (RefSeq symbols, "
            "e.g. BRCA1); pass --region instead."
        )
    return coords


def are_gene_maps_installed(reflib_dir: str) -> bool:
    """Checks if gene mapping files for both hg19 and hg38 are installed."""
    if not reflib_dir:
        return False
    target_dir = str(Path(reflib_dir, "ref"))
    for build in GENE_MAP_URLS.keys():
        tsv_path = str(Path(target_dir, f"genes_{build}.tsv"))
        if not Path(tsv_path).exists():
            return False
    return True


def gene_map_exists(reflib_dir: str | None, build: str = "hg38") -> bool:
    """Return True when a gene map for the requested build exists in a reflib."""
    build_key = canonical_build(build)
    if not reflib_dir or build_key not in GENE_MAP_URLS:
        return False
    root = Path(reflib_dir)
    return (root / "ref" / f"genes_{build_key}.tsv").exists() or (
        root / "microarray" / f"genes_{build_key}.tsv"
    ).exists()


def resolve_gene_map_reflib(
    resolved_ref: str | None, configured_reflib: str | None, build: str = "hg38"
) -> str | None:
    """Choose the reflib that should be used for resolving gene names."""
    ref_candidates: list[str] = []
    inferred_reflib = None
    if resolved_ref:
        ref_parent = Path(resolved_ref).parent
        ref_grandparent = ref_parent.parent
        inferred_reflib = (
            str(ref_grandparent)
            if ref_parent.name in {"genomes", "ref", "microarray"}
            else str(ref_parent)
        )
        for ref_candidate in (ref_parent, ref_grandparent):
            candidate_str = str(ref_candidate)
            if candidate_str and candidate_str not in ref_candidates:
                ref_candidates.append(candidate_str)

    for reflib_candidate in ref_candidates:
        if gene_map_exists(reflib_candidate, build):
            return reflib_candidate
    if gene_map_exists(configured_reflib, build):
        return configured_reflib
    return inferred_reflib or configured_reflib


def delete_gene_maps(reflib_dir: str) -> bool:
    """Deletes gene mapping files from the reference library."""
    if not reflib_dir:
        return False
    target_dir = str(Path(reflib_dir, "ref"))
    success = True
    for build in GENE_MAP_URLS.keys():
        tsv_path = str(Path(target_dir, f"genes_{build}.tsv"))
        if Path(tsv_path).exists():
            try:
                Path(tsv_path).unlink()
            except OSError as e:
                logging.error(f"Failed to delete {tsv_path}: {e}")
                success = False
    return success


def download_gene_maps(
    reflib_dir: str, cancel_event: CancelEvent | None = None
) -> bool:
    """Downloads and processes official UCSC RefGene maps."""
    target_dir = str(Path(reflib_dir, "ref"))
    Path(target_dir).mkdir(parents=True, exist_ok=True)

    success = True
    for build, url in GENE_MAP_URLS.items():
        if cancel_event and cancel_event.is_set():
            return False

        gz_path = str(Path(target_dir, f"refGene_{build}.txt.gz"))
        tsv_path = str(Path(target_dir, f"genes_{build}.tsv"))

        logging.info(f"Downloading {build} gene database from UCSC...")
        try:
            # 1. Download
            if not download_file(url, gz_path, cancel_event=cancel_event):
                success = False
                continue

            if cancel_event and cancel_event.is_set():
                if Path(gz_path).exists():
                    Path(gz_path).unlink()
                return False

            # 2. Parse UCSC format to simple TSV
            # UCSC refGene columns: 2=chrom, 4=txStart, 5=txEnd, 12=name2(Symbol)
            gene_data: dict[str, tuple[str, int, int]] = {}

            with gzip.open(gz_path, "rt") as f_in:
                for i, line in enumerate(f_in):
                    if i % 1000 == 0 and cancel_event and cancel_event.is_set():
                        return False

                    cols = line.split("\t")
                    if len(cols) < 13:
                        continue

                    symbol = cols[12].upper()
                    gene_data[symbol] = _merge_gene_range(
                        gene_data.get(symbol), cols[2], int(cols[4]), int(cols[5])
                    )

            # 3. Write processed TSV, 1-based (refGene's txStart is 0-based),
            # through a temporary file so a failed write leaves no partial map.
            tmp_tsv = tsv_path + ".tmp"
            with open(tmp_tsv, "w") as f_out:
                f_out.write("symbol\tchrom\tstart\tend\n")
                for symbol in sorted(gene_data.keys()):
                    c, s, e = gene_data[symbol]
                    f_out.write(f"{symbol}\t{c}\t{s + 1}\t{e}\n")
            Path(tmp_tsv).replace(tsv_path)

            logging.info(f"Successfully processed {len(gene_data)} genes for {build}")
            Path(gz_path).unlink()  # Cleanup

        except (OSError, EOFError, gzip.BadGzipFile, ValueError) as e:
            logging.error(f"Failed to setup {build} gene map: {e}")
            Path(tsv_path + ".tmp").unlink(missing_ok=True)
            success = False

    return success


def _merge_gene_range(
    current: tuple[str, int, int] | None, chrom: str, start: int, end: int
) -> tuple[str, int, int]:
    """Extend a gene's span with one transcript, never across contigs or loci.

    Primary-assembly placements win over alt/fix/random contigs; otherwise the
    first contig seen keeps the gene (e.g. PAR genes placed on both chrX and chrY).
    """
    if current is None:
        return chrom, start, end
    cur_chrom, cur_start, cur_end = current
    if chrom == cur_chrom:
        merged = (chrom, min(cur_start, start), max(cur_end, end))
        # A second copy far along the same chromosome is a duplication, not an isoform.
        return merged if merged[2] - merged[1] <= _MAX_GENE_SPAN else current
    if _is_primary_contig(chrom) and not _is_primary_contig(cur_chrom):
        return chrom, start, end
    return current
