"""#342: `basecall` turns Nanopore POD5 signal into an unaligned BAM with ONT's
Dorado, an optional tool that is checked, never installed. Dorado is faked at
its boundary: it reads text "POD5" files and writes its BAM with real samtools.
"""

import json
import os
import platform
import random
import shutil
import sys
from pathlib import Path

import pytest

from tests.fake_tools import python_tool, require_bare_name_fakes
from tests.htslib_helpers import needs_tools, run_wgsextract, sam_to_bam, write_fasta
from wgsextract_cli import api
from wgsextract_cli.core.dependencies import clear_tool_caches
from wgsextract_cli.core.utils import run_command

pytestmark = needs_tools("samtools")

APPLE_SILICON = sys.platform == "darwin" and platform.machine() == "arm64"

# Like Dorado: models named for the run's chemistry are fetched into
# --models-directory; reads go to stdout as BAM, with the models in @RG DS and
# MM/ML for --modified-bases. A "POD5" here is lines of "<read id>\t<bases>".
_FAKE_DORADO = r"""
import json, os, subprocess, sys

argv = sys.argv[1:]
if argv == ["--version"]:
    print({version!r})
    sys.exit(0)
with open({log!r}, "a") as log:
    log.write(json.dumps(argv) + "\n")
assert argv[0] == "basecaller"
model, data = argv[1], argv[2]
opts, mods, i = {{}}, [], 3
while i < len(argv):
    if argv[i] == "--modified-bases":
        mods = argv[i + 1:]
        break
    if argv[i] == "--recursive":
        opts["recursive"], i = True, i + 1
    else:
        opts[argv[i]], i = argv[i + 1], i + 2
if not os.path.isdir(model):
    model = "dna_r10.4.1_e8.2_400bps_" + model.split("@")[0] + "@v5.0.0"
    os.makedirs(os.path.join(opts["--models-directory"], model), exist_ok=True)
files = [data]
if os.path.isdir(data):
    files = [os.path.join(d, f) for d, _, fs in os.walk(data) for f in fs
             if f.endswith(".pod5")]
wanted = None
if "--read-ids" in opts:
    wanted = set(open(opts["--read-ids"]).read().split())
modbase = ",".join(model + "_" + m + "@v2" for m in mods)
ds = "basecall_model=" + model + (" modbase_models=" + modbase if mods else "")
sam = ["@HD\tVN:1.6\tSO:unknown", "@RG\tID:run1_" + model + "\tPL:ONT\tDS:" + ds]
for path in files:
    for line in open(path):
        name, seq = line.split()
        if wanted is not None and name not in wanted:
            continue
        tags = "\tRG:Z:run1_" + model
        if mods and {modified}:
            tags += "\tMM:Z:C+m?,0;\tML:B:C,230"
        sam.append(f"{{name}}\t4\t*\t0\t0\t*\t*\t0\t0\t{{seq}}\t{{'5' * len(seq)}}{{tags}}")
subprocess.run([{samtools!r}, "view", "-b", "-"], input="\n".join(sam) + "\n",
               text=True, check=True)
"""


class FakeDorado:
    def __init__(self, bindir: Path, version: str = "2.1.2+8b8fc5d", modified=True):
        self.log = bindir / "dorado-runs.jsonl"
        self.path = python_tool(bindir, "dorado", _FAKE_DORADO.format(
            version=version, log=str(self.log), modified=modified,
            samtools=shutil.which("samtools"),
        ))  # fmt: skip

    def runs(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]


@pytest.fixture
def dorado(tmp_path, monkeypatch):
    require_bare_name_fakes()
    bindir = tmp_path / "dorado" / "bin"
    fake = FakeDorado(bindir)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    clear_tool_caches()
    yield fake
    clear_tool_caches()


@pytest.fixture
def genome(tmp_path):
    rng = random.Random(342)
    seq = "".join(rng.choice("ACGT") for _ in range(20_000))
    return seq, write_fasta(tmp_path / "ref.fa", {"chr1": seq})


