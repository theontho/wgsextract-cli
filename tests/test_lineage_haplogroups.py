"""lineage y-haplogroup / mt-haplogroup end to end with real bcftools/samtools
and stand-in Yleaf/Haplogrep scripts that record what they were handed."""

import gzip
import json
import logging
import random
import textwrap
from pathlib import Path

import pytest

from tests.htslib_helpers import (
    bgzip_index,
    needs_htslib,
    run_wgsextract,
    sam_to_bam,
    write_fasta,
)
from wgsextract_cli.commands._lineage_ydna import _yleaf_build
from wgsextract_cli.core.utils import WGSExtractError

pytestmark = needs_htslib

_PREDICTION = (
    "Sample_name\tHg\tHg_marker\tTotal_reads\tValid_markers\tQC-score\n"
    "kit\tR1b1a1b1a1a2c1\tR-FGC5494\t0\t120\t0.98\n"
)
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


def _yleaf(tmp_path: Path, help_text="", prediction=_PREDICTION, code=0) -> str:
    return _fake_tool(
        tmp_path / "yleaf.py",
        f"""\
        if "--help" in argv:
            print({help_text!r})
            sys.exit(0)
        flag = next(a for a in argv if a in ("-vcf", "-bam", "-cram", "-fastq"))
        given = argv[argv.index(flag) + 1]
        data = os.environ.get("YLEAF_DATA_DIR")
        log(records=records(given) if flag == "-vcf" else None,
            placeholder=bool(data) and Path(data, "hg38", "full_reference.fa").is_file())
        out = Path(argv[argv.index("-o") + 1])
        out.mkdir(parents=True, exist_ok=True)
        if {prediction!r} is not None:
            (out / "hg_prediction.hg").write_text({prediction!r})
        sys.exit({code})
        """,
    )


