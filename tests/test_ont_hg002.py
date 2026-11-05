"""ONT end to end on real HG002 R10.4.1 reads (#299).

tests/fixtures/ont_hg002 holds 44 ONT reads (about 27x) trimmed to
chr20:14,542,001-14,554,000 of GRCh38, with their 5mC/5hmC tags, the reference
window and the GIAB truth in it: 18 homozygous small variants and a homozygous
313 bp Alu deletion (provenance.json; rebuilt by scripts/ont_hg002_data.py).
minimap2, samtools and Sniffles run for real; DeepVariant is stood in for.
"""

import gzip
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.fake_tools import python_tool
from tests.htslib_helpers import needs_htslib, needs_tools, run_wgsextract
from wgsextract_cli.core.dependencies import clear_tool_caches, get_tool_path

FIXTURE = Path(__file__).parent / "fixtures" / "ont_hg002"
pytestmark = [needs_htslib, needs_tools("minimap2")]


def _truth(name: str) -> list[list[str]]:
    lines = (FIXTURE / name).read_text().splitlines()
    return [line.split("\t") for line in lines if not line.startswith("#")]


def _query(vcf: Path, fmt: str, *extra: str) -> list[list[str]]:
    out = subprocess.run(
        [get_tool_path("bcftools"), "query", *extra, "-f", fmt, str(vcf)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line.split("\t") for line in out.splitlines()]


@pytest.fixture(scope="module")
def aligned(tmp_path_factory) -> tuple[Path, Path]:
    """(BAM, reference): the fixture reads through `wgsextract align`."""
    tmp = tmp_path_factory.mktemp("ont_hg002")
    ref = tmp / "ref.fa"
    shutil.copy(FIXTURE / "ref.fa", ref)
    out = tmp / "aligned"
    run_wgsextract(
        "align", "--r1", str(FIXTURE / "reads.fastq.gz"), "--ref", str(ref),
        "--outdir", str(out), "--sample", "HG002", "--threads", "2",
    )  # fmt: skip
    (bam,) = out.glob("*.bam")
    return bam, ref


def test_align_detects_ont_and_keeps_reads_and_modification_tags(aligned):
    bam, _ref = aligned
    samtools = get_tool_path("samtools")
    header = subprocess.run(
        [samtools, "view", "-H", str(bam)], capture_output=True, text=True
    ).stdout
    assert any(
        line.startswith("@RG") and "\tPL:ONT" in line and "\tSM:HG002" in line
        for line in header.splitlines()
    )
    # contract: the map-ont preset is what makes these alignments ONT-aware.
    assert "map-ont" in header
    records = subprocess.run(
        [samtools, "view", "-F", "0x900", str(bam)], capture_output=True, text=True
    ).stdout.splitlines()
    with gzip.open(FIXTURE / "reads.fastq.gz", "rt") as fastq:
        names = {line[1:].split("\t")[0] for line in fastq if line.startswith("@")}
    assert {r.split("\t")[0] for r in records} == names
    assert all(int(r.split("\t")[1]) & 4 == 0 for r in records)
    assert all("\tMM:Z:C+h?" in r and "\tML:B:C," in r for r in records)


@needs_tools("sniffles")
def test_sv_on_ont_runs_sniffles_and_finds_the_truth_deletion(aligned, tmp_path):
    bam, ref = aligned
    out = tmp_path / "sv"
    # Whole-genome Sniffles skips contigs under 1 Mb, as this one is.
    run_wgsextract(
        "vcf", "sv", "--input", str(bam), "--ref", str(ref), "--outdir", str(out),
        "--region", "chr20", "--threads", "2",
    )  # fmt: skip
    (truth,) = _truth("truth_sv.vcf")
    pos, length = int(truth[1]), len(truth[3]) - len(truth[4])
    calls = _query(
        out / "sniffles.vcf.gz",
        "%POS\t%INFO/SVTYPE\t%INFO/SVLEN\t[%GT]\n",
        "-i",
        'FILTER="PASS"',
    )
    assert [
        c
        for c in calls
        if c[1] == "DEL"
        and abs(int(c[0]) - pos) <= 50
        and abs(abs(int(c[2])) - length) <= length // 10
        and c[3] == "1/1"
    ], calls


@pytest.fixture
def deepvariant_plan(tmp_path, monkeypatch) -> Path:
    """A stand-in run_deepvariant on PATH that records its arguments and
    prints a run plan; returns the file the arguments go to."""
    calls = tmp_path / "run_deepvariant.json"
    python_tool(
        tmp_path / "bin",
        "run_deepvariant",
        "import json, sys\n"
        f"open({str(calls)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if '--dry_run=true' in sys.argv:\n"
        "    for stage in ('make_examples', 'call_variants', 'postprocess_variants'):\n"
        "        print('***** Running the command:*****')\n"
        "        print(f'time /opt/deepvariant/bin/{stage} --flag')\n"
        "        print()\n",
    )
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    clear_tool_caches()
    yield calls
    clear_tool_caches()


def test_deepvariant_plans_the_ont_model_for_the_aligned_reads(
    aligned, deepvariant_plan, tmp_path
):
    bam, ref = aligned
    run_wgsextract(
        "vcf", "deepvariant", "--input", str(bam), "--ref", str(ref),
        "--outdir", str(tmp_path / "dv"), "--threads", "2", "--dry-run",
    )  # fmt: skip
    plans = [
        json.loads(line)
        for line in deepvariant_plan.read_text().splitlines()
        if "--dry_run=true" in line
    ]
    assert plans
    for argv in plans:
        # contract: R10.4.1 reads need DeepVariant's ONT_R104 model.
        assert argv[argv.index("--model_type") + 1] == "ONT_R104"


@needs_tools("run_clair3.sh")
def test_clair3_calls_exactly_the_truth_small_variants(aligned, tmp_path):
    bam, ref = aligned
    out = tmp_path / "clair3"
    # The reads carry no basecaller metadata; v410 is the bundled R10.4.1 model.
    run_wgsextract(
        "vcf", "clair3", "--input", str(bam), "--ref", str(ref),
        "--outdir", str(out), "--model", "r1041_e82_400bps_sup_v410",
        "--threads", "2",
    )  # fmt: skip
    calls = _query(
        out / "clair3.vcf.gz", "%POS\t%REF\t%ALT\t[%GT]\n", "-i", 'FILTER="PASS"'
    )
    truth = [
        [t[1], t[3], t[4], t[9].replace("|", "/")] for t in _truth("truth_small.vcf")
    ]
    assert calls == truth
