"""`vcf sv` (Delly, pbsv, Sniffles) and `vcf cnv` (Delly): Delly runs for real
on a tiny genome with a 2 kb deletion; pbsv and Sniffles are stood in for."""

import json
import logging
import os
import random
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.fake_tools import only_tools, python_tool, require_bare_name_fakes
from tests.htslib_helpers import (
    needs_htslib,
    needs_tools,
    run_wgsextract,
    sam_to_bam,
    write_fasta,
)
from wgsextract_cli import api
from wgsextract_cli.api import VcfSvParams
from wgsextract_cli.commands.vcf import structural
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.utils import WGSExtractError, run_command

needs_delly = needs_tools("delly", "bcftools", "bgzip", "tabix", "samtools")

GENOME = 60_000
DELETION = (30_000, 32_000)  # 0-based, half-open: missing from the sample


def _sample(tmp_path: Path, deletion: bool = True) -> tuple[str, str]:
    """(BAM, reference): ~10,000 read pairs over chr1 of a sample missing
    DELETION; the BAM also lists a decoy contig the reference lacks, with one
    read whose mate is there."""
    rng = random.Random(1)
    seq = "".join(rng.choice("ACGT") for _ in range(GENOME))
    ref = write_fasta(tmp_path / "ref.fa", {"chr1": seq})
    start, end = DELETION if deletion else (GENOME, GENOME)
    sample = seq[:start] + seq[end:]

    def ref_pos(pos: int) -> int:  # 1-based reference position of a sample one
        return pos + 1 if pos < start else pos + 1 + end - start

    lines = [
        "@HD\tVN:1.6\tSO:coordinate",
        f"@SQ\tSN:chr1\tLN:{GENOME}",
        "@SQ\tSN:chrUn_decoy\tLN:1000",
        "@RG\tID:1\tSM:S\tPL:ILLUMINA",
    ]
    qual = "I" * 100
    for i, a in enumerate(range(0, len(sample) - 500, 6)):
        b = a + rng.randint(280, 320) - 100
        if a < start < a + 100 or b < start < b + 100:
            continue  # no split reads: the pairs' spans show the deletion
        r1, r2 = ref_pos(a), ref_pos(b)
        tlen = r2 + 100 - r1
        lines += [
            f"p{i}\t99\tchr1\t{r1}\t60\t100M\t=\t{r2}\t{tlen}\t{sample[a : a + 100]}"
            f"\t{qual}\tRG:Z:1",
            f"p{i}\t147\tchr1\t{r2}\t60\t100M\t=\t{r1}\t{-tlen}\t"
            f"{sample[b : b + 100]}\t{qual}\tRG:Z:1",
        ]
    lines.append(
        f"d0\t65\tchr1\t5001\t60\t100M\tchrUn_decoy\t100\t0\t{seq[5000:5100]}\t{qual}"
        "\tRG:Z:1"
    )
    return sam_to_bam(lines, tmp_path / "s.bam"), str(ref)


def _records(vcf: Path, region: str | None = None) -> list[tuple[str, ...]]:
    query = [get_tool_path("bcftools"), "query", "-f", "%CHROM %POS %INFO/END\n"]
    query += ["-r", region] if region else []
    out = subprocess.run(
        [*query, str(vcf)], capture_output=True, text=True, check=True
    ).stdout
    return [tuple(line.split()) for line in out.splitlines()]


def _near_deletion(records: list[tuple[str, ...]]) -> bool:
    return any(
        c == "chr1"
        and abs(int(p) - DELETION[0]) < 300
        and abs(int(e) - DELETION[1]) < 300
        for c, p, e in records
    )


def _run(command: str, bam: str, ref: str, outdir: Path, *extra: str) -> None:
    run_wgsextract(
        "vcf", command, "--input", bam, "--ref", ref, "--outdir", str(outdir),
        "--threads", "1", *extra,
    )  # fmt: skip


# --- Delly ----------------------------------------------------------------------


