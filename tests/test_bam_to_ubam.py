"""bam to-ubam: unaligned BAMs from BAM/CRAM/FASTQ read back with samtools."""

import gzip
import logging
import subprocess
from pathlib import Path

import pytest

from tests.htslib_helpers import needs_tools, run_wgsextract, write_fasta
from wgsextract_cli.commands import _bam_ubam
from wgsextract_cli.commands._bam_ubam import read_group_from_name
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.utils import WGSExtractError

pytestmark = needs_tools("samtools")

_GENOME = "ACGTTGCAACGGATCCTTAG" * 10  # 200 bp
_COMP = str.maketrans("ACGT", "TGCA")
_NAME = "A00123:8:HABCDEFXX:2:1101:{}:2000"


def _samtools(*args: str, stdin: str | None = None) -> str:
    return subprocess.run(
        [get_tool_path("samtools"), *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _aligned(path: Path, rg: bool = False, ref=None) -> str:
    """Two aligned pairs and an unmapped read, with alignment-only tags, a soft
    clip and an original-quality (OQ) tag to keep."""
    q1, q2 = "ABCDEFGHIJ", "abcdefghij"
    tag = "\tRG:Z:lane1" if rg else ""
    records = [
        f"{_NAME.format(2)}\t99\tchr1\t11\t60\t2S8M\t=\t51\t50\t{_GENOME[8:18]}\t{q1}"
        f"\tNM:i:0\tMD:Z:8\tAS:i:8\tOQ:Z:{q2}{tag}",
        f"{_NAME.format(2)}\t147\tchr1\t51\t60\t10M\t=\t11\t-50\t{_GENOME[50:60]}\t{q2}"
        f"\tNM:i:0\tXS:i:3{tag}",
        f"{_NAME.format(1)}\t83\tchr1\t101\t60\t10M\t=\t61\t-50\t{_GENOME[100:110]}"
        f"\t{q1}\tSA:Z:chr1,5,+,10M,60,0;{tag}",
        f"{_NAME.format(1)}\t163\tchr1\t61\t60\t10M\t=\t101\t50\t{_GENOME[60:70]}"
        f"\t{q2}{tag}",
        f"{_NAME.format(1)}\t2115\tchr1\t5\t60\t10M\t=\t61\t0\t{_GENOME[4:14]}"
        f"\t{q1}{tag}",  # supplementary: must not become a second read
        f"{_NAME.format(3)}\t4\t*\t0\t0\t*\t*\t0\t0\tGGGGCCCCAA\t{q1}{tag}",
    ]
    header = (
        "@HD\tVN:1.6\tSO:coordinate\n@SQ\tSN:chr1\tLN:200\n"
        + ("@RG\tID:lane1\tSM:NA1\tPL:ILLUMINA\n" if rg else "")
        + "@PG\tID:bwa\tPN:bwa\tCL:bwa mem\n@PG\tID:samtools\tPP:bwa\tPN:samtools\n"
    )
    sam = header + "".join(r + "\n" for r in records)
    unsorted = path.with_suffix(".unsorted.sam")
    unsorted.write_text(sam)
    ref_opt = ["--reference", str(ref)] if ref else []
    out_fmt = path.suffix[1:]
    _samtools("sort", "-O", out_fmt, *ref_opt, "-o", str(path), str(unsorted))
    return str(path)


def _reads(bam: Path) -> list[list[str]]:
    return [line.split("\t") for line in _samtools("view", str(bam)).splitlines()]


def _expected_reads(rg: str) -> list[list[str]]:
    """The reads as sequenced: name order, reverse strands restored, OQ/RG kept."""
    rev = _GENOME[100:110].translate(_COMP)[::-1]
    rev2 = _GENOME[50:60].translate(_COMP)[::-1]
    unmapped = ["*", "0", "0", "*", "*", "0", "0"]
    return [
        [_NAME.format(1), "77", *unmapped, rev, "JIHGFEDCBA", f"RG:Z:{rg}"],
        [_NAME.format(1), "141", *unmapped, _GENOME[60:70], "abcdefghij", f"RG:Z:{rg}"],
        [_NAME.format(2), "77", *unmapped, _GENOME[8:18], "ABCDEFGHIJ",
         "OQ:Z:abcdefghij", f"RG:Z:{rg}"],
        [_NAME.format(2), "141", *unmapped, rev2, "jihgfedcba", f"RG:Z:{rg}"],
        [_NAME.format(3), "4", *unmapped, "GGGGCCCCAA", "ABCDEFGHIJ", f"RG:Z:{rg}"],
    ]  # fmt: skip


def _sorted_tags(reads):
    return [r[:11] + sorted(r[11:]) for r in reads]


def test_bam_without_read_group_gets_one_from_read_names(tmp_path):
    bam = _aligned(tmp_path / "NA1.bam")
    run_wgsextract("bam", "to-ubam", "--input", bam, "--outdir", str(tmp_path / "o"))
    out = tmp_path / "o" / "NA1_unaligned.bam"
    header = _samtools("view", "-H", str(out))
    assert "@RG\tID:HABCDEFXX.2\tSM:NA1\tLB:lib1\tPU:HABCDEFXX.2\tPL:ILLUMINA" in header
    assert "@SQ" not in header and "ID:bwa" not in header and "SO:queryname" in header
    expected = _expected_reads("HABCDEFXX.2")
    assert _sorted_tags(_reads(out)) == _sorted_tags(expected)


def test_existing_read_groups_win_over_sample_and_platform(tmp_path, caplog):
    bam = _aligned(tmp_path / "NA1.bam", rg=True)
    with caplog.at_level(logging.WARNING):
        run_wgsextract(
            "bam", "to-ubam", "--input", bam, "--sample", "other", "--platform", "ONT"
        )
    out = tmp_path / "NA1_unaligned.bam"
    assert "@RG\tID:lane1\tSM:NA1\tPL:ILLUMINA" in _samtools("view", "-H", str(out))
    assert _sorted_tags(_reads(out)) == _sorted_tags(_expected_reads("lane1"))
    assert "--sample/--platform are ignored" in caplog.text


def test_cram_input_needs_and_uses_its_reference(tmp_path):
    ref = write_fasta(tmp_path / "ref.fa", {"chr1": _GENOME})
    (tmp_path / "kit").mkdir()
    cram = _aligned(tmp_path / "kit" / "s.cram", ref=ref)
    run_wgsextract(
        "bam", "to-ubam", "--input", cram, "--ref", str(ref), "--sample", "S9",
        "--library", "L7",
    )  # fmt: skip
    out = tmp_path / "kit" / "s_unaligned.bam"
    header = _samtools("view", "-H", str(out))
    assert "@RG\tID:HABCDEFXX.2\tSM:S9\tLB:L7\tPU:HABCDEFXX.2\tPL:ILLUMINA" in header
    assert _sorted_tags(_reads(out)) == _sorted_tags(_expected_reads("HABCDEFXX.2"))
    with pytest.raises(WGSExtractError, match="--ref is required"):
        run_wgsextract(
            "bam", "to-ubam", "--input", cram, "--ref", str(tmp_path / "none.fa"),
            "--outdir", str(tmp_path / "o2"),
        )  # fmt: skip


def test_single_end_gzipped_fastq_with_platform_override(tmp_path):
    fq = tmp_path / "HG002.read1.fastq.gz"
    with gzip.open(fq, "wt") as f:
        f.write("@m1\nACGTA\n+\nIIIII\n@m0\nTTGCA\n+\n!!!!!\n")
    run_wgsextract("bam", "to-ubam", "--r1", str(fq), "--platform", "PACBIO")
    out = tmp_path / "HG002_unaligned.bam"
    assert "@RG\tID:HG002\tSM:HG002\tLB:lib1\tPL:PACBIO" in _samtools(
        "view", "-H", str(out)
    )
    assert [r[:2] + r[9:] for r in _reads(out)] == [
        ["m0", "4", "TTGCA", "!!!!!", "RG:Z:HG002"],
        ["m1", "4", "ACGTA", "IIIII", "RG:Z:HG002"],
    ]


def test_undetectable_platform_warns(tmp_path, caplog):
    fq = tmp_path / "x.fq"
    fq.write_text("@read7\nACGT\n+\nIIII\n")
    with caplog.at_level(logging.WARNING):
        run_wgsextract("bam", "to-ubam", "--r1", str(fq))
    assert "pass --platform" in caplog.text
    assert "@RG\tID:x\tSM:x\tLB:lib1\n" in _samtools(
        "view", "-H", str(tmp_path / "x_unaligned.bam")
    )


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--input", "{bam}", "--r1", "{fq}"], "either --input"),
        ([], "either --input"),
        (["--input", "{bam}", "--r2", "{fq}"], "--r2 needs --r1"),
        (["--input", "{fq}"], "must be a BAM or CRAM"),
        (["--r1", "{bam}"], "Could not read FASTQ"),
        (["--r1", "{sam}"], "does not look like a FASTQ"),
        (["--r1", "{fq}", "--r2", "{missing}"], "--r2"),
    ],
)
def test_bad_inputs_are_rejected(tmp_path, argv, message):
    paths = {
        "bam": tmp_path / "a.bam",
        "fq": tmp_path / "a.fq",
        "missing": tmp_path / "m.fq",
    }
    paths["sam"] = tmp_path / "a.sam"
    paths["sam"].write_text("r\t4\t*\t0\t0\t*\t*\t0\t0\tA\tI\n")
    paths["bam"].write_bytes(gzip.compress(b"BAM\1"))
    paths["fq"].write_text("@r\nA\n+\nI\n")
    argv = [a.format(**paths) for a in argv]
    with pytest.raises(WGSExtractError, match=message):
        run_wgsextract("bam", "to-ubam", *argv)


