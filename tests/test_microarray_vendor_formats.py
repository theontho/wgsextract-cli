"""Vendor raw-data files written from a CombinedKit, and the hg38->hg19 liftover.

Templates here mirror the layout and quirks of the reference library's real ones
(`ref bootstrap`): AncestryDNA numbers chrX/Y/PAR/MT as 23/24/25/26, 23andMe's
API list is chrom, position, rsID, CSV bodies are quoted, and multi-part chips
are split into numbered bodies.
"""

import gzip
from pathlib import Path
from types import SimpleNamespace

import pytest

from wgsextract_cli.commands import microarray
from wgsextract_cli.core.microarray_utils import (
    convert_to_vendor_format,
    liftover_hg38_to_hg19,
)
from wgsextract_cli.core.utils import WGSExtractError

KIT = [
    ("rs1", "1", "1000", "AC"),
    ("rs2", "1", "2000", "CT"),
    ("rs3", "1", "3000", "GT"),
    ("rsNo", "1", "4000", "--"),
    ("rsX", "X", "5000", "GG"),
    ("rsPAR", "X", "60100", "AG"),
    ("rsY", "Y", "6000", "TT"),
    ("rsMT", "MT", "7000", "CC"),
]


def _kit(path: Path, rows=KIT) -> str:
    path.write_text(
        "# RSID\tCHROM\tPOS\tRESULT\n" + "".join("\t".join(r) + "\n" for r in rows)
    )
    return str(path)


def _templates(root: Path, name: str, head: str, *bodies: str) -> Path:
    """raw_file_templates/{head,body} for one format; several bodies are parts."""
    templates = root / "raw_file_templates"
    (templates / "head").mkdir(parents=True, exist_ok=True)
    (templates / "body").mkdir(exist_ok=True)
    stem, suffix = name.rsplit(".", 1)
    (templates / "head" / name).write_text(head, newline="")
    for i, body in enumerate(bodies, 1):
        part = f"_{i}" if len(bodies) > 1 else ""
        (templates / "body" / f"{stem}{part}.{suffix}").write_text(body, newline="")
    return root


def _convert(tmp_path: Path, fmt: str, suffix: str, head: str, *bodies: str) -> str:
    root = _templates(tmp_path / "lib", f"{fmt}{suffix}", head, *bodies)
    out = tmp_path / f"out{suffix}"
    convert_to_vendor_format(fmt, _kit(tmp_path / "kit.txt"), str(out), str(root))
    return out.read_bytes().decode()


def test_23andme_writes_the_header_then_the_template_markers_in_order(tmp_path):
    head = "# 23andMe header\n# rsid\tchromosome\tposition\tgenotype\n"
    body = (
        "rsMT\tMT\t7000\t\r\nrs2\t1\t2000\t\r\nrsGone\t1\t9999\t\r\nrsY\tY\t6000\t\r\n"
    )

    out = _convert(tmp_path, "23andMe_V3", ".txt", head, body)

    # 23andMe writes MT and Y calls as one letter.
    assert out == head + (
        "rsMT\tMT\t7000\tC\nrs2\t1\t2000\tCT\nrsGone\t1\t9999\t--\nrsY\tY\t6000\tT\n"
    )


def test_ancestry_splits_alleles_into_columns_and_writes_no_calls_as_zero(tmp_path):
    out = _convert(
        tmp_path,
        "Ancestry_V2",
        ".txt",
        "rsid\tchromosome\tposition\tallele1\tallele2\r\n",
        "rs1\t1\t1000\r\nrsNo\t1\t4000\r\nrsGone\t1\t1\r\n",
        *[""] * 4,
    )

    assert out.splitlines()[1:] == [
        "rs1\t1\t1000\tA\tC",
        "rsNo\t1\t4000\t0\t0",
        "rsGone\t1\t1\t0\t0",
    ]


def test_ancestry_numbers_sex_chromosomes_and_mt(tmp_path):
    out = _convert(
        tmp_path,
        "Ancestry_V2",
        ".txt",
        "rsid\tchromosome\tposition\tallele1\tallele2\r\n",
        "rs1\t1\t1000\r\n",
        "rsX\t23\t5000\r\n",
        "rsY\t24\t6000\r\n",
        "rsPAR\t25\t60100\r\n",
        "rsMT\t26\t7000\r\n",
    )

    assert out.splitlines()[1:] == [
        "rs1\t1\t1000\tA\tC",
        "rsX\t23\t5000\tG\tG",
        "rsY\t24\t6000\tT\tT",
        "rsPAR\t25\t60100\tA\tG",
        "rsMT\t26\t7000\tC\tC",
    ]