@needs_delly
@pytest.mark.parametrize("region", [None, "chr1:20000-40000"])
def test_delly_sv_finds_the_deletion(tmp_path, region):
    bam, ref = _sample(tmp_path)
    out = tmp_path / "out"
    _run("sv", bam, ref, out, *(["--region", region] if region else []))
    records = _records(out / "sv.vcf.gz")
    assert len(records) == 1 and _near_deletion(records)
    # The BCF and any region BAM are gone.
    assert sorted(p.name for p in out.iterdir()) == ["sv.vcf.gz", "sv.vcf.gz.tbi"]


@needs_delly
def test_delly_sv_on_a_cram_region(tmp_path):
    bam, ref = _sample(tmp_path)
    cram = str(tmp_path / "s.cram")
    samtools = get_tool_path("samtools")
    run_command([samtools, "view", "-C", "-T", ref, "-o", cram, bam])
    run_command([samtools, "index", cram])
    _run("sv", cram, ref, tmp_path / "out", "--region", "chr1")
    assert _near_deletion(_records(tmp_path / "out" / "sv.vcf.gz"))


@needs_delly
def test_no_svs_warn_only_for_a_whole_genome(tmp_path, caplog):
    bam, ref = _sample(tmp_path, deletion=False)
    with caplog.at_level(logging.WARNING):
        _run("sv", bam, ref, tmp_path / "region", "--region", "chr1:1-20000")
    assert "No variants were called" not in caplog.text
    with caplog.at_level(logging.WARNING):
        _run("sv", bam, ref, tmp_path / "whole")
    assert "No variants were called" in caplog.text
    assert _records(tmp_path / "whole" / "sv.vcf.gz") == []


@needs_delly
def test_api_sv_and_cnv_report_their_calls(tmp_path):
    # #346: the callers run through the typed API; the Result names the
    # output's role and the SV caller chosen.
    bam, ref = _sample(tmp_path)
    common = {"input": Path(bam), "ref": Path(ref), "threads": 1}
    sv = api.vcf.sv(api.VcfSvParams(**common, outdir=tmp_path / "sv"))
    (out,) = [o for o in sv.outputs if o.role == "structural_variants"]
    assert _near_deletion(_records(Path(out.path)))
    assert sv.metrics["caller"] == "delly"
    cnv = api.vcf.cnv(
        api.VcfCnvParams(**common, outdir=tmp_path / "cnv", region="chr1")
    )
    (out,) = [o for o in cnv.outputs if o.role == "copy_number_variants"]
    assert Path(out.path).name == "cnv.vcf.gz" and Path(out.path).is_file()


@needs_delly
def test_cnv_region_cant_be_read_as_a_samtools_option(tmp_path):
    # Review of PR #454: a region "-o<file>" made the region cut write <file>.
    bam, ref = _sample(tmp_path)
    written = tmp_path / "written.sam"
    with pytest.raises(api.CommandFailed):
        api.vcf.cnv(
            api.VcfCnvParams(
                input=Path(bam), ref=Path(ref), outdir=tmp_path / "cnv",
                region=f"-o{written}",
            )
        )  # fmt: skip
    assert not written.exists()


@needs_delly
def test_region_bam_takes_the_reference_contigs(tmp_path):
    bam, ref = _sample(tmp_path)
    region_bam = str(tmp_path / "region.bam")
    structural._extract_region_bam_with_reference_header(
        bam, "chr1:4000-6000", ref, region_bam
    )
    samtools = get_tool_path("samtools")
    header = run_command([samtools, "view", "-H", region_bam], capture_output=True)
    assert [line for line in header.stdout.splitlines() if "@SQ" in line] == [
        f"@SQ\tSN:chr1\tLN:{GENOME}"
    ]
    assert "@RG\tID:1" in header.stdout
    reads = run_command([samtools, "view", region_bam], capture_output=True).stdout
    expected = run_command(
        [samtools, "view", "-c", bam, "chr1:4000-6000"], capture_output=True
    ).stdout
    assert len(reads.splitlines()) == int(expected)
    # The read whose mate is on the dropped decoy is kept, its mate unknown.
    mate = next(r for r in reads.splitlines() if r.startswith("d0\t")).split("\t")
    assert mate[2] == "chr1" and mate[6] == "*"