def test_insufficient_space_and_failed_pipeline_leave_no_output(tmp_path, monkeypatch):
    fq = tmp_path / "x.fq"
    fq.write_text("@read7\nACGT\n+\nIIII\n")
    monkeypatch.setattr(_bam_ubam, "check_free_space", lambda *_: False)
    with pytest.raises(WGSExtractError, match="Insufficient free disk space"):
        run_wgsextract("bam", "to-ubam", "--r1", str(fq))
    monkeypatch.setattr(_bam_ubam, "check_free_space", lambda *_: True)

    def broken(stages, description=""):
        Path(stages[-1][stages[-1].index("-o") + 1]).write_text("partial")
        raise subprocess.CalledProcessError(1, stages[0])

    monkeypatch.setattr(_bam_ubam, "run_pipeline", broken)
    with pytest.raises(WGSExtractError, match="Unaligned BAM creation failed"):
        run_wgsextract("bam", "to-ubam", "--r1", str(fq))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["x.fq"]


def test_pre_casava_illumina_name_gets_no_bogus_platform_unit():
    assert read_group_from_name("HWI-ST1234:8:1101:1000:2000#0/1")[0] == "HWI-ST1234.8"
    assert read_group_from_name("A00123:8:HABCDEFXX:2:1101")[0] is None


