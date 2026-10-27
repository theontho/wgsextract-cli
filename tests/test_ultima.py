"""Ultima Genomics reads: detection, flow-tag orientation and the tooling chosen."""

import subprocess
from argparse import Namespace

import pytest

from tests.htslib_helpers import needs_tools, sam_to_bam
from wgsextract_cli.commands import align
from wgsextract_cli.commands._bam_ubam import read_group_from_name
from wgsextract_cli.commands.vcf import deepvariant
from wgsextract_cli.core import ultima
from wgsextract_cli.core.utils import WGSExtractError

_RG = "@RG\tID:Z0138\tFO:TGCATGCA\tPL:ULTIMA\tSM:HG008\tmc:20\ttp:reference"
_SEQ = "ACGTTGCAAC"


def _record(flag: int, name: str = "038266_1-Z0138-0510185343") -> str:
    return (
        f"{name}\t{flag}\tchr1\t11\t60\t10M\t*\t0\t0\t{_SEQ}\tABCDEFGHIJ"
        "\ttp:B:c,1,0,-1,0,0,0,0,0,1,2\tt0:Z:0123456789\ttm:Z:A\tRG:Z:Z0138"
    )


def _tag(record: str, name: str) -> str:
    return next(f for f in record.split("\t") if f.startswith(name + ":"))


def test_reverse_strand_flow_tags_are_reversed():
    flipped = ultima.reverse_flow_tags((_record(16) + "\n").encode()).decode()
    assert _tag(flipped, "tp") == "tp:B:c,2,1,0,0,0,0,0,-1,0,1"
    assert _tag(flipped, "t0") == "t0:Z:9876543210"
    assert flipped.endswith("\n")
    assert ultima.reverse_flow_tags(flipped.encode()).decode() == _record(16) + "\n"


@pytest.mark.parametrize("line", [_record(0), _record(256), _RG, "@HD\tVN:1.6"])
def test_forward_reads_and_headers_pass_through(line):
    assert ultima.reverse_flow_tags(line.encode()) == line.encode()


def test_flow_tag_filter_runs_as_a_pipeline_stage():
    sam = "\n".join([_RG, _record(16), _record(0)]) + "\n"
    out = subprocess.run(
        ultima.flow_tag_filter(), input=sam.encode(), capture_output=True, check=True
    ).stdout.decode()
    lines = out.splitlines()
    assert _tag(lines[1], "t0") == "t0:Z:9876543210"
    assert lines[2] == _record(0)


def test_read_oriented_flow_tags_are_left_alone():
    read_rg = _RG.replace("tp:reference", "tp:read")
    other = "@RG\tID:B\tPL:ULTIMA\ttp:reference"
    sam = "\n".join([read_rg, other, _record(16),
                     _record(16).replace("RG:Z:Z0138", "RG:Z:B")]) + "\n"  # fmt: skip
    out = subprocess.run(
        ultima.flow_tag_filter(), input=sam.encode(), capture_output=True, check=True
    ).stdout.decode()
    lines = out.splitlines()
    assert lines[2] == _record(16)
    assert _tag(lines[3], "t0") == "t0:Z:9876543210"


def test_read_group_platform_and_flow_order():
    header = f"@HD\tVN:1.6\n{_RG}\n"
    assert ultima.is_ultima_header(header)
    assert ultima.has_flow_order(header)
    illumina = "@RG\tID:a\tPL:ILLUMINA\tFO:TGCA\n"
    assert not ultima.is_ultima_header(illumina)
    assert not ultima.has_flow_order("@RG\tID:a\tPL:ULTIMA\n")


def test_ultima_read_names_are_recognized():
    assert read_group_from_name("038266_1-Z0138-0510185343")[1] == "ULTIMA"


def test_fastq_flow_tags_are_found_in_the_comment(tmp_path):
    tagged = tmp_path / "t.fastq"
    tagged.write_text(f"@r1\ttp:B:c,0,1\tt0:Z:DD\n{_SEQ}\n+\nABCDEFGHIJ\n")
    plain = tmp_path / "p.fastq"
    plain.write_text(f"@SRR30784715.1 1/1\n{_SEQ}\n+\nABCDEFGHIJ\n")
    assert ultima.fastq_has_flow_tags(tagged)
    spaced = tmp_path / "s.fastq"
    spaced.write_text(f"@r1 tp:B:c,0,1\tt0:Z:DD\n{_SEQ}\n+\nABCDEFGHIJ\n")
    assert ultima.fastq_has_flow_tags(spaced)
    assert not ultima.fastq_has_flow_tags(plain)


def test_realigned_read_groups_keep_flow_order_and_set_orientation():
    header = "@RG\tID:Z0138\tFO:TGCA\tPL:ULTIMA\tSM:HG008\tmc:20\ttp:read\n"
    (line,) = align.ultima_read_groups(header, Namespace(sample=None), "x")
    fields = dict(f.split(":", 1) for f in line.split("\t")[1:])
    assert fields == {"ID": "Z0138", "FO": "TGCA", "PL": "ULTIMA", "SM": "HG008",
                      "mc": "20", "tp": "reference"}  # fmt: skip
    (named,) = align.ultima_read_groups(header, Namespace(sample="me"), "x")
    assert "\tSM:me\t" in named


def _align_args(**overrides):
    values = {"platform": "ultima", "aligner": "auto", "long_read": False,
              "preset": None, "gpu": False, "r2": None}  # fmt: skip
    return Namespace(**{**values, **overrides})