@needs_delly
def test_region_extraction_and_delly_failures_are_reported(tmp_path):
    bam, ref = _sample(tmp_path)
    # Without its index the BAM's region can't be read.
    unindexed = tmp_path / "unindexed.bam"
    shutil.copy(bam, unindexed)
    with pytest.raises(WGSExtractError, match="SV region extraction failed.*exit 1"):
        _run("sv", str(unindexed), ref, tmp_path / "out", "--region", "chr1")
    with pytest.raises(WGSExtractError, match="CNV region extraction failed"):
        _run("cnv", str(unindexed), ref, tmp_path / "out", "--region", "chr1")
    # Too few reads in the region for Delly to estimate the library.
    with pytest.raises(WGSExtractError, match="SV calling failed with exit code 1"):
        _run("sv", bam, ref, tmp_path / "out", "--region", "chr1:100-300")
    # Too little coverage over the genome for its GC correction.
    with pytest.raises(WGSExtractError, match="CNV calling failed with exit code 1"):
        _run("cnv", bam, ref, tmp_path / "out", "--region", "chr1:1-20000")
    assert not [p for p in (tmp_path / "out").iterdir() if p.name.startswith(".")]


@needs_delly
def test_rerunning_sv_replaces_the_index(tmp_path):
    bam, ref = _sample(tmp_path)
    out = tmp_path / "out"
    _run("sv", bam, ref, out, "--region", "chr1:1-20000")  # no SVs
    _run("sv", bam, ref, out)
    assert _near_deletion(_records(out / "sv.vcf.gz", region="chr1"))


@needs_delly
def test_delly_cnv_finds_the_deletion_and_takes_a_ploidy(tmp_path, caplog):
    bam, ref = _sample(tmp_path)
    out = tmp_path / "out"
    with (
        caplog.at_level(logging.WARNING),
        patch.object(structural, "run_command", wraps=structural.run_command) as rc,
    ):
        _run("cnv", bam, ref, out, "--ploidy", "2", "--map", "unused.map")
    delly = next(c.args[0] for c in rc.call_args_list if c.args[0][0] == "delly")
    assert delly[:2] == ["delly", "cnv"] and delly[delly.index("-y") + 1] == "2"
    assert "Ignoring --map" in caplog.text
    assert _near_deletion(_records(out / "cnv.vcf.gz"))
    assert sorted(p.name for p in out.iterdir()) == ["cnv.vcf.gz", "cnv.vcf.gz.tbi"]


# --- pbsv and Sniffles (stood in for) -------------------------------------------

SV_VCF = (
    "##fileformat=VCFv4.2\n##contig=<ID=chr1,length=60000>\n"
    '##INFO=<ID=SVTYPE,Number=1,Type=String,Description="t">\n'
    '##INFO=<ID=END,Number=1,Type=Integer,Description="e">\n'
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
)


_FAKE_CALLER = """\
import json, sys
name, args = {name!r}, sys.argv[1:]
if args in (["--version"], ["--help"]):
    print(name, "2.0.0")
    sys.exit(0)
state = json.load(open({state!r}))
record = {{"argv": [name, *args]}}
if "--regions" in args:
    record["bed"] = open(args[args.index("--regions") + 1]).read()
if args[:1] == ["call"]:
    record["reference"] = open(args[-3], "rb").read(1).decode()
with open(state["log"], "a") as log:
    log.write(json.dumps(record) + "\\n")
if name == state["fail"]:
    sys.exit(3)
vcf = open({header!r}).read() + state["records"]
if args[:1] == ["discover"]:
    open(args[-1], "wb").write(b"svsig")
elif args[:1] == ["call"]:
    assert open(args[-2], "rb").read() == b"svsig"
    open(args[-1], "w").write(vcf)
else:
    open(args[args.index("--vcf") + 1], "w").write(vcf)
"""