def test_multi_part_bodies_are_written_in_part_order(tmp_path):
    out = _convert(
        tmp_path,
        "FTDNA_V3",
        ".csv",
        "RSID,CHROMOSOME,POSITION,RESULT\r\n",
        "rs1,1,1000,\r\n",
        "rsMT,MT,7000,\r\n",
        "rsX,X,5000,\r\n",
    )

    # FTDNA v3 is unquoted.
    assert out.splitlines() == [
        "RSID,CHROMOSOME,POSITION,RESULT",
        "rs1,1,1000,AC",
        "rsMT,MT,7000,CC",
        "rsX,X,5000,GG",
    ]


def test_ftdna_v2_quotes_every_field(tmp_path):
    out = _convert(
        tmp_path,
        "FTDNA_V2",
        ".csv",
        "RSID,CHROMOSOME,POSITION,RESULT\n",
        '"rs1","1","1000",\n"rsNo","1","4000",\n',
    )

    assert out == (
        'RSID,CHROMOSOME,POSITION,RESULT\n"rs1","1","1000","AC"\n"rsNo","1","4000","--"\n'
    )


def test_myheritage_v1_writes_ct_and_gt_reversed_and_v2_does_not(tmp_path):
    body = '"rs1","1","1000",\n"rs2","1","2000",\n"rs3","1","3000",\n'
    v1 = _convert(tmp_path / "v1", "MyHeritage_V1", ".csv", "RSID\n", body)
    v2 = _convert(tmp_path / "v2", "MyHeritage_V2", ".csv", "RSID\n", body)

    assert v1.splitlines()[1:] == [
        '"rs1","1","1000","AC"',
        '"rs2","1","2000","TC"',
        '"rs3","1","3000","TG"',
    ]
    assert v2.splitlines()[1:] == [
        '"rs1","1","1000","AC"',
        '"rs2","1","2000","CT"',
        '"rs3","1","3000","GT"',
    ]


def test_living_dna_is_tab_separated_with_double_dash_no_calls(tmp_path):
    out = _convert(
        tmp_path,
        "LDNA_V2",
        ".txt",
        "# rsid\tchromosome\tposition\tgenotype\r\n",
        "rsX\tX\t5000\t\r\nrsNo\t1\t4000\t\r\n",
    )

    assert out.splitlines()[1:] == ["rsX\tX\t5000\tGG", "rsNo\t1\t4000\t--"]


def test_23andme_api_reads_its_chrom_position_rsid_template(tmp_path):
    out = _convert(
        tmp_path,
        "23andMe_SNPs_API",
        ".txt",
        "# rsid\tchromosome\tposition\tgenotype\n",
        "chr1\t1000\trs1\nchrX\t5000\trsX\n",
    )

    assert out.splitlines()[1:] == ["rs1\t1\t1000\tAC", "rsX\tX\t5000\tGG"]


def test_unknown_format_falls_back_to_tab_separated(tmp_path):
    out = _convert(tmp_path, "Custom_V1", ".txt", "", "rs3\t1\t3000\nrsY\tY\t6000\n")

    assert out == "rs3\t1\t3000\tGT\nrsY\tY\t6000\tTT\n"


def test_a_corrupt_body_leaves_no_partial_vendor_file(tmp_path):
    root = _templates(tmp_path / "lib", "23andMe_V3.txt", "# head\n", "")
    body = root / "raw_file_templates" / "body"
    (body / "23andMe_V3.txt").unlink()
    (body / "23andMe_V3.txt.gz").write_bytes(gzip.compress(b"rs1\t1\t1000\n")[:-12])
    out = tmp_path / "out.txt"

    with pytest.raises(EOFError):
        convert_to_vendor_format(
            "23andMe_V3", _kit(tmp_path / "kit.txt"), str(out), str(root)
        )

    assert sorted(p.name for p in tmp_path.iterdir()) == ["kit.txt", "lib"]


def _convert_outputs(tmp_path: Path, formats: str, build: str | None = "hg19"):
    lib_root = tmp_path / "lib"
    _templates(
        lib_root, "23andMe_V5.txt", "# h\n", "rs1\t1\t1000\n", "rsMT\tMT\t7000\n"
    )
    _templates(lib_root, "FTDNA_V2.csv", "H\n", '"rs1","1","1000",\n')
    ref = lib_root / "ref.fa"
    ref.touch()
    outdir = tmp_path / "out"
    outdir.mkdir()
    microarray._convert_microarray_outputs(
        args=SimpleNamespace(formats=formats),
        outdir=str(outdir),
        base_name="sample",
        lib=SimpleNamespace(root=str(lib_root), build=build, liftover_chain=None),
        combined_kit_txt=_kit(outdir / "sample_CombinedKit.txt"),
        ref_fasta=str(ref),
        start_total=0.0,
    )
    return outdir