def _haplogrep(
    tmp_path: Path, result=_HAPLOGREP + '"kit"\t"1-16569"\t"H1c"\t"1"\t"0.93"\n', code=0
) -> str:
    return _fake_tool(
        tmp_path / "haplogrep.py",
        f"""\
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


def test_y_haplogroup_from_vcf_hands_yleaf_only_chry(tmp_path, capsys):
    vcf = _vcf(tmp_path / "kit.vcf.gz", _HG38, _KIT_BODY, "##reference=GRCh38.fa\n")
    pos = tmp_path / "markers.txt"
    pos.write_text("chrY\t2781480\n")
    run_wgsextract(
        "lineage", "y-haplogroup", "--input", vcf, "--outdir", str(tmp_path / "o"),
        "--yleaf-path", _yleaf(tmp_path, "--no-update-check"), "--threads", "2",
        "--pos-file", str(pos), "--extra-args", "-force -dh 5",
    )  # fmt: skip
    (call,) = _calls(tmp_path)
    assert call["records"] == [
        ["chrY", "2781480", "C", "T", "1"],
        ["chrY", "22739367", "G", "A", "1"],
    ]
    argv = call["argv"]
    assert argv[argv.index("-rg") + 1] == "hg38" and argv.count("-force") == 1
    assert argv[argv.index("-o") + 1] == str(tmp_path / "o" / "yleaf")
    assert argv[argv.index("-t") + 1] == "2"
    assert argv[-5:] == ["--no-update-check", "-pos", str(pos), "-dh", "5"]
    assert call["placeholder"]  # never the ~3 GB reference download
    # Yleaf's working copy of the VCF is cleaned up; the user's folder is not.
    assert sorted(p.name for p in (tmp_path / "o").iterdir()) == ["yleaf"]
    assert "R1b1a1b1a1a2c1\tR-FGC5494" in capsys.readouterr().out


def test_y_haplogroup_uses_the_vcf_build(tmp_path):
    hg19 = {"chr1": 249_250_621, "chrY": 59_373_566}
    body = "chrY\t2655180\t.\tG\tA\t50\tPASS\t.\tGT\t1\n"
    vcf = _vcf(tmp_path / "kit.vcf.gz", hg19, body, "##reference=hg19.fa\n")
    run_wgsextract(
        "lineage", "y-haplogroup", "--input", vcf, "--yleaf-path", _yleaf(tmp_path)
    )
    argv = _calls(tmp_path)[0]["argv"]
    assert argv[argv.index("-rg") + 1] == "hg19"
    assert "--no-update-check" not in argv  # this Yleaf doesn't offer it


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


def test_y_haplogroup_vcf_without_chry_fails_before_yleaf(tmp_path):
    body = _KIT_BODY.replace("chrY", "chrX")
    vcf = _vcf(tmp_path / "f.vcf.gz", _HG38, body, "##reference=GRCh38.fa\n")
    with pytest.raises(WGSExtractError, match="no variants on chromosome (chr)?Y"):
        run_wgsextract(
            "lineage", "y-haplogroup", "--input", vcf, "--yleaf-path", _yleaf(tmp_path)
        )
    assert not (tmp_path / "calls.jsonl").exists()


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"code": 3}, "Yleaf failed"),
        ({"prediction": None}, "did not write"),
        ({"prediction": "Sample_name\tHg\nkit\tNA\n"}, "could not predict"),
    ],
)
def test_y_haplogroup_reports_yleaf_failures(tmp_path, kwargs, message):
    vcf = _vcf(tmp_path / "kit.vcf.gz", _HG38, _KIT_BODY, "##reference=GRCh38.fa\n")
    with pytest.raises(WGSExtractError, match=message):
        run_wgsextract(
            "lineage", "y-haplogroup", "--input", vcf,
            "--yleaf-path", _yleaf(tmp_path, **kwargs),
        )  # fmt: skip


def test_y_haplogroup_never_reports_a_previous_runs_prediction(tmp_path):
    vcf = _vcf(tmp_path / "kit.vcf.gz", _HG38, _KIT_BODY, "##reference=GRCh38.fa\n")
    argv = ["lineage", "y-haplogroup", "--input", vcf, "--yleaf-path"]
    run_wgsextract(*argv, _yleaf(tmp_path))
    with pytest.raises(WGSExtractError, match="did not write"):
        run_wgsextract(*argv, _yleaf(tmp_path, prediction=None))


def test_y_haplogroup_fastq_is_aligned_by_yleaf_with_our_reference(tmp_path, caplog):
    ref = write_fasta(tmp_path / "ref.fa", {"chrY": "ACGT" * 100})
    fq = tmp_path / "kit.fastq.gz"
    fq.write_bytes(gzip.compress(b"@r1\nACGT\n+\nIIII\n"))
    argv = ["lineage", "y-haplogroup", "--input", str(fq), "--ref", str(ref)]
    run_wgsextract(*argv, "--yleaf-path", _yleaf(tmp_path, "--ref-fasta"))
    call = _calls(tmp_path)[0]
    assert call["argv"][:2] == ["-fastq", str(fq)]
    assert call["argv"][call["argv"].index("--ref-fasta") + 1] == str(ref)
    assert call["env"] is None  # Yleaf needs its real reference to align

    with caplog.at_level(logging.WARNING):
        run_wgsextract(*argv, "--yleaf-path", _yleaf(tmp_path))
    assert "--ref-fasta" not in _calls(tmp_path)[1]["argv"]
    assert "No uncompressed reference FASTA" in caplog.text


def _reads(chrom: str, genome: str, n: int, start: int = 0, snp: int | None = None):
    """``n`` 50 bp reads at ``start``, with ALT C at 0-based ``snp`` if given."""
    seq = genome[start : start + 50]
    if snp is not None:
        seq = seq[: snp - start] + "C" + seq[snp - start + 1 :]
    return [
        f"{chrom}_{i}\t0\t{chrom}\t{start + 1}\t60\t50M\t*\t0\t0\t{seq}\t{'I' * 50}"
        for i in range(n)
    ]


def test_y_haplogroup_from_cram_decodes_with_the_reference(tmp_path):
    rng = random.Random(7)
    genome = "".join(rng.choice("AG") for _ in range(400))
    ref = write_fasta(tmp_path / "ref.fa", {"chrX": genome, "chrY": genome})
    header = ["@SQ\tSN:chrX\tLN:400", "@SQ\tSN:chrY\tLN:400"]
    bam = sam_to_bam(header + _reads("chrY", genome, 20), tmp_path / "kit.bam")
    run_wgsextract(
        "lineage", "y-haplogroup", "--input", bam, "--ref", str(ref),
        "--yleaf-path", _yleaf(tmp_path),
    )  # fmt: skip
    argv = _calls(tmp_path)[0]["argv"]
    assert argv[:2] == ["-bam", bam] and "-cr" not in argv

    from wgsextract_cli.core.utils import run_command

    cram = str(tmp_path / "kit.cram")
    run_command(["samtools", "view", "-C", "-T", str(ref), "-o", cram, bam])
    run_command(["samtools", "index", cram])
    run_wgsextract(
        "lineage", "y-haplogroup", "--input", cram, "--ref", str(ref),
        "--yleaf-path", _yleaf(tmp_path),
    )  # fmt: skip
    argv = _calls(tmp_path)[1]["argv"]
    assert argv[:2] == ["-cram", cram]
    assert argv[argv.index("-cr") + 1] == str(ref)


def test_y_haplogroup_rejects_unsupported_input(tmp_path):
    sam = tmp_path / "kit.sam"
    sam.write_text("@SQ\tSN:chrY\tLN:10\n")
    with pytest.raises(WGSExtractError, match="Unsupported Yleaf input type"):
        run_wgsextract(
            "lineage", "y-haplogroup", "--input", str(sam),
            "--yleaf-path", _yleaf(tmp_path),
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
    tmp_path, meta, contigs, expected
):
    y = "chrY" if "chrY" in contigs else "Y"
    vcf = _vcf(
        tmp_path / "kit.vcf.gz",
        contigs,
        f"{y}\t100\t.\tA\tG\t50\tPASS\t.\tGT\t1\n",
        meta,
    )
    run_wgsextract(
        "lineage", "y-haplogroup", "--input", vcf, "--yleaf-path", _yleaf(tmp_path)
    )
    argv = _calls(tmp_path)[0]["argv"]
    assert argv[argv.index("-rg") + 1] == expected


# --- mt-haplogroup -----------------------------------------------------------


@pytest.mark.parametrize("mt", ["chrM", "MT"])
def test_mt_haplogroup_from_vcf_hands_haplogrep_only_mtdna(tmp_path, capsys, mt):
    contigs = {(mt if c == "chrM" else c): n for c, n in _HG38.items()}
    vcf = _vcf(tmp_path / "kit.vcf.gz", contigs, _KIT_BODY.replace("chrM", mt))
    run_wgsextract(
        "lineage", "mt-haplogroup", "--input", vcf, "--outdir", str(tmp_path / "o"),
        "--haplogrep-path", _haplogrep(tmp_path),
    )  # fmt: skip
    (call,) = _calls(tmp_path)
    assert call["argv"][:3] == ["classify", "--format", "vcf"]
    assert call["records"] == [[mt, "73", "A", "G", "1"], [mt, "263", "A", "G", "1"]]
    assert sorted(p.name for p in (tmp_path / "o").iterdir()) == [
        "haplogrep_results.txt"
    ]
    out = capsys.readouterr().out
    assert "Haplogroup      : H1c" in out and "Quality         : 0.93" in out


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


def _mito_bam(tmp_path: Path, mt_length: int) -> tuple[str, Path]:
    """A BAM with 12 reads carrying ALT C at chrM:150 (and none elsewhere)."""
    rng = random.Random(3)
    mito = "".join(rng.choice("AGT") for _ in range(mt_length))
    ref = write_fasta(tmp_path / "ref.fa", {"chr1": mito[:500], "chrM": mito})
    header = ["@SQ\tSN:chr1\tLN:500", f"@SQ\tSN:chrM\tLN:{mt_length}"]
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