class _LongReadCallers:
    """pbsv and Sniffles stand-ins in ``bindir``, writing the VCF each would
    (one deletion, or none) and logging how they were run."""

    def __init__(self, bindir: Path, tools=("pbsv", "sniffles")):
        self._state = bindir / "callers.json"
        self._data = {"log": str(bindir / "calls.jsonl"), "fail": "", "records": ""}
        self.records = "chr1\t30000\tdel\tN\t<DEL>\t60\tPASS\tSVTYPE=DEL;END=32000\n"
        header = bindir / "header.vcf"
        header.write_text(SV_VCF)
        for name in tools:
            python_tool(
                bindir,
                name,
                _FAKE_CALLER.format(
                    name=name, state=str(self._state), header=str(header)
                ),
            )

    def _set(self, key, value):
        self._data[key] = value
        self._state.write_text(json.dumps(self._data))

    @property
    def records(self) -> str:
        return self._data["records"]

    @records.setter
    def records(self, value: str) -> None:
        self._set("records", value)

    @property
    def fail(self) -> str:
        return self._data["fail"]

    @fail.setter
    def fail(self, value: str) -> None:
        self._set("fail", value)

    @property
    def runs(self) -> list[dict]:
        log = Path(self._data["log"])
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    @property
    def calls(self) -> list[list[str]]:
        return [run["argv"] for run in self.runs]

    def clear(self) -> None:
        Path(self._data["log"]).unlink(missing_ok=True)


def _long_read_bam(tmp_path: Path, name: str = "hifi", platform: str = "") -> str:
    """A real one-read BAM, its read group naming ``platform`` if given."""
    group = [f"@RG\tID:1\tSM:S\tPL:{platform}"] if platform else []
    tag = "\tRG:Z:1" if platform else ""
    return sam_to_bam(
        ["@HD\tVN:1.6\tSO:coordinate", "@SQ\tSN:chr1\tLN:60000", *group,
         f"r1\t0\tchr1\t100\t60\t4M\t*\t0\t0\tACGT\tIIII{tag}"],
        tmp_path / f"{name}.bam",
    )  # fmt: skip


@pytest.fixture
def long_read(tmp_path, monkeypatch):
    """(callers, bam, ref) with pbsv and Sniffles 'installed'."""
    require_bare_name_fakes()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    callers = _LongReadCallers(bindir)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    ref = write_fasta(tmp_path / "ref.fa", {"chr1": "ACGT" * 15_000})
    return callers, _long_read_bam(tmp_path), str(ref)


@needs_htslib
def test_pacbio_runs_pbsv_discover_then_call(tmp_path, long_read):
    callers, bam, ref = long_read
    repeats = tmp_path / "trf.bed"
    repeats.write_text("chr1\t0\t100\n")
    out = tmp_path / "out"
    _run("sv", bam, ref, out, "--pacbio", "--tandem-repeats", str(repeats))

    discover, call = callers.calls
    assert discover[:2] == ["pbsv", "discover"]
    assert discover[discover.index("--tandem-repeats") + 1] == str(repeats)
    assert "--hifi" in discover and "--region" not in discover
    assert discover[-2] == bam
    assert call[:4] == ["pbsv", "call", "-j", "1"] and "--ccs" in call
    assert call[-3] == ref
    assert _records(out / "pbsv.vcf.gz") == [("chr1", "30000", "32000")]
    assert sorted(p.name for p in out.iterdir()) == ["pbsv.vcf.gz", "pbsv.vcf.gz.tbi"]


@needs_htslib
def test_pbsv_calls_a_region_against_an_uncompressed_reference(tmp_path, long_read):
    callers, bam, _ref = long_read
    ref = tmp_path / "gz" / "ref.fa"
    write_fasta(ref, {"chr1": "ACGT" * 15_000})
    run_command([get_tool_path("bgzip"), "-i", str(ref)])
    run_command([get_tool_path("samtools"), "faidx", f"{ref}.gz"])
    ref.with_suffix(".fa.fai").unlink()
    out = tmp_path / "out"

    _run("sv", bam, f"{ref}.gz", out, "--pacbio", "--region", "chr1")

    discover, call = callers.runs
    for run in (discover, call):
        argv = run["argv"]
        # contract: pbsv discovers and calls only the region asked for
        assert argv[argv.index("--region") + 1] == "chr1"
    # pbsv call can't read .fa.gz: it got a plain FASTA, since removed.
    assert call["reference"] == ">"
    assert not Path(call["argv"][-3]).exists()
    assert sorted(p.name for p in out.iterdir()) == ["pbsv.vcf.gz", "pbsv.vcf.gz.tbi"]