def test_vendor_files_are_named_after_the_input_and_template(tmp_path):
    outdir = _convert_outputs(tmp_path, "all,23andMe-V5,ftdna_v2")

    assert sorted(p.name for p in outdir.iterdir()) == [
        "sample_23andMe_V5.txt",
        "sample_CombinedKit.txt",
        "sample_FTDNA_V2.csv",
    ]
    assert (outdir / "sample_23andMe_V5.txt").read_text() == (
        "# h\nrs1\t1\t1000\tAC\nrsMT\tMT\t7000\tC\n"
    )


def test_all_writes_only_the_combined_kit(tmp_path):
    outdir = _convert_outputs(tmp_path, "all")

    assert [p.name for p in outdir.iterdir()] == ["sample_CombinedKit.txt"]


def test_unknown_format_without_a_template_names_the_format(tmp_path):
    with pytest.raises(WGSExtractError, match="Failed to generate nonsense_v9"):
        _convert_outputs(tmp_path, "nonsense_v9")


def test_t2t_input_is_refused(tmp_path):
    with pytest.raises(WGSExtractError, match="T2T"):
        _convert_outputs(tmp_path, "23andme_v5", build="T2Tv20")


# --- hg38 -> hg19 liftover ---------------------------------------------------


def _chain(path: Path, *chains: str) -> str:
    """A UCSC chain file (hg38 = target, hg19 = query)."""
    path.write_text("\n\n".join(chains) + "\n")
    return str(path)


def _lift(tmp_path: Path, rows, chain: str, **kwargs) -> list[list[str]]:
    src = tmp_path / "kit_hg38.txt"
    src.write_text("".join("\t".join(r) + "\n" for r in rows))
    out = tmp_path / "kit_hg19.txt"
    liftover_hg38_to_hg19(str(src), str(out), chain, **kwargs)
    return [line.split("\t") for line in out.read_text().splitlines()]


# hg38 chr1 [0,1000) -> hg19 chr1 [100,1100); hg38 chr1 [2000,3000) -> hg19
# chr1 [5000,6000) (0-based, half-open); chr2 maps onto an alt contig.
TWO_BLOCKS = (
    "chain 1 chr1 10000 + 0 3000 chr1 10000 + 100 6000 1\n1000 1000 3900\n1000",
    "chain 1 chr2 10000 + 0 1000 chr2_alt 10000 + 0 1000 2\n1000",
    "chain 1 chrX 10000 + 0 1000 chrX 10000 + 0 1000 3\n1000",
)


def test_liftover_sorts_by_chromosome_and_drops_alt_contigs(tmp_path):
    rows = [
        ("rsX", "X", "10", "GG"),
        ("rsB", "1", "2500", "CT"),
        ("rsAlt", "2", "10", "AA"),
        ("rsA", "1", "500", "AC"),
        ("rsGap", "1", "1500", "TT"),
    ]

    lifted = _lift(tmp_path, rows, _chain(tmp_path / "c.chain", *TWO_BLOCKS))

    assert lifted == [
        ["rsA", "1", "600", "AC"],
        ["rsB", "1", "5500", "CT"],
        ["rsX", "X", "10", "GG"],
    ]


def test_liftover_without_a_header_uses_the_23andme_v3_head(tmp_path):
    root = _templates(tmp_path / "lib", "23andMe_V3.txt", "# v3 head\n", "")

    lifted = _lift(
        tmp_path,
        [("rsA", "1", "500", "AC")],
        _chain(tmp_path / "c.chain", *TWO_BLOCKS),
        templates_dir=str(root),
    )

    assert lifted == [["# v3 head"], ["rsA", "1", "600", "AC"]]


def test_liftover_missing_chain_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        _lift(tmp_path, [], str(tmp_path / "missing.chain"))


def test_liftover_maps_block_edges_one_based(tmp_path):
    rows = [("rsLast", "1", "1000", "AC"), ("rsBefore", "1", "2000", "CT")]

    lifted = _lift(tmp_path, rows, _chain(tmp_path / "c.chain", *TWO_BLOCKS))

    # hg38 1-based 1000 is 0-based 999, the first block's last base; 2000 is
    # 0-based 1999, in the gap.
    assert lifted == [["rsLast", "1", "1100", "AC"]]


def test_liftover_complements_genotypes_in_inverted_regions(tmp_path):
    chain = _chain(
        tmp_path / "c.chain",
        "chain 1 chr1 10000 + 0 1000 chr1 10000 - 0 1000 1\n1000",
    )

    lifted = _lift(tmp_path, [("rsInv", "1", "1", "AC")], chain)

    # hg38 chr1:1 is the last base of hg19's reverse-strand block: chr1:10000.
    # AC complements to TG, written in alphabetical order.
    assert lifted == [["rsInv", "1", "10000", "GT"]]


# --- 23andMe haploid calls, line endings, template fixes ----------------------