def _pod5(path: Path, reads: dict[str, str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{name}\t{seq}\n" for name, seq in reads.items()))
    return path


def _reads(seq: str) -> dict[str, str]:
    return {f"read{i}": seq[i * 4000 : i * 4000 + 3000] for i in range(4)}


def _view(path, *opts):
    out = run_command(["samtools", "view", *opts, str(path)], capture_output=True)
    return [line.split("\t") for line in out.stdout.splitlines()]


def _basecall(tmp_path, source, **params):
    params.setdefault("models_dir", tmp_path / "models")
    params.setdefault("device", "cpu")
    return api.align.basecall(
        api.BasecallParams(input=source, outdir=tmp_path / "out", **params)
    )


def _codes(result):
    return {w.code: w.details for w in result.warnings}


@needs_tools("minimap2")
def test_modified_bases_survive_into_the_aligned_bam(tmp_path, genome, dorado):
    """#342: the unaligned BAM keeps Dorado's MM/ML tags, and `align` takes it
    as ONT reads (from its @RG PL:ONT) without being told the platform, keeping
    the basecaller models its read group names."""
    seq, ref = genome
    reads = _reads(seq)
    pod5 = _pod5(tmp_path / "pod5" / "flowcell.pod5", reads)

    result = _basecall(tmp_path, pod5, modified_bases="5mCG_5hmCG")

    bam = tmp_path / "out" / "flowcell.hac.5mCG_5hmCG.bam"
    [output] = result.outputs
    assert (Path(output.path), output.role) == (bam, "unaligned_reads")
    assert result.metrics["reads"] == result.metrics["reads_with_modifications"] == 4
    assert result.metrics["model"] == "dna_r10.4.1_e8.2_400bps_hac@v5.0.0"
    assert result.metrics["modbase_models"].endswith("_5mCG_5hmCG@v2")
    assert result.metrics["device"] == "cpu"
    assert "basecall_on_cpu" in _codes(result)
    # The model was fetched into the models folder, not the working folder.
    assert (tmp_path / "models" / "dna_r10.4.1_e8.2_400bps_hac@v5.0.0").is_dir()
    [argv] = dorado.runs()
    # contract: --modified-bases takes every value after it, so it must come
    # after the model and data positionals.
    assert argv[:3] == ["basecaller", "hac", str(pod5)]
    assert argv[-2:] == ["--modified-bases", "5mCG_5hmCG"]

    run_wgsextract(
        "align", "--r1", str(bam), "--ref", str(ref), "--outdir",
        str(tmp_path / "aligned"), "--threads", "1", "--memory", "256M",
    )  # fmt: skip

    [aligned] = (tmp_path / "aligned").glob("*.bam")
    primary = _view(aligned, "-F", "0x904")
    assert sorted(r[0] for r in primary) == sorted(reads)
    assert {r[2] for r in primary} == {"chr1"}
    assert all("MM:Z:C+m?,0;" in r[11:] for r in primary)
    assert all(any(t.startswith("ML:B:C") for t in r[11:]) for r in primary)
    # The basecaller models stay named, for Clair3's model choice.
    header = run_command(
        ["samtools", "view", "-H", str(aligned)], capture_output=True
    ).stdout
    [read_group] = [line for line in header.splitlines() if line.startswith("@RG")]
    assert "PL:ONT" in read_group
    assert "basecall_model=dna_r10.4.1_e8.2_400bps_hac@v5.0.0" in read_group


def test_a_folder_is_basecalled_recursively(tmp_path, genome, dorado):
    seq, _ = genome
    reads = _reads(seq)
    _pod5(tmp_path / "pod5" / "a" / "1.pod5", dict(list(reads.items())[:2]))
    _pod5(tmp_path / "pod5" / "b" / "2.pod5", dict(list(reads.items())[2:]))

    result = _basecall(tmp_path, tmp_path / "pod5", model="fast")

    bam = tmp_path / "out" / "pod5.fast.bam"
    assert sorted(r[0] for r in _view(bam)) == sorted(reads)
    assert "--recursive" in dorado.runs()[0]
    assert "reads_with_modifications" not in result.metrics


def test_reads_named_in_a_list_are_basecalled_alone(tmp_path, genome, dorado):
    seq, _ = genome
    pod5 = _pod5(tmp_path / "run.pod5", _reads(seq))
    ids = tmp_path / "ids.txt"
    ids.write_text("read1\nread3\n")

    result = _basecall(tmp_path, pod5, read_ids=ids)

    bam = tmp_path / "out" / "run.hac.subset.bam"
    assert sorted(r[0] for r in _view(bam)) == ["read1", "read3"]
    assert result.metrics["reads"] == 2


def test_a_read_list_matching_nothing_warns(tmp_path, genome, dorado):
    seq, _ = genome
    pod5 = _pod5(tmp_path / "run.pod5", _reads(seq))
    ids = tmp_path / "ids.txt"
    ids.write_text("not-in-this-run\n")

    result = _basecall(tmp_path, pod5, read_ids=ids)

    assert result.metrics["reads"] == 0
    assert "no_reads" in _codes(result)


def test_panel_reads_are_re_basecalled_by_their_pod5_id(tmp_path, genome, dorado):
    """--regions-bed with --alignment re-basecalls the reads mapped in the
    panel, naming a read Dorado split by its parent (pi tag), the ID its signal
    has in the POD5; unmapped and off-panel reads are left out."""
    seq, ref = genome
    reads = _reads(seq)
    pod5 = _pod5(tmp_path / "run.pod5", reads)

    def record(name, flag, pos, extra=""):
        chrom, at = ("*", 0) if flag & 4 else ("chr1", pos)
        return (
            f"{name}\t{flag}\t{chrom}\t{at}\t{60 if chrom != '*' else 0}\t"
            f"{'100M' if chrom != '*' else '*'}\t*\t0\t0\t{'A' * 100}\t{'5' * 100}"
            f"{extra}"
        )

    aligned = sam_to_bam(
        [
            "@HD\tVN:1.6\tSO:coordinate",
            "@SQ\tSN:chr1\tLN:20000",
            "@RG\tID:r\tPL:ONT\tSM:s",
            record("read0", 0, 1_000),
            record("child-of-read2", 0, 1_200, "\tpi:Z:read2"),
            record("read1", 0, 15_000),
            record("read3", 4, 0),
        ],
        tmp_path / "aligned.bam",
    )
    panel = tmp_path / "panel.bed"
    panel.write_text("chr1\t900\t2000\n")

    result = _basecall(
        tmp_path, pod5, model="sup", regions_bed=panel, alignment=aligned, ref=ref
    )

    bam = tmp_path / "out" / "run.sup.subset.bam"
    assert sorted(r[0] for r in _view(bam)) == ["read0", "read2"]
    assert {i.role for i in result.inputs} >= {"alignment", "regions_bed"}


def test_a_panel_without_mapped_reads_is_refused(tmp_path, genome, dorado):
    seq, _ = genome
    pod5 = _pod5(tmp_path / "run.pod5", _reads(seq))
    aligned = sam_to_bam(
        ["@HD\tVN:1.6\tSO:coordinate", "@SQ\tSN:chr1\tLN:20000"],
        tmp_path / "aligned.bam",
    )
    panel = tmp_path / "panel.bed"
    panel.write_text("chr1\t900\t2000\n")

    with pytest.raises(api.CommandFailed, match="no mapped reads"):
        _basecall(tmp_path, pod5, regions_bed=panel, alignment=aligned)
    assert dorado.runs() == []


def test_missing_modification_tags_warn(tmp_path, genome, monkeypatch):
    """A model without the requested modification calls writes no MM/ML;
    the run says so rather than leaving methylation pileup to find nothing."""
    seq, _ = genome
    fake = FakeDorado(tmp_path / "bin", modified=False)
    pod5 = _pod5(tmp_path / "run.pod5", _reads(seq))

    result = _basecall(tmp_path, pod5, modified_bases="6mA", dorado_path=str(fake.path))

    assert result.metrics["reads_with_modifications"] == 0
    assert _codes(result)["no_modification_tags"] == {"modifications": "6mA"}


def test_a_missing_dorado_says_how_to_install_it(tmp_path, genome, request):
    from tests.fake_tools import only_tools

    only_tools(request, "samtools")
    clear_tool_caches()
    pod5 = _pod5(tmp_path / "run.pod5", _reads(genome[0]))

    with pytest.raises(api.CommandFailed, match="nanoporetech/dorado"):
        _basecall(tmp_path, pod5)


def test_dorado_before_1_0_is_refused(tmp_path, genome):
    old = FakeDorado(tmp_path / "bin", version="0.9.6+abc")
    pod5 = _pod5(tmp_path / "run.pod5", _reads(genome[0]))

    with pytest.raises(api.CommandFailed, match="0.9.6 is too old"):
        _basecall(tmp_path, pod5, dorado_path=str(old.path))
    assert old.runs() == []


def test_fast5_is_refused_with_how_to_convert_it(tmp_path, dorado):
    fast5 = tmp_path / "reads" / "batch0.fast5"
    fast5.parent.mkdir()
    fast5.write_bytes(b"\x89HDF")

    for source in (fast5, fast5.parent):
        with pytest.raises(api.CommandFailed, match="pod5 convert fast5"):
            _basecall(tmp_path, source)
    assert dorado.runs() == []


@pytest.mark.skipif(APPLE_SILICON, reason="auto picks Metal on Apple silicon")
@pytest.mark.parametrize(("gpus", "device"), [("", "cpu"), ("GPU 0: A100", "cuda:all")])
def test_auto_device_uses_an_nvidia_gpu_only_when_one_is_listed(
    tmp_path, genome, dorado, gpus, device
):
    bindir = tmp_path / "dorado" / "bin"
    python_tool(bindir, "nvidia-smi", f"print({gpus!r})")
    clear_tool_caches()
    pod5 = _pod5(tmp_path / "run.pod5", _reads(genome[0]))

    result = _basecall(tmp_path, pod5, device="auto")

    [argv] = dorado.runs()
    # contract: -x is Dorado's device; the CPU is a fallback, never a GPU guess.
    assert argv[argv.index("-x") + 1] == device
    assert ("basecall_on_cpu" in _codes(result)) == (device == "cpu")
