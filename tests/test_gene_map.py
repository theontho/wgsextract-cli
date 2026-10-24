"""Gene map download, parsing, loading and deletion, with the UCSC download faked."""

import gzip
from pathlib import Path

import pytest

from wgsextract_cli.core import gene_map as gm
from wgsextract_cli.core.gene_map import (
    GENE_MAP_URLS,
    GeneMap,
    are_gene_maps_installed,
    delete_gene_maps,
    download_gene_maps,
    gene_map_exists,
)


def _refgene_row(symbol, chrom, tx_start, tx_end):
    """A refGene.txt line: chrom, txStart, txEnd and name2 at columns 2, 4, 5, 12."""
    cols = ["0", "NM_1", chrom, "+", str(tx_start), str(tx_end), *["0"] * 6, symbol]
    return "\t".join([*cols, "cmpl", "cmpl", "0,\n"])


REFGENE = "".join(
    [
        _refgene_row("BRCA1", "chr17", 43044294, 43125364),
        _refgene_row("BRCA1", "chr17", 43044294, 43170245),  # longer isoform
        _refgene_row("Kcnq2", "chr20", 63400000, 63472677),
        _refgene_row("HLA-A", "chr6_GL000251v2_alt", 1000, 5000),
        _refgene_row("HLA-A", "chr6", 29941259, 29945884),
        _refgene_row("SHOX", "chrX", 624343, 659411),
        _refgene_row("SHOX", "chrY", 624343, 659411),  # PAR copy: first contig wins
        "too\tfew\tcolumns\n",
    ]
)


@pytest.fixture
def ucsc(monkeypatch):
    """Serve payloads[build] (bytes, or None to fail) as that build's refGene."""
    urls = {url: build for build, url in GENE_MAP_URLS.items()}
    payloads: dict[str, bytes | None] = {
        b: gzip.compress(REFGENE.encode()) for b in GENE_MAP_URLS
    }

    def download(url, dest, progress_callback=None, cancel_event=None):
        data = payloads[urls[url]]
        if data is None:
            return False
        Path(dest).write_bytes(data)
        return True

    monkeypatch.setattr(gm, "download_file", download)
    return payloads


def _tsv(lib: Path, build: str) -> list[str]:
    return (lib / "ref" / f"genes_{build}.tsv").read_text().splitlines()


def test_download_builds_sorted_merged_gene_maps(tmp_path, ucsc):
    assert not are_gene_maps_installed(str(tmp_path))
    assert download_gene_maps(str(tmp_path))
    assert are_gene_maps_installed(str(tmp_path))
    assert sorted(p.name for p in (tmp_path / "ref").iterdir()) == [
        "genes_hg19.tsv",
        "genes_hg38.tsv",
    ]
    assert _tsv(tmp_path, "hg38")[0] == "symbol\tchrom\tstart\tend"
    assert [line.split("\t")[0] for line in _tsv(tmp_path, "hg38")[1:]] == [
        "BRCA1",
        "HLA-A",
        "KCNQ2",
        "SHOX",
    ]
    genes = GeneMap(str(tmp_path))
    assert genes.get_coords("brca1", "GRCh38").startswith("chr17:4304429")
    assert genes.get_coords("BRCA1", "hg38").endswith("-43170245")
    assert genes.get_coords("HLA-A", "hg38").startswith("chr6:")
    assert genes.get_coords("SHOX", "hg19").startswith("chrX:")


def test_gene_region_is_one_based(tmp_path, ucsc):
    ucsc["hg38"] = gzip.compress(_refgene_row("TINY", "chr1", 100, 200).encode())
    assert download_gene_maps(str(tmp_path))
    assert GeneMap(str(tmp_path)).get_coords("TINY", "hg38") == "chr1:101-200"


def test_one_failed_build_fails_the_download_but_keeps_the_other(tmp_path, ucsc):
    ucsc["hg19"] = None
    assert not download_gene_maps(str(tmp_path))
    assert gene_map_exists(str(tmp_path), "hg38")
    assert not gene_map_exists(str(tmp_path), "hg19")
    assert not are_gene_maps_installed(str(tmp_path))


def test_non_gzip_download_writes_no_gene_map(tmp_path, ucsc):
    ucsc["hg38"] = b"<html>404</html>"
    assert not download_gene_maps(str(tmp_path))
    assert not gene_map_exists(str(tmp_path), "hg38")
    assert gene_map_exists(str(tmp_path), "hg19")


def test_truncated_download_fails_cleanly(tmp_path, ucsc):
    whole = gzip.compress((REFGENE * 50).encode())
    ucsc["hg38"] = whole[: len(whole) // 2]
    assert not download_gene_maps(str(tmp_path))
    assert not gene_map_exists(str(tmp_path), "hg38")


class _CancelAfter:
    """A cancel event that reports set from its nth check on."""

    def __init__(self, n: int) -> None:
        self.checks, self.n = 0, n

    def is_set(self) -> bool:
        self.checks += 1
        return self.checks >= self.n


@pytest.mark.parametrize("n", [1, 2, 3])
def test_cancelled_download_writes_no_gene_map(tmp_path, ucsc, n):
    # Checks: before downloading, after downloading, then while parsing.
    assert not download_gene_maps(str(tmp_path), cancel_event=_CancelAfter(n))
    assert not list((tmp_path / "ref").glob("genes_*.tsv"))
    if n == 2:
        assert not list((tmp_path / "ref").iterdir())


def test_delete_removes_both_maps(tmp_path, ucsc):
    assert not delete_gene_maps("")
    assert download_gene_maps(str(tmp_path))
    assert delete_gene_maps(str(tmp_path))
    assert not list((tmp_path / "ref").iterdir())
    assert delete_gene_maps(str(tmp_path))  # nothing to delete is still success


def test_delete_reports_a_map_it_cannot_remove(tmp_path):
    (tmp_path / "ref" / "genes_hg38.tsv").mkdir(parents=True)
    (tmp_path / "ref" / "genes_hg19.tsv").write_text("symbol\tchrom\tstart\tend\n")
    assert not delete_gene_maps(str(tmp_path))
    assert not (tmp_path / "ref" / "genes_hg19.tsv").exists()


def test_gene_map_falls_back_to_microarray_and_caches(tmp_path):
    micro = tmp_path / "microarray" / "genes_hg19.tsv"
    micro.parent.mkdir()
    micro.write_text(
        "symbol\tchrom\tstart\tend\nAPOE\t19\t45409011\t45412650\nNOEND\t19\t5\t\n"
    )
    assert gene_map_exists(str(tmp_path), "GRCh37")
    assert not gene_map_exists(str(tmp_path), "T2Tv20")
    assert not gene_map_exists(None)
    genes = GeneMap(str(tmp_path))
    assert genes.get_coords("apoe", "hg19") == "19:45409011-45412650"
    assert genes.get_coords("NOEND", "hg19") is None
    micro.unlink()
    assert genes.load("hg19")  # served from memory
    assert genes.get_coords("APOE", "hg19") == "19:45409011-45412650"
    assert genes.get_coords("APOE", "hg38") is None  # no hg38 map anywhere


def test_malformed_gene_map_loads_nothing(tmp_path, caplog):
    tsv = tmp_path / "ref" / "genes_hg38.tsv"
    tsv.parent.mkdir()
    tsv.write_text("symbol\tchrom\tstart\tend\nBRCA1\tchr17\tforty\t43125364\n")
    assert GeneMap(str(tmp_path)).get_coords("BRCA1", "hg38") is None
    assert "Failed to load gene map" in caplog.text