@needs_htslib
def test_pacbio_falls_back_to_sniffles_without_pbsv(tmp_path, request):
    bindir = only_tools(request, "samtools", "bcftools", "bgzip", "tabix")
    callers = _LongReadCallers(bindir, tools=("sniffles",))
    ref = write_fasta(tmp_path / "ref.fa", {"chr1": "ACGT" * 15_000})
    out = tmp_path / "out"
    _run("sv", _long_read_bam(tmp_path), str(ref), out, "--pacbio")
    assert [c[0] for c in callers.calls] == ["sniffles"]
    assert _records(out / "sniffles.vcf.gz") == [("chr1", "30000", "32000")]


@needs_htslib
def test_sniffles_takes_a_region_as_a_bed_file(tmp_path, long_read):
    callers, bam, ref = long_read
    _run("sv", bam, ref, tmp_path / "out", "--caller", "sniffles", "--region", "chr1")
    # Sniffles2 --regions takes a BED file, not a region string.
    (run,) = callers.runs
    assert run["bed"] == "chr1\t0\t60000\n"
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == [
        "sniffles.vcf.gz",
        "sniffles.vcf.gz.tbi",
    ]


@needs_htslib
def test_pbsv_without_pacbio_uses_default_thresholds(tmp_path, long_read):
    callers, bam, ref = long_read
    _run("sv", bam, ref, tmp_path / "out", "--caller", "pbsv")
    discover, call = callers.calls
    assert "--hifi" not in discover and "--ccs" not in call


@needs_htslib
def test_pbsv_failures_are_reported(tmp_path, long_read):
    callers, bam, ref = long_read
    with pytest.raises(WGSExtractError, match="--tandem-repeats"):
        _run("sv", bam, ref, tmp_path / "out", "--caller", "pbsv",
             "--tandem-repeats", str(tmp_path / "missing.bed"))  # fmt: skip
    assert callers.calls == []
    callers.fail = "pbsv"
    with pytest.raises(WGSExtractError, match="PacBio SV calling failed .* 3"):
        _run("sv", bam, ref, tmp_path / "out", "--caller", "pbsv")
    assert not list((tmp_path / "out").iterdir())


@needs_htslib
def test_pacbio_prefers_pbsv_when_installed(tmp_path, long_read):
    callers, bam, ref = long_read
    _run("sv", bam, ref, tmp_path / "out", "--pacbio")
    assert [c[0] for c in callers.calls] == ["pbsv", "pbsv"]
    callers.clear()
    _run("sv", bam, ref, tmp_path / "out", "--pacbio", "--caller", "sniffles")
    assert [c[0] for c in callers.calls] == ["sniffles"]


@needs_htslib
def test_sniffles_whole_genome_and_its_warnings(tmp_path, long_read, caplog):
    callers, bam, ref = long_read
    out = tmp_path / "out"
    _run("sv", bam, ref, out, "--caller", "sniffles")
    (cmd,) = callers.calls
    assert "--regions" not in cmd
    assert cmd[cmd.index("--reference") + 1] == ref
    assert cmd[cmd.index("--threads") + 1] == "1"
    assert _records(out / "sniffles.vcf.gz") == [("chr1", "30000", "32000")]

    callers.records = ""
    with caplog.at_level(logging.WARNING):
        _run("sv", bam, ref, tmp_path / "empty", "--caller", "sniffles")
    assert "No variants were called" in caplog.text

    callers.fail = "sniffles"
    with pytest.raises(WGSExtractError, match="Sniffles SV calling failed"):
        _run("sv", bam, ref, out, "--caller", "sniffles", "--region", "chr1:1-100")
    with pytest.raises(WGSExtractError, match="Invalid region"):
        _run("sv", bam, ref, out, "--caller", "sniffles", "--region", "chr1:9-2")