def test_uppercase_fastq_extension_is_stripped(tmp_path):
    fq = tmp_path / "NA1_R1.FASTQ.GZ"
    fq.write_bytes(gzip.compress(b"@read7\nACGT\n+\nIIII\n"))
    run_wgsextract("bam", "to-ubam", "--r1", str(fq))
    assert (tmp_path / "NA1_unaligned.bam").exists()


def test_bcl2fastq_read_one_suffix_is_not_the_sample_name(tmp_path):
    r1 = tmp_path / "Sample_S1_L001_R1_001.fastq"
    r2 = tmp_path / "Sample_S1_L001_R2_001.fastq"
    r1.write_text(f"@{_NAME.format(1)} 1:N:0:1\nACGT\n+\nIIII\n")
    r2.write_text(f"@{_NAME.format(1)} 2:N:0:1\nACGT\n+\nIIII\n")
    run_wgsextract("bam", "to-ubam", "--r1", str(r1), "--r2", str(r2))
    (out,) = tmp_path.glob("*_unaligned.bam")
    assert "R1" not in out.name
    (rg,) = [
        h for h in _samtools("view", "-H", str(out)).splitlines() if h[:3] == "@RG"
    ]
    assert "SM:Sample_S1_L001\t" in rg


def test_single_end_fastq_with_slash_one_names_stays_unpaired(tmp_path):
    fq = tmp_path / "se.fq"
    fq.write_text("@HWI-ST1234:8:1101:1000:2000#0/1\nACGT\n+\nIIII\n")
    run_wgsextract("bam", "to-ubam", "--r1", str(fq), "--platform", "ILLUMINA")
    assert [r[1] for r in _reads(tmp_path / "se_unaligned.bam")] == ["4"]