def test_ultima_aligns_with_bwa():
    assert align.select_aligner(_align_args()) == "bwa"
    assert align.select_aligner(_align_args(aligner="bwa-mem2")) == "bwa-mem2"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"aligner": "minimap2"}, "BWA-MEM"),
        ({"gpu": True}, "flow tags"),
        ({"r2": "r2.fq"}, "single-end"),
    ],
)
def test_ultima_refuses_what_would_lose_its_flow_data(overrides, message):
    with pytest.raises(WGSExtractError, match=message):
        align.select_aligner(_align_args(**overrides))


@needs_tools("samtools")
def test_ultima_bam_input_is_detected(tmp_path):
    bam = sam_to_bam(["@SQ\tSN:chr1\tLN:1000", _RG, _record(0)], tmp_path / "ug.bam")
    assert align.resolve_platform(Namespace(platform=None), bam) == "ultima"
    assert align.resolve_platform(Namespace(platform="illumina"), bam) == "illumina"
    assert ultima.bcftools_options(bam) == (["-X", "ultima"], [])
    data = ultima.flow_data(bam, f"{_RG}\n")
    assert data.flow_aware


@needs_tools("samtools")
def test_ultima_reads_without_flow_tags_are_not_flow_aware(tmp_path):
    record = _record(0).split("\ttp:")[0] + "\tRG:Z:Z0138"
    bam = sam_to_bam(["@SQ\tSN:chr1\tLN:1000", _RG, record], tmp_path / "ug.bam")
    assert not ultima.flow_data(bam, f"{_RG}\n").flow_aware


def test_deepvariant_picks_the_ultima_model_and_image(monkeypatch):
    monkeypatch.setattr(
        ultima, "alignment_flow_data", lambda *_a: ultima.FlowData(True, True, True)
    )
    args = Namespace(input="ug.bam", pacbio=False, model_type=None, wes=False)
    assert deepvariant._model_type(args, "ref.fa") == "ULTIMA"
    assert deepvariant._model_type(Namespace(**{**vars(args), "wes": True}),
                                   "ref.fa") == "WES"  # fmt: skip
    assert deepvariant.deepvariant_image("ULTIMA") == "google/deepvariant:gh1100-ultima"


def _samtools_reads(path) -> list[str]:
    from wgsextract_cli.core.dependencies import get_tool_path

    return subprocess.run(
        [get_tool_path("samtools"), "view", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()  # fmt: skip


@needs_tools("samtools")
def test_to_ubam_keeps_flow_tags_in_read_orientation(tmp_path):
    from tests.htslib_helpers import run_wgsextract

    bam = sam_to_bam(["@SQ\tSN:chr1\tLN:1000", _RG, _record(16)], tmp_path / "ug.bam")
    run_wgsextract("bam", "to-ubam", "--input", bam, "--outdir", str(tmp_path / "o"))
    (read,) = _samtools_reads(tmp_path / "o" / "ug_unaligned.bam")
    assert read.split("\t")[1] == "4"
    assert _tag(read, "tp") == "tp:B:c,2,1,0,0,0,0,0,-1,0,1"
    assert _tag(read, "t0") == "t0:Z:9876543210" and "\ttm:Z:A" in read


_GENOME = (
    "GATTACAGGCTTAACCGTAGCTAGGCATCGATCGTTAGCCTAGGATCCAGTCGATCGGACTTAGCAATGCGTAC"
    "CGTAGGCTAAGTCGATTGCACGTTAGCCATGGCTAGCTTAGGACTGACGTCAGTGCATCGATGCTAGCATCGAG"
)


@needs_tools("samtools", "bwa")
def test_align_ultima_bam_carries_flow_tags_through_bwa(tmp_path):
    """Realigning an Ultima BAM: a read the aligner turns around gets its tp/t0
    reversed, its read group (flow order) is kept, and it lands where it came from."""
    from tests.htslib_helpers import run_wgsextract, write_fasta

    ref = tmp_path / "ref.fa"
    write_fasta(ref, {"chr1": _GENOME * 4})
    comp = str.maketrans("ACGT", "TGCA")
    seq = _GENOME[20:120]
    tp = ",".join(str(i % 3 - 1) for i in range(len(seq)))
    t0 = "".join(chr(33 + i % 40) for i in range(len(seq)))
    # Stored as sequenced (unaligned, forward): the read is the reverse strand.
    read = (f"r1\t4\t*\t0\t0\t*\t*\t0\t0\t{seq.translate(comp)[::-1]}\t{'I' * 100}"
            f"\ttp:B:c,{tp}\tt0:Z:{t0}\tRG:Z:Z0138")  # fmt: skip
    bam = sam_to_bam([_RG, read], tmp_path / "ug.bam")
    run_wgsextract("align", "--r1", bam, "--ref", str(ref), "--outdir",
                   str(tmp_path / "o"))  # fmt: skip
    (out,) = [r for r in _samtools_reads(tmp_path / "o" / "ug_aligned.bam")
              if not int(r.split("\t")[1]) & 0x900]  # fmt: skip
    fields = out.split("\t")
    assert int(fields[1]) & 0x10 and fields[9] == seq
    assert _tag(out, "tp") == f"tp:B:c,{','.join(reversed(tp.split(',')))}"
    assert _tag(out, "t0") == f"t0:Z:{t0[::-1]}"
    from wgsextract_cli.core.alignment_metadata import get_bam_header

    header = get_bam_header(tmp_path / "o" / "ug_aligned.bam")
    assert "FO:TGCATGCA" in header and "tp:reference" in header