def _platform_bam(tmp_path: Path, platform: str) -> str:
    """A real one-read BAM whose read group names the sequencing platform."""
    return _long_read_bam(tmp_path, platform.lower(), platform)


@needs_htslib
@pytest.mark.parametrize("flags", [[], ["--ont"], ["--long-read"]])
def test_ont_input_runs_sniffles_by_default(tmp_path, long_read, flags, caplog):
    callers, _bam, ref = long_read
    caplog.set_level(logging.INFO)
    bam = _platform_bam(tmp_path, "ONT")
    repeats = tmp_path / "trf.bed"
    repeats.write_text("chr1\t0\t100\n")
    out = tmp_path / "out"
    _run("sv", bam, ref, out, *flags, "--tandem-repeats", str(repeats))
    (cmd,) = callers.calls
    assert cmd[0] == "sniffles" and cmd[cmd.index("--input") + 1] == bam
    assert cmd[cmd.index("--tandem-repeats") + 1] == str(repeats)
    assert _records(out / "sniffles.vcf.gz") == [("chr1", "30000", "32000")]
    # #299: the log named Delly while Sniffles ran.
    assert "Calling SVs using sniffles" in caplog.text
    assert "delly" not in caplog.text.lower()


@needs_htslib
@pytest.mark.parametrize(
    "flags",
    [
        ["--caller", "pbsv"],
        ["--ont", "--caller", "pbsv"],
        ["--long-read", "--caller", "pbsv"],
        ["--pacbio", "--caller", "pbsv"],
        ["--pacbio"],
    ],
)
def test_pbsv_is_refused_for_ont_reads(tmp_path, long_read, flags):
    callers, _bam, ref = long_read
    bam = _platform_bam(tmp_path, "ONT")
    with pytest.raises(WGSExtractError, match="pbsv is PacBio-only"):
        _run("sv", bam, ref, tmp_path / "out", *flags)
    assert callers.calls == []


@needs_htslib
def test_pacbio_input_keeps_delly_default_and_pbsv(tmp_path, long_read):
    callers, _bam, ref = long_read
    bam = _platform_bam(tmp_path, "PACBIO")
    params = VcfSvParams(input=Path(bam), ref=Path(ref))
    assert structural._select_sv_caller(params).caller == "delly"
    _run("sv", bam, ref, tmp_path / "out", "--long-read", "--caller", "pbsv")
    assert [c[:2] for c in callers.calls] == [["pbsv", "discover"], ["pbsv", "call"]]
    callers.clear()
    _run("sv", bam, ref, tmp_path / "lr", "--long-read")
    assert [c[0] for c in callers.calls] == ["sniffles"]
    callers.clear()
    _run("sv", bam, ref, tmp_path / "pb", "--pacbio")
    assert [c[0] for c in callers.calls] == ["pbsv", "pbsv"]


@pytest.mark.parametrize(
    ("flags", "message"),
    [
        (["--ont", "--pacbio"], "cannot be combined"),
        (["--ont", "--caller", "delly"], "--ont selects a long-read"),
        (["--long-read", "--caller", "delly"], "--long-read selects a long-read"),
        (["--pacbio", "--caller", "delly"], "--pacbio selects a long-read"),
    ],
)
def test_contradictory_long_read_flags_are_rejected(tmp_path, flags, message):
    bam = tmp_path / "reads.bam"
    bam.write_bytes(b"")
    with pytest.raises(WGSExtractError, match=message):
        _run("sv", str(bam), str(tmp_path / "ref.fa"), tmp_path / "out", *flags)


@needs_htslib
def test_explicit_delly_on_ont_warns(tmp_path, long_read, caplog):
    _callers, _bam, ref = long_read
    params = VcfSvParams(
        input=Path(_platform_bam(tmp_path, "ONT")), ref=Path(ref), caller="delly"
    )
    with caplog.at_level(logging.WARNING):
        assert structural._select_sv_caller(params).caller == "delly"
    assert "not validated for ONT" in caplog.text
