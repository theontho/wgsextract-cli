import gzip

import pytest

from scripts.microarray_template_from_raw import build_template


def test_extraction_never_copies_source_identity_or_genotypes(tmp_path):
    raw = tmp_path / "private-export.csv"
    raw.write_bytes(
        b"##fileformat=MyHeritage\r\n"
        b"#Name: Private Donor\r\n"
        b"#Kit: PRIVATE-KIT-XYZ\r\n"
        b"#Email: donor@example.invalid\r\n"
        b"##timestamp=2099-12-31T23:59:59Z\r\n"
        b"RSID,CHROMOSOME,POSITION,RESULT\r\n"
        b'"rs1","1","100","AG"\r\n'
        b"#Sample: PRIVATE-SAMPLE\r\n"
        b'"rs2","X","200","--"\r\n'
    )
    root = tmp_path / "templates"
    assert build_template(raw, "MyHeritage_GSA_2025", root) == 2
    head = (root / "head/MyHeritage_GSA_2025.csv").read_bytes()
    body = root / "body/MyHeritage_GSA_2025.csv.gz"
    assert gzip.decompress(body.read_bytes()) == b"rs1\t1\t100\nrs2\tX\t200\n"
    assert b"##chip=GSA\r\n" in head
    assert head.endswith(b"RSID,CHROMOSOME,POSITION,RESULT\r\n")
    assert head.count(b"\n") == head.count(b"\r\n")
    for secret in (
        b"Private Donor",
        b"PRIVATE-KIT",
        b"donor@",
        b"2099",
        b"PRIVATE-SAMPLE",
    ):
        assert secret not in head + gzip.decompress(body.read_bytes())
    assert body.read_bytes()[3] & 8 == 0  # No gzip filename.
    assert body.read_bytes()[4:8] == b"\0\0\0\0"


def test_template_bytes_do_not_depend_on_calls_or_missingness(tmp_path):
    bodies = []
    for index, calls in enumerate((("AA", "--"), ("TT", "AC"))):
        raw = tmp_path / f"source-{index}.csv"
        raw.write_text(
            "RSID,CHROMOSOME,POSITION,RESULT\n"
            f"rs1,1,100,{calls[0]}\nrs2,2,200,{calls[1]}\n"
        )
        root = tmp_path / f"templates-{index}"
        build_template(raw, "MyHeritage_GSA_2025", root)
        bodies.append((root / "body/MyHeritage_GSA_2025.csv.gz").read_bytes())
    assert bodies[0] == bodies[1]


@pytest.mark.parametrize(
    "content",
    [
        "Name,CHROMOSOME,POSITION,RESULT\nPrivateDonor,1,100,AA\n",
        "RSID,CHROMOSOME,RESULT,POSITION\nrs1,1,00,100\n",
        "RSID,CHROMOSOME,POSITION,RESULT\nrs1,1,AA,100\n",
        "RSID,CHROMOSOME,POSITION,RESULT\nrs1,AG,100,AA\n",
        "RSID,CHROMOSOME,POSITION,RESULT\nPRIVATE-KIT,1,100,AA\n",
        "RSID,CHROMOSOME,POSITION,RESULT\nrs1,1,100,AA,unexpected\n",
        "rs1,1,100,AA\n",
        "##reference=build38\nRSID,CHROMOSOME,POSITION,RESULT\nrs1,1,100,AA\n",
        "#An export without markers\n",
    ],
)
def test_invalid_or_reordered_schema_publishes_nothing(tmp_path, content):
    raw = tmp_path / "source.csv"
    raw.write_text(content)
    root = tmp_path / "templates"
    with pytest.raises(ValueError):
        build_template(raw, "MyHeritage_GSA_2025", root)
    assert not root.exists()


def test_existing_template_is_not_overwritten_without_explicit_permission(tmp_path):
    raw = tmp_path / "source.csv"
    raw.write_text("RSID,CHROMOSOME,POSITION,RESULT\nrs1,1,100,AA\n")
    root = tmp_path / "templates"
    build_template(raw, "MyHeritage_GSA_2025", root)
    original = (root / "body/MyHeritage_GSA_2025.csv.gz").read_bytes()
    raw.write_text("RSID,CHROMOSOME,POSITION,RESULT\nrs2,2,200,TT\n")
    with pytest.raises(FileExistsError):
        build_template(raw, "MyHeritage_GSA_2025", root)
    assert (root / "body/MyHeritage_GSA_2025.csv.gz").read_bytes() == original
    build_template(raw, "MyHeritage_GSA_2025", root, overwrite=True)
    assert (
        gzip.decompress((root / "body/MyHeritage_GSA_2025.csv.gz").read_bytes())
        == b"rs2\t2\t200\n"
    )


def test_ancestry_preserves_numeric_sex_chromosomes_without_alleles(tmp_path):
    raw = tmp_path / "source.txt.gz"
    with gzip.open(raw, "wt") as stream:
        stream.write(
            "#AncestryDNA raw data download\n"
            "rsid\tchromosome\tposition\tallele1\tallele2\n"
            "rs1\t23\t100\tA\tG\n"
            "rs2\t26\t200\t0\t0\n"
            "rs3\t20\t0\t0\t0\n"
        )
    root = tmp_path / "templates"
    build_template(raw, "Ancestry_V2_2021", root)
    assert (
        gzip.decompress((root / "body/Ancestry_V2_2021.txt.gz").read_bytes())
        == b"rs1\t23\t100\nrs2\t26\t200\nrs3\t20\t0\n"
    )


@pytest.mark.parametrize("suffix", [".txt", ".txt.gz"])
def test_existing_headless_template_is_not_shadowed(tmp_path, suffix):
    root = tmp_path / "templates"
    (root / "body").mkdir(parents=True)
    existing = root / "body" / f"Nucleus_V1{suffix}"
    content = b"rs1\t1\t100\n"
    existing.write_bytes(gzip.compress(content) if suffix.endswith(".gz") else content)
    original = existing.read_bytes()
    raw = tmp_path / "source.txt"
    raw.write_text("rs2\t1\t200\tAA\n")
    with pytest.raises(FileExistsError):
        build_template(raw, "Nucleus_V1", root)
    assert existing.read_bytes() == original
    assert list((root / "body").iterdir()) == [existing]
