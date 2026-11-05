"""Native Yleaf and Haplogrep integration using real bcftools/samtools."""

import csv
import gzip
import json
import logging
import random
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.haplogrep_helpers import mini_tree as mini_tree
from tests.htslib_helpers import (
    bgzip_index,
    needs_htslib,
    needs_tools,
    run_wgsextract,
    sam_to_bam,
    write_fasta,
)
from tests.yleaf_helpers import native_tables as native_tables
from wgsextract_cli.commands._lineage_ydna import _yleaf_build
from wgsextract_cli.core.utils import WGSExtractError
from wgsextract_cli.ydna.yleaf_native.trees import MANIFEST

# Haplogrep's trees are downloaded; serve a tiny one instead.
pytestmark = [needs_htslib, pytest.mark.usefixtures("mini_tree")]

_HAPLOGREP = '"SampleID"\t"Range"\t"Haplogroup"\t"Rank"\t"Quality"\n'


def _fake_tool(path: Path, body: str) -> str:
    """A Python stand-in that logs its argv, env and input records to calls.jsonl."""
    path.write_text(
        textwrap.dedent(
            """\
            import gzip, json, os, sys
            from pathlib import Path
            argv = sys.argv[1:]
            here = Path(__file__).parent

            def records(vcf):
                opener = gzip.open if vcf.endswith(".gz") else open
                with opener(vcf, "rt") as f:
                    lines = [l.rstrip("\\n").split("\\t") for l in f]
                return [l[:2] + l[3:5] + l[9:10] for l in lines if l[0][:1] != "#"]

            def log(**extra):
                entry = {"argv": argv, "env": os.environ.get("YLEAF_DATA_DIR"), **extra}
                with open(here / "calls.jsonl", "a") as f:
                    f.write(json.dumps(entry) + "\\n")

            """
        )
        + textwrap.dedent(body)
    )
    return str(path)


def _haplogrep(
    tmp_path: Path, result=_HAPLOGREP + '"kit"\t"1-16569"\t"H1c"\t"1"\t"0.93"\n', code=0
) -> str:
    return _fake_tool(
        tmp_path / "haplogrep.py",
        f"""\
        if not argv:
            sys.exit(print("Haplogrep 3 3.2.2"))
        given = argv[argv.index("--in") + 1]
        with gzip.open(given, "rt") as f:
            contigs = [l for l in f if l.startswith("##contig")]
        log(records=records(given), contigs=contigs)
        Path(argv[argv.index("--out") + 1]).write_text({result!r})
        sys.exit({code})
        """,
    )


