import gzip
import hashlib
import io
import itertools
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scripts.build_microarray_chm13_map import write_site_map
from wgsextract_cli.core.microarray_chm13 import (
    load_chm13_catalogue,
    validate_chm13_alignment_header,
    validate_chm13_reference,
    write_chm13_ploidy,
)
from wgsextract_cli.core.microarray_site_map import _CHROMS, SiteProjection
from wgsextract_cli.core.sample_ploidy import _sex_aware
from wgsextract_cli.core.utils import WGSExtractError


def test_packaged_catalogue_loads_and_roundtrips_native_sites():
    catalogue = load_chm13_catalogue()
    mapping = catalogue.mapping
    examples = [
        *itertools.islice(mapping.native_sites(), 0, None, 100_000),
        next(mapping.native_range("chrM", 1, catalogue.contigs["chrM"].length)),
    ]

    for site in examples:
        assert site.target_chrom is not None and site.target_pos is not None
        assert mapping.source_lookup(site.source_chrom, site.source_pos) == site
        assert mapping.native_lookup(site.target_chrom, site.target_pos) == site


def _catalogue(directory: Path, *, bad_mask: bool = False) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    alignment = b">rCRS\nA\n>CHM13\nA\n"
    compressed = gzip.compress(alignment, mtime=0)
    alignment_name = (
        "mt-alignment-v1-" + hashlib.sha256(alignment).hexdigest()[:16] + ".fa.gz"
    )
    (directory / alignment_name).write_bytes(compressed)
    lengths = dict.fromkeys(_CHROMS, 100)
    lengths.update(chrY=80, chrM=16569)
    profile = {
        "chrY_PAR_masking": "unmasked" if bad_mask else "masked",
        "contigs": [
            {
                "name": chrom,
                "length": length,
                "md5": (
                    "ec493a132ac4823aa696e37109f64972"
                    if chrom == "chrM"
                    else hashlib.md5(b"A" * length).hexdigest()
                ),
            }
            for chrom, length in lengths.items()
        ],
        "sha256": "0" * 64,
        "fai_sha256": "0" * 64,
        "gzi_sha256": "0" * 64,
        "par_intervals": [
            {"chrom": "chrX", "start0": 0, "end0": 10},
            {"chrom": "chrX", "start0": 90, "end0": 100},
            {"chrom": "chrY", "start0": 0, "end0": 8},
            {"chrom": "chrY", "start0": 75, "end0": 80},
        ],
    }
    return write_site_map(
        [SiteProjection("chr1", 10, "chr1", 20, False, "mapped", "ref_equal")],
        lengths,
        lengths,
        directory,
        {
            "native_reference": profile,
            "mitochondria": {
                "alignment_file": alignment_name,
                "alignment_sha256": hashlib.sha256(compressed).hexdigest(),
                "alignment_uncompressed_sha256": hashlib.sha256(alignment).hexdigest(),
            },
        },
    )


def test_ploidy_uses_native_par_boundaries_and_haploid_mitochondria(
    tmp_path: Path,
) -> None:
    catalogue = load_chm13_catalogue(_catalogue(tmp_path))
    path = write_chm13_ploidy(
        catalogue, {chrom: chrom for chrom in catalogue.contigs}, tmp_path
    )
    rows = {tuple(line.split()) for line in path.read_text().splitlines()}
    assert {
        ("chrX", "1", "10", "M", "2"),
        ("chrX", "11", "90", "M", "1"),
        ("chrX", "91", "100", "M", "2"),
        ("chrX", "1", "100", "F", "2"),
        ("chrY", "1", "8", "M", "0"),
        ("chrY", "9", "75", "M", "1"),
        ("chrY", "76", "80", "M", "0"),
        ("chrY", "1", "80", "F", "0"),
        ("chrM", "1", "16569", "M", "1"),
        ("chrM", "1", "16569", "F", "1"),
    } <= rows
    args = ["--ploidy-file", str(path)]
    assert not _sex_aware(args, "chr20")
    assert not _sex_aware(args, "chrM")
    assert _sex_aware(args, "chrX")
    assert _sex_aware(args, None)


def test_catalogue_rejects_a_different_y_masking_profile(tmp_path: Path) -> None:
    with pytest.raises(WGSExtractError, match="PAR masking"):
        load_chm13_catalogue(_catalogue(tmp_path, bad_mask=True))


def test_catalogue_detects_reference_metadata_corruption(tmp_path: Path) -> None:
    manifest = _catalogue(tmp_path)
    data = json.loads(manifest.read_text())
    data["provenance"]["native_reference"]["par_intervals"][0]["end0"] = 11
    manifest.write_text(json.dumps(data))
    with pytest.raises(WGSExtractError, match="manifest SHA256"):
        load_chm13_catalogue(manifest)


def test_catalogue_detects_pinned_alignment_corruption(tmp_path: Path) -> None:
    manifest = _catalogue(tmp_path)
    alignment = next(tmp_path.glob("mt-alignment-v1-*.fa.gz"))
    alignment.write_bytes(alignment.read_bytes() + b"corrupt")
    with pytest.raises(WGSExtractError, match="alignment SHA256"):
        load_chm13_catalogue(manifest)


@pytest.mark.parametrize("chrom", ["chrM", "chrY"])
def test_alignment_cannot_substitute_a_same_length_reference_variant(
    tmp_path: Path, chrom: str
) -> None:
    catalogue = load_chm13_catalogue(_catalogue(tmp_path))
    names = {name: name for name in catalogue.contigs}
    header = "".join(
        f"@SQ\tSN:{name}\tLN:{contig.length}\tM5:{contig.md5}\n"
        for name, contig in catalogue.contigs.items()
    )
    assert validate_chm13_alignment_header(header, catalogue, names) == list(names)
    original = catalogue.contigs[chrom].md5
    bad = header.replace(
        f"SN:{chrom}\tLN:{catalogue.contigs[chrom].length}\tM5:{original}",
        f"SN:{chrom}\tLN:{catalogue.contigs[chrom].length}\tM5:{'0' * 32}",
    )
    with pytest.raises(WGSExtractError, match="reference variant"):
        validate_chm13_alignment_header(bad, catalogue, names)
    with pytest.raises(WGSExtractError, match="reference variant"):
        validate_chm13_alignment_header(
            header.replace(f"\tM5:{original}", ""), catalogue, names
        )


def test_reference_rejects_same_length_wrong_mitochondrial_sequence(
    tmp_path: Path,
) -> None:
    catalogue = load_chm13_catalogue(_catalogue(tmp_path))
    fasta = tmp_path / "input.fa"
    fasta.write_text(">unused\nA\n")
    Path(str(fasta) + ".fai").write_text(
        "".join(
            f"{chrom}\t{contig.length}\t0\t1\t2\n"
            for chrom, contig in catalogue.contigs.items()
        )
    )

    def command(args: list[str], **_kwargs: object) -> object:
        chrom = args[-1]
        process = MagicMock()
        process.__enter__.return_value = process
        process.stdout = io.BytesIO(
            f">{chrom}\n{'A' * catalogue.contigs[chrom].length}\n".encode()
        )
        process.wait.return_value = 0
        return process

    with (
        patch("wgsextract_cli.core.microarray_chm13.subprocess.Popen", command),
        pytest.raises(WGSExtractError, match="chrM"),
    ):
        validate_chm13_reference(fasta, catalogue)
