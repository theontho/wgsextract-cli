import gzip
import io
import json
from pathlib import Path

import pytest

from tests.htslib_helpers import needs_htslib, run_wgsextract, sam_to_bam
from wgsextract_cli.core import ybrowse
from wgsextract_cli.core.config import settings
from wgsextract_cli.core.utils import WGSExtractError, run_command


def mini_snapshot(path: Path, rows: list[str] | None = None) -> Path:
    # Synthetic metadata/alleles only; never an upstream dataset or its samples.
    path.write_text(
        "##fileformat=VCFv4.2\n"
        "##source=SYNTHETIC_TEST_NOT_YBROWSE_DATA\n"
        f'##copyright="{ybrowse.COPYRIGHT}"\n'
        f"##contig=<ID=chrY,length={ybrowse.Y_LENGTH}>\n"
        '##INFO=<ID=AA,Number=1,Type=String,Description="Synthetic ancestral allele">\n'
        '##INFO=<ID=HG,Number=1,Type=String,Description="Synthetic HG">\n'
        '##INFO=<ID=ISOGG,Number=1,Type=String,Description="Synthetic ISOGG">\n'
        '##FORMAT=<ID=GT,Number=1,Type=String,Description="Synthetic GT">\n'
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSYNTHETIC\n"
        + "\n".join(
            rows
            if rows is not None
            else [
                "chrY\t11\tSYN_C\tA\tC\t.\t.\tHG=R-C;ISOGG=R1;AA=A\tGT\t0",
                "chrY\t11\tSYN_ALIAS\tA\tC\t.\t.\tHG=R-C2;ISOGG=R2;AA=A\tGT\t0",
                "chrY\t11\tSYN_G\tA\tG\t.\t.\tHG=R-G;AA=A\tGT\t1",
                "chrY\t21\tSYN_T\tA\tT\t.\t.\tHG=R-T;AA=T\tGT\t1",
                "chrY\t31\tSYN_MISSING\tA\tG\t.\t.\tHG=R-MISSING;AA=A\tGT\t1",
            ]
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


@needs_htslib
def test_install_strips_samples_aggregates_only_matching_alleles_and_detects_corruption(
    tmp_path,
):
    source = mini_snapshot(tmp_path / "mini.vcf")
    library = tmp_path / "library"
    installed = ybrowse.install_snapshot(library, source)
    with gzip.open(installed / "sites.vcf.gz", "rt") as stream:
        text = stream.read()
    assert "FORMAT" not in text and "SYNTHETIC" not in text
    assert f'##copyright="{ybrowse.COPYRIGHT}"' in text
    records = [
        line.split("\t") for line in text.splitlines() if not line.startswith("#")
    ]
    assert records[0][2] == "SYN_ALIAS;SYN_C"
    assert records[0][7] == "HG=R-C,R-C2;ISOGG=R1,R2;AA=A"
    assert records[1][2] == "SYN_G" and records[1][7] == "HG=R-G;AA=A"
    manifest = json.loads((installed / "manifest.json").read_text())
    assert manifest["source_kind"] == "user-supplied"
    assert manifest["source_records"] == 5 and manifest["snp_alleles"] == 4
    assert manifest["copyright"] == ybrowse.COPYRIGHT
    assert ybrowse.installed_snapshot(library)[0] == installed
    (installed / "targets.tsv.gz").write_bytes(b"corrupt")
    with pytest.raises(WGSExtractError, match="checksum"):
        ybrowse.installed_snapshot(library)
    ybrowse.install_snapshot(library, source)
    assert ybrowse.installed_snapshot(library)[0] == installed


def test_download_drift_fails_without_replacing_installed_pointer(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        ybrowse.urllib.request,
        "urlopen",
        lambda *a, **kw: io.BytesIO(b"changed upstream"),
    )
    root = tmp_path / "ybrowse" / "hg38"
    root.mkdir(parents=True)
    pointer = root / "current.json"
    pointer.write_text('{"version":"previous"}')
    with pytest.raises(WGSExtractError, match="hash mismatch.*--snapshot"):
        ybrowse.install_snapshot(tmp_path)
    assert pointer.read_text() == '{"version":"previous"}'
    assert list(root.iterdir()) == [pointer]


@pytest.mark.parametrize(
    "change,error",
    [
        (("57227415", "59373566"), "GRCh37/T2T"),
        ((ybrowse.COPYRIGHT, "Unlicensed"), "copyright"),
        (("##INFO=<ID=HG,", "##INFO=<ID=OTHER,"), "INFO/HG"),
        (("ID=HG,Number=1", "ID=HG,Number=A"), "allele-indexed INFO/HG"),
        (("chrY\t11", "chr1\t11"), "only Y"),
    ],
)
def test_bad_snapshot_headers_and_coordinates_fail(tmp_path, change, error):
    source = mini_snapshot(tmp_path / "mini.vcf")
    source.write_text(source.read_text().replace(*change))
    with pytest.raises(WGSExtractError, match=error):
        ybrowse.install_snapshot(tmp_path / "library", source)
    assert not (tmp_path / "library/ybrowse/hg38/current.json").exists()


@pytest.fixture
def synthetic_y(tmp_path):
    reference = tmp_path / "ref.fa"
    with reference.open("w", newline="\n") as stream:
        stream.write(">chrY\n")
        for start in range(0, ybrowse.Y_LENGTH, 100_000):
            stream.write("A" * min(100_000, ybrowse.Y_LENGTH - start) + "\n")
    run_command(["samtools", "faidx", str(reference)])
    lines = [f"@SQ\tSN:chrY\tLN:{ybrowse.Y_LENGTH}"]
    for i in range(30):
        lines.append(
            f"r{i}\t{16 if i % 2 else 0}\tchrY\t11\t60\t20M\t*\t0\t0"
            "\tCAAAGAAAAAAAAAAAAAAA\tIIIIIIIIIIIIIIIIIIII"
        )
    bam = sam_to_bam(lines, tmp_path / "sample.bam")
    return reference, bam


@needs_htslib
def test_actual_targeted_call_preserves_called_gt_and_allele_specific_names(
    tmp_path, synthetic_y, monkeypatch
):
    reference, bam = synthetic_y
    library = tmp_path / "library"
    monkeypatch.setitem(settings, "reference_library", str(library))
    run_wgsextract(
        "ref",
        "ybrowse",
        "--install",
        "--snapshot",
        str(mini_snapshot(tmp_path / "mini.vcf")),
        "--ref",
        str(library),
    )
    out = tmp_path / "output"
    run_wgsextract(
        "extract",
        "ydna-vcf",
        "--ybrowse",
        "--input",
        bam,
        "--ref",
        str(reference),
        "--outdir",
        str(out),
        "--threads",
        "2",
    )
    output = out / "sample_Y_ybrowse.vcf.gz"
    records = run_command(
        ["bcftools", "query", "-f", "%POS\t%ID\t%ALT\t%INFO/HG[\t%GT]\n", str(output)],
        capture_output=True,
    ).stdout.splitlines()
    assert "11\tSYN_ALIAS;SYN_C\tC\tR-C,R-C2\t1" in records
    assert "11\tSYN_G\tG\tR-G\t." in records
    assert not any(row.startswith("15\t") for row in records)
    assert all(not row.endswith("\t0") for row in records if row.startswith("31\t"))
    ancestral = run_command(
        ["bcftools", "query", "-f", "%POS\t%ID\t%INFO/AA[\t%GT]\n", str(output)],
        capture_output=True,
    ).stdout.splitlines()
    assert "21\tSYN_T\tT\t0" in ancestral
    header = run_command(
        ["bcftools", "view", "-h", str(output)], capture_output=True
    ).stdout
    assert f'##copyright="{ybrowse.COPYRIGHT}"' in header
    assert "##ybrowse_snapshot_sha256=" in header
    assert "\tSYNTHETIC" not in header
    assert (out / "sample_Y_ybrowse.vcf.gz.tbi").exists()
    assert not (out / "sample_Y.vcf.gz").exists()
    assert not list(out.glob(".ybrowse_*"))
    Path(str(output) + ".csi").write_bytes(b"obsolete CSI")
    Path(str(output) + ".tbi").write_bytes(b"obsolete TBI")
    run_wgsextract(
        "extract",
        "ydna-vcf",
        "--ybrowse",
        "--input",
        bam,
        "--ref",
        str(reference),
        "--outdir",
        str(out),
        "--threads",
        "2",
    )
    assert not Path(str(output) + ".csi").exists()
    refreshed = run_command(
        [
            "bcftools",
            "query",
            "-r",
            "chrY:11-11",
            "-f",
            "%POS\t%ID[\t%GT]\n",
            str(output),
        ],
        capture_output=True,
    ).stdout
    assert "11\tSYN_ALIAS;SYN_C\t1" in refreshed


@needs_htslib
def test_reference_mismatch_is_rejected_before_extracting_reads(
    tmp_path, synthetic_y, monkeypatch
):
    from wgsextract_cli.commands import extract

    reference, bam = synthetic_y
    library = tmp_path / "library"
    ybrowse.install_snapshot(
        library,
        mini_snapshot(
            tmp_path / "mini.vcf", ["chrY\t11\tSYN_BAD\tC\tG\t.\t.\tHG=R-BAD\tGT\t0"]
        ),
    )
    monkeypatch.setitem(settings, "reference_library", str(library))
    monkeypatch.setattr(
        extract, "_extract_regions", lambda *a, **kw: pytest.fail("must not extract")
    )
    with pytest.raises(WGSExtractError, match="Reference allele mismatch|REF alleles"):
        run_wgsextract(
            "extract",
            "ydna-vcf",
            "--ybrowse",
            "--input",
            bam,
            "--ref",
            str(reference),
            "--outdir",
            str(tmp_path / "output"),
        )