def _calls(tmp_path: Path) -> list[dict]:
    lines = (tmp_path / "calls.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


def _vcf(path: Path, contigs: dict[str, int], body: str, meta: str = "") -> str:
    text = (
        "##fileformat=VCFv4.2\n" + meta
        + "".join(f"##contig=<ID={c},length={n}>\n" for c, n in contigs.items())
        + '##FORMAT=<ID=GT,Number=1,Type=String,Description="g">\n'
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tkit\n" + body
    )  # fmt: skip
    plain = path.with_suffix("")
    plain.write_text(text)
    return bgzip_index(plain, path)


_HG38 = {"chr1": 248_956_422, "chrX": 156_040_895, "chrY": 57_227_415, "chrM": 16_569}
_KIT_BODY = (
    "chr1\t1000\t.\tA\tG\t50\tPASS\t.\tGT\t0/1\n"
    "chrX\t5000\t.\tC\tT\t50\tPASS\t.\tGT\t1\n"
    "chrY\t2781480\t.\tC\tT\t50\tPASS\t.\tGT\t1\n"
    "chrY\t22739367\t.\tG\tA\t50\tPASS\t.\tGT\t1\n"
    "chrM\t73\t.\tA\tG\t50\tPASS\t.\tGT\t1\n"
    "chrM\t263\t.\tA\tG\t50\tPASS\t.\tGT\t1\n"
)


# --- y-haplogroup ------------------------------------------------------------


@pytest.mark.parametrize("tree_links", [False, True])
def test_y_haplogroup_from_vcf_uses_native_markers_only_on_chry(
    tmp_path,
    capsys,
    tree_links,
    native_tables,
):
    body = "chr1\t100\t.\tA\tT\t50\tPASS\t.\tGT\t1\n" + "".join(
        f"chrY\t{pos}\t.\tA\tG\t50\tPASS\t.\tGT\t{gt}\n"
        for pos, gt in ((100, 1), (110, 1), (120, 1), (130, 1), (140, 0), (150, 0))
    )
    vcf = _vcf(tmp_path / "kit.vcf.gz", _HG38, body, "##reference=GRCh38.fa\n")
    run_wgsextract(
        "lineage", "y-haplogroup", "--input", vcf, "--outdir", str(tmp_path / "o"),
        "--no-ref-inference",
        *(["--tree-links"] if tree_links else []),
    )  # fmt: skip
    assert sorted(p.name for p in (tmp_path / "o").iterdir()) == ["yleaf"]
    output = capsys.readouterr().out
    assert "R1b1\tM-leaf\tVCF\t6\t1.0\t1.0\t1.0\t1.0" in output
    assert "Ralf et al." in output
    # Issue #359: the result names the tree release it was called against.
    yfull = MANIFEST["trees"]["yfull"]
    assert (
        f"tree YFull YTree {yfull['version']} ({yfull['date']}, {yfull['license']})"
        in output
    )
    assert ("https://www.yfull.com/tree/M-leaf/" in output) is tree_links
    assert (
        "https://discover.familytreedna.com/y-dna/M-leaf/tree" in output
    ) is tree_links


@pytest.mark.parametrize(
    "prediction",
    [
        "Sample_name\tHg\tHg_marker\n",
        "Sample_name\tHg\tHg_marker\tValid_markers\nkit\tNA\t\t0\n",
        "Sample_name\tHg\tHg_marker\tValid_markers\nkit\tNA\t\t3\n",
    ],
)
def test_y_haplogroup_without_a_prediction_is_an_error(
    tmp_path, monkeypatch, prediction
):
    from wgsextract_cli.ydna.yleaf_native import analysis

    def no_call(_input, output, **_kwargs):
        output.mkdir(parents=True, exist_ok=True)
        (output / "hg_prediction.hg").write_text(prediction)
        return output / "hg_prediction.hg"

    # Yleaf's analysis is the boundary: what it writes when nothing matched.
    monkeypatch.setattr(analysis, "analyze", no_call)
    vcf = _vcf(tmp_path / "kit.vcf.gz", _HG38, _KIT_BODY)
    with pytest.raises(WGSExtractError, match="could not predict a Y haplogroup"):
        run_wgsextract(
            "lineage", "y-haplogroup", "--input", vcf, "--no-ref-inference",
        )  # fmt: skip


def test_mitochondrial_tree_links_are_opt_in(tmp_path, capsys):
    from wgsextract_cli.commands.lineage import _print_haplogrep_result

    output = tmp_path / "haplogrep.txt"
    output.write_text(_HAPLOGREP + '"kit"\t"1-16569"\t"H1c"\t"1"\t"0.93"\n')
    _print_haplogrep_result(str(output), None)
    assert "https://" not in capsys.readouterr().out
    _print_haplogrep_result(str(output), None, tree_links=True)
    text = capsys.readouterr().out
    assert "https://www.yfull.com/mtree/H1c/" in text
    assert "https://discover.familytreedna.com/mtdna/H1c/tree" in text


def test_y_haplogroup_uses_the_vcf_build(tmp_path, monkeypatch):
    from wgsextract_cli.ydna.yleaf_native import analysis

    seen = []

    def stop(*args, **kwargs):
        seen.append(kwargs["build"])
        raise WGSExtractError("stop")

    monkeypatch.setattr(analysis, "analyze", stop)
    hg19 = {"chr1": 249_250_621, "chrY": 59_373_566}
    body = "chrY\t2655180\t.\tG\tA\t50\tPASS\t.\tGT\t1\n"
    vcf = _vcf(tmp_path / "kit.vcf.gz", hg19, body, "##reference=hg19.fa\n")
    with pytest.raises(WGSExtractError, match="stop"):
        run_wgsextract("lineage", "y-haplogroup", "--input", vcf)
    assert seen == ["hg19"]


@pytest.mark.parametrize(
    ("reference_build", "no_inference", "mismatch"),
    [("hg19", False, True), ("hg38", False, False), ("hg19", True, False)],
)
def test_y_build_override_cannot_infer_against_an_incompatible_fasta(
    tmp_path, monkeypatch, reference_build, no_inference, mismatch
):
    from wgsextract_cli.commands import _lineage_ydna
    from wgsextract_cli.ydna.yleaf_native import analysis

    reference = write_fasta(tmp_path / f"{reference_build}.fa", {"chrY": "A" * 400})
    vcf = _vcf(
        tmp_path / "kit.vcf.gz",
        {"chrY": 400},
        "chrY\t130\t.\tA\tG\t50\tPASS\t.\tGT\t1\n",
    )
    monkeypatch.setattr(
        _lineage_ydna,
        "ReferenceLibrary",
        lambda *a, **k: SimpleNamespace(fasta=str(reference), build="hg19"),
    )

    def stop(*args, **kwargs):
        assert kwargs["build"] == "hg38"
        assert kwargs["reference"] == reference
        raise WGSExtractError("analysis reached")

    monkeypatch.setattr(analysis, "analyze", stop)
    with pytest.raises(
        WGSExtractError,
        match="selected reference FASTA is hg19" if mismatch else "analysis reached",
    ):
        run_wgsextract(
            "lineage", "y-haplogroup", "--input", vcf, "--build", "hg38",
            *(["--no-ref-inference"] if no_inference else []),
        )  # fmt: skip


@pytest.mark.parametrize("indexed", [True, False])
def test_native_y_vcf_query_uses_regions_only_when_indexed(
    tmp_path, monkeypatch, indexed
):
    from wgsextract_cli.ydna.yleaf_native import analysis

    vcf = Path(
        _vcf(
            tmp_path / "kit.vcf.gz",
            {"chr1": 1000, "chrY": 1000, "Y": 1000},
            "chr1\t100\t.\tA\tT\t50\tPASS\t.\tGT\t1\n"
            "chrY\t130\t.\tA\tG\t50\tPASS\t.\tGT\t1\n"
            "Y\t140\t.\tA\tT\t50\tPASS\t.\tGT\t1\n",
        )
    )
    if not indexed:
        plain = vcf.with_suffix("")
        plain.write_bytes(gzip.decompress(vcf.read_bytes()))
        vcf = plain
    commands = []
    run = analysis.run_command

    def capture(command, **kwargs):
        commands.append(command)
        return run(command, **kwargs)

    monkeypatch.setattr(analysis, "run_command", capture)
    calls, has_ad = analysis._query_vcf(vcf)
    assert not has_ad
    assert {pos: call.base for pos, call in calls.items()} == {130: "G", 140: "T"}
    query = next(command for command in commands if "-f" in command)
    assert ("-r" in query) is indexed
    if indexed:
        assert query[query.index("-r") + 1] == "chrY,Y"


@pytest.mark.parametrize(
    ("build", "yleaf"),
    [("hg38", "hg38"), ("hs37d5", "hg19"), ("GRCh37", "hg19"), ("t2t", "t2t")],
)
def test_yleaf_build_names(build, yleaf):
    assert _yleaf_build(build) == yleaf


def test_yleaf_build_defaults_to_hg38_with_a_warning(caplog):
    with caplog.at_level(logging.WARNING):
        assert _yleaf_build(None) == "hg38"
    assert "not supported by Yleaf" in caplog.text


def test_y_haplogroup_vcf_without_chry_fails(tmp_path, native_tables):
    body = _KIT_BODY.replace("chrY", "chrX")
    vcf = _vcf(tmp_path / "f.vcf.gz", _HG38, body, "##reference=GRCh38.fa\n")
    with pytest.raises(WGSExtractError, match="no records on chromosome Y"):
        run_wgsextract("lineage", "y-haplogroup", "--input", vcf, "--no-ref-inference")
    assert not (tmp_path / "calls.jsonl").exists()


def test_y_haplogroup_reports_no_matching_markers(tmp_path, native_tables):
    vcf = _vcf(tmp_path / "kit.vcf.gz", _HG38, _KIT_BODY, "##reference=GRCh38.fa\n")
    with pytest.raises(WGSExtractError, match="could not predict"):
        run_wgsextract(
            "lineage", "y-haplogroup", "--input", vcf,
            "--no-ref-inference",
        )  # fmt: skip


def test_y_haplogroup_fastq_requires_explicit_alignment(tmp_path):
    ref = write_fasta(tmp_path / "ref.fa", {"chrY": "ACGT" * 100})
    fq = tmp_path / "kit.fastq.gz"
    fq.write_bytes(gzip.compress(b"@r1\nACGT\n+\nIIII\n"))
    argv = ["lineage", "y-haplogroup", "--input", str(fq), "--ref", str(ref)]
    with pytest.raises(WGSExtractError, match="Align FASTQ"):
        run_wgsextract(*argv)


def _reads(chrom: str, genome: str, n: int, start: int = 0, snp: int | None = None):
    """``n`` 50 bp reads at ``start``, with ALT C at 0-based ``snp`` if given."""
    seq = genome[start : start + 50]
    if snp is not None:
        seq = seq[: snp - start] + "C" + seq[snp - start + 1 :]
    return [
        f"{chrom}_{i}\t0\t{chrom}\t{start + 1}\t60\t50M\t*\t0\t0\t{seq}\t{'I' * 50}"
        for i in range(n)
    ]


def test_y_haplogroup_from_cram_decodes_with_the_reference(tmp_path, native_tables):
    rng = random.Random(7)
    genome = "".join(rng.choice("AG") for _ in range(400))
    ref = write_fasta(tmp_path / "ref.fa", {"chrX": genome, "chrY": genome})
    header = ["@SQ\tSN:chrX\tLN:400", "@SQ\tSN:chrY\tLN:400"]
    # A derived marker at position 130 (A>G).
    genome = "".join(
        "G" if pos in (100, 110, 120, 130) else "A" for pos in range(1, 401)
    )
    ref = write_fasta(tmp_path / "ref.fa", {"chrX": genome, "chrY": genome})
    bam = sam_to_bam(
        header + _reads("chrY", genome, 20, start=95), tmp_path / "kit.bam"
    )
    run_wgsextract(
        "lineage", "y-haplogroup", "--input", bam, "--ref", str(ref),
    )  # fmt: skip
    bam_prediction = (tmp_path / "yleaf/hg_prediction.hg").read_text()

    from wgsextract_cli.core.utils import run_command

    cram = str(tmp_path / "kit.cram")
    run_command(["samtools", "view", "-C", "-T", str(ref), "-o", cram, bam])
    run_command(["samtools", "index", cram])
    run_wgsextract(
        "lineage", "y-haplogroup", "--input", cram, "--ref", str(ref),
    )  # fmt: skip
    assert (tmp_path / "yleaf/hg_prediction.hg").read_text() == bam_prediction


def test_y_haplogroup_from_cram_without_a_reference_fails(
    tmp_path, monkeypatch, native_tables
):
    genome = "".join(random.Random(7).choices("ACGT", k=400))
    encoder = tmp_path / "encoder"
    encoder.mkdir()
    ref = write_fasta(encoder / "ref.fa", {"chrX": genome, "chrY": genome})
    header = ["@SQ\tSN:chrX\tLN:400", "@SQ\tSN:chrY\tLN:400"]
    bam = sam_to_bam(header + _reads("chrY", genome, 20, start=95), encoder / "k.bam")
    cram = str(tmp_path / "kit.cram")

    from wgsextract_cli.core.utils import run_command

    run_command(["samtools", "view", "-C", "-T", str(ref), "-o", cram, bam])
    run_command(["samtools", "index", cram])
    # Nothing can find the encoding reference: not beside the CRAM, not in a
    # library, and not from htslib's MD5 lookup (which defaults to the network).
    for path in encoder.iterdir():
        path.unlink()
    monkeypatch.setenv("REF_PATH", str(tmp_path / "no-ref-cache"))
    monkeypatch.delenv("REF_CACHE", raising=False)

    with pytest.raises(WGSExtractError, match="reference FASTA.*CRAM"):
        run_wgsextract("lineage", "y-haplogroup", "--input", cram)
    assert not list(tmp_path.rglob("hg_prediction.hg"))


def test_y_haplogroup_rejects_unsupported_input(tmp_path):
    sam = tmp_path / "kit.sam"
    sam.write_text("@SQ\tSN:chrY\tLN:10\n")
    with pytest.raises(WGSExtractError, match="Native Yleaf accepts"):
        run_wgsextract(
            "lineage", "y-haplogroup", "--input", str(sam),
        )  # fmt: skip


@pytest.mark.parametrize(
    ("meta", "contigs", "expected"),
    [
        (
            "##reference=chm13v2.0.fa\n",
            {"chr1": 248_387_328, "chrY": 62_460_029},
            "t2t",
        ),
        (
            "##reference=human_g1k_v37.fasta\n",
            {"1": 249_250_621, "Y": 59_373_566},
            "hg19",
        ),
    ],
)
def test_y_haplogroup_detects_build_from_vcf_contig_lengths(
    tmp_path,
    meta,
    contigs,
    expected,
    monkeypatch,
):
    y = "chrY" if "chrY" in contigs else "Y"
    vcf = _vcf(
        tmp_path / "kit.vcf.gz",
        contigs,
        f"{y}\t100\t.\tA\tG\t50\tPASS\t.\tGT\t1\n",
        meta,
    )
    from wgsextract_cli.ydna.yleaf_native import analysis

    seen = []

    def stop(*args, **kwargs):
        seen.append(kwargs["build"])
        raise WGSExtractError("stop")

    monkeypatch.setattr(analysis, "analyze", stop)
    with pytest.raises(WGSExtractError, match="stop"):
        run_wgsextract("lineage", "y-haplogroup", "--input", vcf)
    assert seen == [expected]


# --- mt-haplogroup -----------------------------------------------------------


@pytest.mark.parametrize("mt", ["chrM", "MT"])
def test_mt_haplogroup_from_vcf_hands_haplogrep_only_mtdna(tmp_path, capsys, mt):
    contigs = {(mt if c == "chrM" else c): n for c, n in _HG38.items()}
    vcf = _vcf(tmp_path / "kit.vcf.gz", contigs, _KIT_BODY.replace("chrM", mt))
    run_wgsextract(
        "lineage", "mt-haplogroup", "--input", vcf, "--outdir", str(tmp_path / "o"),
        "--haplogrep-path", _haplogrep(tmp_path),
        *(["--tree-links"] if mt == "chrM" else []),
    )  # fmt: skip
    (call,) = _calls(tmp_path)
    # contract: the pinned default tree, named as Haplogrep's id@version.
    assert call["argv"][call["argv"].index("--tree") + 1] == "phylotree-fu-rcrs@1.0"
    assert call["records"] == [[mt, "73", "A", "G", "1"], [mt, "263", "A", "G", "1"]]
    assert sorted(p.name for p in (tmp_path / "o").iterdir()) == [
        "haplogrep_results.txt"
    ]
    out = capsys.readouterr().out
    assert "Haplogroup      : H1c" in out and "Quality         : 0.93" in out
    assert ("https://www.yfull.com/mtree/H1c/" in out) is (mt == "chrM")
    assert ("https://discover.familytreedna.com/mtdna/H1c/tree" in out) is (
        mt == "chrM"
    )


def test_mt_haplogroup_vcf_without_mtdna_fails(tmp_path):
    body = "".join(line for line in _KIT_BODY.splitlines(True) if "chrM" not in line)
    vcf = _vcf(tmp_path / "kit.vcf.gz", _HG38, body)
    with pytest.raises(WGSExtractError, match="no variants on chromosome (chrM|MT)"):
        run_wgsextract(
            "lineage", "mt-haplogroup", "--input", vcf,
            "--haplogrep-path", _haplogrep(tmp_path),
        )  # fmt: skip
    assert not (tmp_path / "temp_haplogrep_input.vcf.gz").exists()


def test_mt_haplogroup_haplogrep_failure_cleans_up(tmp_path):
    vcf = _vcf(tmp_path / "kit.vcf.gz", _HG38, _KIT_BODY)
    with pytest.raises(WGSExtractError, match="Haplogrep failed"):
        run_wgsextract(
            "lineage", "mt-haplogroup", "--input", vcf, "--outdir", str(tmp_path / "o"),
            "--haplogrep-path", _haplogrep(tmp_path, code=2),
        )  # fmt: skip
    assert sorted(p.name for p in (tmp_path / "o").iterdir()) == [
        "haplogrep_results.txt"
    ]


def _mito_bam(
    tmp_path: Path, mt_length: int, read_groups: tuple[str, ...] = ()
) -> tuple[str, Path]:
    """A BAM with 12 reads carrying ALT C at chrM:150 (and none elsewhere)."""
    rng = random.Random(3)
    mito = "".join(rng.choice("AGT") for _ in range(mt_length))
    ref = write_fasta(tmp_path / "ref.fa", {"chr1": mito[:500], "chrM": mito})
    header = ["@SQ\tSN:chr1\tLN:500", f"@SQ\tSN:chrM\tLN:{mt_length}"]
    header += [f"@RG\tID:{sample}\tSM:{sample}" for sample in read_groups]
    reads = _reads("chrM", mito, 12, start=120, snp=149) + _reads("chr1", mito, 5)
    return sam_to_bam(header + reads, tmp_path / "kit.bam"), ref


def test_mt_haplogroup_from_bam_calls_haploid_mtdna_variants(tmp_path):
    bam, ref = _mito_bam(tmp_path, 16_569)
    run_wgsextract(
        "lineage", "mt-haplogroup", "--input", bam, "--ref", str(ref),
        "--haplogrep-path", _haplogrep(tmp_path),
    )  # fmt: skip
    (call,) = _calls(tmp_path)
    assert [(*r[:2], r[3], r[4].split(":")[0]) for r in call["records"]] == [
        ("chrM", "150", "C", "1")
    ]
    assert not (tmp_path / "temp_haplogrep_input.vcf.gz").exists()


def test_mt_haplogroup_refuses_or_warns_on_yoruba_mtdna(tmp_path, caplog):
    bam, ref = _mito_bam(tmp_path, 16_571)
    with caplog.at_level(logging.WARNING):
        try:
            run_wgsextract(
                "lineage", "mt-haplogroup", "--input", bam, "--ref", str(ref),
                "--haplogrep-path", _haplogrep(tmp_path),
            )  # fmt: skip
        except WGSExtractError as e:
            assert "Yoruba" in str(e)
            return
    assert "Yoruba" in caplog.text


@pytest.mark.parametrize(
    "result",
    [
        _HAPLOGREP,
        '"SampleID"\t"Haplogroup"\n"kit"\t""\n',
    ],
)
def test_mt_haplogroup_without_a_haplogroup_names_the_reference(
    tmp_path, monkeypatch, result
):
    # Haplogrep writes a header alone when it gets no mtDNA variants, as when
    # the alignment was called against a reference of another build.
    # Without --ref, as the GUI's ancestry page calls it with a library set up
    # that doesn't hold this sample's reference: the FASTA beside the BAM is used.
    from wgsextract_cli.core.config import settings

    (tmp_path / "library").mkdir()
    monkeypatch.setitem(settings, "reference_library", str(tmp_path / "library"))
    bam, _ref = _mito_bam(tmp_path, 16_569)
    with pytest.raises(WGSExtractError, match=r"no mtDNA variants.*ref\.fa"):
        run_wgsextract(
            "lineage", "mt-haplogroup", "--input", bam,
            "--haplogrep-path", _haplogrep(tmp_path, result),
        )  # fmt: skip
    (call,) = _calls(tmp_path)
    assert [r[:2] for r in call["records"]] == [["chrM", "150"]]


def test_mt_haplogroup_calling_failure_stops_before_haplogrep(tmp_path):
    bam, ref = _mito_bam(tmp_path, 16_569)
    Path(f"{bam}.bai").unlink()  # mpileup -r needs the index
    with pytest.raises(WGSExtractError, match="mpileup"):
        run_wgsextract(
            "lineage", "mt-haplogroup", "--input", bam, "--ref", str(ref),
            "--haplogrep-path", _haplogrep(tmp_path),
        )  # fmt: skip
    assert not (tmp_path / "calls.jsonl").exists()


# --- Haplogrep 3 and haplocheck (#361) ---------------------------------------

needs_haplogrep3 = needs_tools("haplogrep3", "java")


def _mt_vcf(path: Path, body: str, af: bool = False) -> str:
    meta = '##FORMAT=<ID=AF,Number=1,Type=Float,Description="level">\n' if af else ""
    return _vcf(path, {"chrM": 16_569}, body, meta)


def _classify(vcf: str, outdir: Path, **params):
    from wgsextract_cli import api

    return api.lineage.mt_haplogroup(
        api.MtHaplogroupParams(input=Path(vcf), outdir=outdir, **params)
    )


@needs_haplogrep3
def test_haplogrep3_classifies_with_the_chosen_tree(tmp_path, mini_tree):
    vcf = _mt_vcf(tmp_path / "kit.vcf.gz", mini_tree.vcf_body([100, 200, 300]))
    for tree in (None, "mitotree-rcrs"):
        result = _classify(vcf, tmp_path / str(tree), tree=tree)
        assert result.metrics["haplogroup"] == "A1"
        assert result.details["tree"]["id"] == (tree or "phylotree-fu-rcrs")
        assert result.details["haplogrep_version"] == "3.2.2"
    _classify(vcf, tmp_path / "again")
    assert len(mini_tree.downloads) == 2  # each tree once, then reused


def test_mt_haplogroup_refuses_haplogrep_2(tmp_path):
    """#361: Haplogrep 2 takes other arguments and an older tree."""
    old = _fake_tool(tmp_path / "haplogrep.py", 'print("HaploGrep 2.4.0")\n')
    vcf = _vcf(tmp_path / "kit.vcf.gz", _HG38, _KIT_BODY)
    with pytest.raises(WGSExtractError, match="not Haplogrep 3"):
        run_wgsextract(
            "lineage", "mt-haplogroup", "--input", vcf, "--haplogrep-path", old,
        )  # fmt: skip


def test_a_modified_tree_is_downloaded_again(tmp_path, mini_tree):
    vcf = _vcf(tmp_path / "kit.vcf.gz", _HG38, _KIT_BODY)
    library = tmp_path / "library"
    library.mkdir()
    argv = ["lineage", "mt-haplogroup", "--input", vcf, "--ref", str(library)]
    run_wgsextract(*argv, "--haplogrep-path", _haplogrep(tmp_path))
    tree_xml = next(library.glob("haplogrep3/trees/phylotree-fu-rcrs/*/tree.xml"))
    original = tree_xml.read_bytes()
    tree_xml.write_text("<phylotree/>")
    run_wgsextract(*argv, "--haplogrep-path", _haplogrep(tmp_path))
    assert tree_xml.read_bytes() == original
    assert len(mini_tree.downloads) == 2


@needs_haplogrep3
@needs_tools("haplocheck")
def test_contamination_check_reports_haplocheck_status(tmp_path, mini_tree):
    body = "".join(
        line.replace("\tGT\t1", "\tGT:AF\t1:1.0")
        for line in mini_tree.vcf_body([100, 200, 300]).splitlines(True)
    )
    vcf = _mt_vcf(tmp_path / "kit.vcf.gz", body, af=True)
    result = _classify(vcf, tmp_path / "o", contamination=True)
    assert result.metrics["mt_contamination"] == "ND"
    roles = {Path(o.path).name: o.role for o in result.outputs}
    expected = {
        "haplogrep_results.txt": "haplogroup_report",
        "haplocheck_contamination.txt": "contamination_report",
        "haplocheck_contamination.raw.txt": "contamination_details",
    }
    warned = {w.code for w in result.warnings}
    # haplocheck 1.3.3 can't load its HTML template on Windows (PR #436 CI).
    if sys.platform == "win32":
        assert "haplocheck_no_html" in warned
    else:
        expected["haplocheck_contamination.html"] = "contamination_html"
        assert "haplocheck_no_html" not in warned
    assert roles == expected
    written = sorted(p.name for p in (tmp_path / "o").iterdir())
    assert written == sorted([*roles, "haplogrep_results.txt.manifest.json"])


@needs_haplogrep3
@needs_tools("haplocheck")
def test_contamination_check_detects_a_mixture(tmp_path):
    # Issue #361: mutserve's calls on NA12878 chrM reads with 5% of HG01112's.
    body = (Path(__file__).parent / "fixtures" / "mt_mixture_5pct.tsv").read_text()
    vcf = _mt_vcf(tmp_path / "mix.vcf.gz", body, af=True)
    result = _classify(vcf, tmp_path / "o", contamination=True)
    assert result.metrics["mt_contamination"] == "YES"
    assert result.metrics["mt_contamination_level"] == pytest.approx(0.05, abs=0.01)
    (warning,) = (w for w in result.warnings if w.code == "mt_contamination")
    assert warning.details["major_haplogroup"].startswith("H13")
    assert warning.details["minor_haplogroup"].startswith("A2")


def test_contamination_check_refuses_reads_of_several_samples(tmp_path):
    two, ref = _mito_bam(tmp_path, 16_569, read_groups=("mother", "child"))
    with pytest.raises(WGSExtractError, match="single-sample.*child, mother"):
        run_wgsextract(
            "lineage", "mt-haplogroup", "--input", two, "--ref", str(ref),
            "--contamination", "--haplogrep-path", _haplogrep(tmp_path),
            "--haplocheck-path", _haplogrep(tmp_path),
        )  # fmt: skip


def test_relative_tool_paths_survive_the_temporary_working_directory(
    tmp_path, monkeypatch
):
    vcf = _vcf(tmp_path / "kit.vcf.gz", _HG38, _KIT_BODY)
    _haplogrep(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = _classify(vcf, tmp_path, haplogrep_path="haplogrep.py")
    assert result.metrics["haplogroup"] == "H1c"
    # The override that ran is the version recorded, not one found by name.
    assert result.provenance.tools["haplogrep3"] == "3.2.2"


def test_contamination_check_needs_heteroplasmy_levels(tmp_path):
    vcf = _vcf(tmp_path / "kit.vcf.gz", _HG38, _KIT_BODY)
    with pytest.raises(WGSExtractError, match="heteroplasmy levels"):
        run_wgsextract(
            "lineage", "mt-haplogroup", "--input", vcf, "--contamination",
            "--haplogrep-path", _haplogrep(tmp_path),
            "--haplocheck-path", _haplogrep(tmp_path),
        )  # fmt: skip


@needs_haplogrep3
@needs_tools("haplocheck")
def test_contamination_check_on_a_bam_calls_heteroplasmies(tmp_path, mini_tree):
    bam, ref = _mito_bam(tmp_path, 16_569)
    from wgsextract_cli import api

    result = api.lineage.mt_haplogroup(
        api.MtHaplogroupParams(
            input=Path(bam), ref=ref, outdir=tmp_path / "o", contamination=True
        )
    )
    # Too few variants to judge; mutserve called the 12-read homoplasmy at 150.
    assert result.metrics["mt_contamination"] == "ND"
    (row,) = csv.DictReader(
        (tmp_path / "o" / "haplocheck_contamination.raw.txt").open(), delimiter="\t"
    )
    assert row["Sample Coverage"] == "12" and row["Overall Homoplasmies"] == "1"