def _x_kit(path: Path, het_every: int) -> str:
    """A kit with 200 non-PAR chrX calls (every het_every-th heterozygous), a
    PAR call, and an MT call."""
    rows = [
        (f"rsX{i}", "X", str(3_000_000 + i), "AG" if i % het_every == 0 else "GG")
        for i in range(200)
    ]
    rows += [("rsPAR", "X", "60100", "CC"), ("rsMT", "MT", "7000", "TT")]
    return _kit(path, rows)


def _convert_x_kit(tmp_path: Path, het_every: int) -> dict[str, str]:
    body = "rsX1\tX\t3000001\nrsX2\tX\t3000002\nrsPAR\tX\t60100\nrsMT\tMT\t7000\n"
    root = _templates(tmp_path / "lib", "23andMe_V3.txt", "# h\n", body)
    out = tmp_path / "out.txt"
    kit = _x_kit(tmp_path / "kit.txt", het_every)
    convert_to_vendor_format("23andMe_V3", kit, str(out), str(root))
    return dict(line.split("\t")[::3] for line in out.read_text().splitlines()[1:])


def test_23andme_writes_a_mans_x_outside_the_pars_as_one_letter(tmp_path):
    calls = _convert_x_kit(tmp_path, het_every=1000)

    assert calls == {"rsX1": "G", "rsX2": "G", "rsPAR": "CC", "rsMT": "T"}


def test_23andme_keeps_a_womans_x_diploid(tmp_path):
    calls = _convert_x_kit(tmp_path, het_every=3)

    assert calls == {"rsX1": "GG", "rsX2": "GG", "rsPAR": "CC", "rsMT": "T"}


def test_body_line_endings_follow_the_head(tmp_path):
    out = _convert(tmp_path, "23andMe_V3", ".txt", "# h\r\n", "rs1\t1\t1000\n")

    assert out == "# h\r\nrs1\t1\t1000\tAC\r\n"


def test_vendor_xy_chromosome_is_the_x_par(tmp_path):
    out = _convert(
        tmp_path,
        "MyHeritage_V2",
        ".csv",
        "RSID,CHROMOSOME,POSITION,RESULT\n",
        '"rsPAR","XY","60100"\n',
    )

    assert out.splitlines()[1] == '"rsPAR","XY","60100","AG"'


def test_template_corrections_move_yoruba_mt_positions_to_the_rcrs(tmp_path):
    root = _templates(
        tmp_path / "lib", "23andMe_V4.txt", "# h\n", "rs397507452\tMT\t7444\n", ""
    )
    kit = _kit(tmp_path / "kit.txt", [("i1", "MT", "7443", "AA")])
    out = tmp_path / "out.txt"

    convert_to_vendor_format("23andMe_V4", kit, str(out), str(root))

    assert out.read_text() == "# h\nrs397507452\tMT\t7443\tA\n"


def test_ftdna_v1_finds_calls_at_its_markers_lifted_positions(tmp_path):
    root = _templates(
        tmp_path / "lib",
        "FTDNA_V1_Affy.csv",
        "RSID,CHROMOSOME,POSITION,RESULT\n",
        "rsA,1,500\nrsB,1,1500\n",
    )
    # NCBI36 chr1 [0,1000) is GRCh37 chr1 [100,1100); 1500 is unmapped.
    chain = _chain(
        tmp_path / "hg18ToHg19.chain",
        "chain 1 chr1 10000 + 0 1000 chr1 10000 + 100 1100 1\n1000",
    )
    kit = _kit(tmp_path / "kit.txt", [("rsOther", "1", "600", "CT")])
    out = tmp_path / "out.csv"

    convert_to_vendor_format("FTDNA_V1_Affy", kit, str(out), str(root), chain)

    assert out.read_text().splitlines()[1:] == ["rsA,1,500,CT", "rsB,1,1500,--"]


def test_ftdna_v1_complements_calls_in_regions_inverted_since_ncbi36(tmp_path):
    root = _templates(
        tmp_path / "lib",
        "FTDNA_V1_Affy.csv",
        "RSID,CHROMOSOME,POSITION,RESULT\n",
        "rsInv,1,1\n",
    )
    # NCBI36 chr1:1 is the last base of GRCh37's reverse-strand block: chr1:10000.
    chain = _chain(
        tmp_path / "hg18ToHg19.chain",
        "chain 1 chr1 10000 + 0 1000 chr1 10000 - 0 1000 1\n1000",
    )
    kit = _kit(tmp_path / "kit.txt", [("rsInv", "1", "10000", "AC")])
    out = tmp_path / "out.csv"

    convert_to_vendor_format("FTDNA_V1_Affy", kit, str(out), str(root), chain)

    assert out.read_text().splitlines()[1:] == ["rsInv,1,1,GT"]
