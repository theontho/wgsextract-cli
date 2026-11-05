"""Tests for `analyze comprehensive` and `analyze batch-gen`.

The subcommands the pipeline runs as child `wgsextract` processes (info,
lineage, vcf snp/indel, chain-annotate) need samtools, Haplogrep, Yleaf and
gigabytes of annotation data, so the child interpreter is replaced by a fake
that writes the files each step would write and logs its command line. The
step journal, discovery filter, report and batch handling run for real.
"""

import csv
import json
import os
import re
import shutil
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil
import pytest

from tests.fake_tools import python_tool
from tests.htslib_helpers import bgzip_index, needs_tools, run_wgsextract, sam_to_bam
from tests.smoke_utils import state_directory_setting
from wgsextract_cli.commands import _analyze_workflows as wf
from wgsextract_cli.core.config import get_config_path
from wgsextract_cli.core.dependencies import clear_tool_caches, get_tool_path
from wgsextract_cli.core.utils import WGSExtractError, run_command
from wgsextract_cli.main import main

needs_bcftools = needs_tools("bcftools", "bgzip", "tabix", "samtools")

_INFO_HEADERS = {
    "GNOMAD_AF": '##INFO=<ID=GNOMAD_AF,Number=A,Type=Float,Description="g">',
    "CLNSIG": '##INFO=<ID=CLNSIG,Number=.,Type=String,Description="c">',
    "REVEL": '##INFO=<ID=REVEL,Number=1,Type=Float,Description="r">',
}


def write_vcf(
    path: Path,
    records: Iterable[tuple[int, str, str, str]],
    infos: Iterable[str] = (),
    header: str = "",
) -> str:
    """bgzipped, indexed single-contig VCF of (POS, ID, QUAL, INFO) A>G sites."""
    lines = [
        "##fileformat=VCFv4.2",
        "##contig=<ID=chr1,length=10000>",
        *(_INFO_HEADERS[i] for i in infos),
        *([header] if header else []),
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO",
        *(f"chr1\t{p}\t{i}\tA\tG\t{q}\tPASS\t{inf}" for p, i, q, inf in records),
    ]
    plain = Path(str(path).removesuffix(".gz"))
    plain.write_text("\n".join(lines) + "\n")
    return bgzip_index(plain, path)


def ids(vcf: str | Path) -> list[str]:
    out: str = run_command(
        [get_tool_path("bcftools"), "query", "-f", "%ID\n", str(vcf)],
        capture_output=True,
    ).stdout
    return out.split()


# Annotated output the fake chain-annotate writes: id1 rare, id2 common and
# benign, id3 common but pathogenic.
_ANNOTATED = [
    (100, "rare", "50", "GNOMAD_AF=0.001"),
    (200, "common", "50", "GNOMAD_AF=0.3;CLNSIG=Benign"),
    (300, "patho", "50", "GNOMAD_AF=0.3;CLNSIG=Pathogenic"),
]

# Every annotation's data, as the config settings that point at it.
_ANNOTATION_DATA = {
    "clinvar": "clinvar_vcf_path",
    "revel": "revel_tsv_path",
    "phylop": "phylop_tsv_path",
    "cadd": "cadd_tsv_path",
    "gerp": "gerp_bigwig_path",
    "gnomad": "gnomad_vcf_path",
    "spliceai": "spliceai_vcf_path",
    "alphamissense": "alphamissense_vcf_path",
    "pharmgkb": "pharmgkb_vcf_path",
}

_FAKE_WGSEXTRACT = """\
import json, os, shutil, subprocess, sys
from pathlib import Path

if sys.argv[1:3] != ["-m", "wgsextract_cli.main"]:
    # Not a step, such as the keep-awake helper: the real interpreter runs it.
    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
home = Path({home!r})
state = json.loads((home / "state.json").read_text())
argv = sys.argv[3:]  # after `-m wgsextract_cli.main`
with open(home / "calls.jsonl", "a") as log:
    log.write(json.dumps(argv) + "\\n")
# The real parser: a step must accept every option it is given.
from wgsextract_cli.cli.parser import build_parser
build_parser()[0].parse_args(argv)
while argv[0].startswith("--"):
    argv = argv[2:] if argv[0] in ("--config", "--tmp-dir") else argv[1:]
name = " ".join(argv[:2]) if argv[0] in ("lineage", "vcf") else argv[0]
opt = dict(zip(argv, argv[1:]))
outdir = Path(opt["--outdir"])
output = {{
    "vcf snp": outdir / "snps.vcf.gz",
    "vcf indel": outdir / "indels.vcf.gz",
    "vcf chain-annotate": outdir / "chain_annotated.vcf.gz",
}}.get(name)
document = {{"command": name}}
if name in state["fail"]:
    sys.exit(f"{{name}} failed")
if name == state["die"]:
    output.write_bytes(b"\\x1f\\x8b half a file")
    if state["writer"]:
        # A tool of the step that outlives it, still writing.
        writer = "import sys, time; open(sys.argv[1], 'ab').write(b'1'); time.sleep(60)"
        tool = subprocess.Popen([sys.executable, "-c", writer, str(output)],
                                start_new_session=True, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        (home / "writer.pid").write_text(str(tool.pid))
    os._exit(9)
if name == "info":
    cache = outdir / (Path(opt["--input"]).name + ".wgse_info.json")
    cache.write_text("{{not json" if state["broken_info"]
                     else json.dumps({{"gender": state["gender"]}}))
elif output and not (name in state["skip_existing"] and output.exists()):
    source = state["outputs"].get(name)
    if source:
        shutil.copy(source, output)
        shutil.copy(source + ".tbi", str(output) + ".tbi")
if name == "vcf snp" and state["reference"]:
    # Callers name the FASTA they used only in their provenance.
    document["provenance"] = {{"reference": {{"path": state["reference"]}}}}
Path(opt["--result-file"]).write_text(json.dumps(document))
"""


@dataclass
class Run:
    """One `analyze comprehensive` run: its exit code, result document and the
    steps it ran (rather than skipped)."""

    code: int
    document: dict[str, Any]
    ran: list[str] = field(default_factory=list)

    @property
    def error(self) -> str:
        return str((self.document.get("error") or {}).get("message", ""))


class FakeSteps:
    """The child `wgsextract` processes of `analyze comprehensive`."""

    def __init__(self, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.home = home
        home.mkdir()
        self.gender = "Male"
        self.fail: set[str] = set()
        self.die: str | None = None
        self.writer = False
        self.skip_existing: set[str] = set()
        self.broken_info = False
        self.reference: Path | None = None
        self.outputs: dict[str, str] = {}
        if shutil.which("bgzip") and get_tool_path("bcftools"):
            self.outputs = {
                "vcf snp": write_vcf(home / "snps.vcf.gz", [(100, "s", "50", ".")]),
                "vcf indel": write_vcf(home / "indels.vcf.gz", [(100, "i", "50", ".")]),
                "vcf chain-annotate": write_vcf(
                    home / "annotated.vcf.gz", _ANNOTATED, ["GNOMAD_AF", "CLNSIG"]
                ),
            }
        monkeypatch.setattr(
            sys, "executable", str(python_tool(home / "bin", "python",
                                               _FAKE_WGSEXTRACT.format(home=str(home))))
        )  # fmt: skip
        # Every annotation's data is installed, and VEP with its cache.
        data = home / "data"
        data.mkdir()
        self.config = get_config_path().read_text()
        # An empty library, so only the configured data counts as installed.
        (home / "library").mkdir()
        self.settings: dict[str, object] = {"reference_library": str(home / "library")}
        for ann, key in _ANNOTATION_DATA.items():
            (data / ann).write_text(f"{ann} release 1")
            self.settings[key] = str(data / ann)
        (data / "vep").mkdir()
        self.settings["vep_cache_directory"] = str(data / "vep")
        python_tool(home / "tools", "vep", "")
        monkeypatch.setenv("PATH", f"{home / 'tools'}{os.pathsep}{os.environ['PATH']}")
        clear_tool_caches()
        self.runs = 0

    def install_only(self, *annotations: str) -> None:
        """Leave just these annotations' data (and VEP's cache) installed."""
        for ann, key in _ANNOTATION_DATA.items():
            if ann not in annotations:
                self.settings.pop(key, None)
        if "vep" not in annotations:
            self.settings.pop("vep_cache_directory", None)

    @property
    def calls(self) -> list[list[str]]:
        """The child command lines of the last run, without global flags."""
        log = self.home / "calls.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if (
            log.exists()
        ) else []  # fmt: skip

    def annotated(self) -> dict[str, str]:
        """chain-annotate --input -> --annotations, in the last run."""
        return {
            c[c.index("--input") + 1]: c[c.index("--annotations") + 1]
            for c in self.calls
            if "chain-annotate" in c
        }

    def prepare(self) -> None:
        """Write the children's behaviour and the run's config."""
        (self.home / "calls.jsonl").unlink(missing_ok=True)
        (self.home / "state.json").write_text(json.dumps({
            "gender": self.gender, "fail": sorted(self.fail), "die": self.die,
            "writer": self.writer, "skip_existing": sorted(self.skip_existing),
            "broken_info": self.broken_info, "outputs": self.outputs,
            "reference": str(self.reference) if self.reference else None,
        }))  # fmt: skip
        # Literal strings: TOML would read a Windows path's backslashes as escapes.
        get_config_path().write_text(
            self.config + "".join(f"{k} = '{v}'\n" for k, v in self.settings.items())
        )

    def run(self, *argv: str) -> Run:
        self.prepare()
        self.runs += 1
        document = self.home / f"result-{self.runs}.json"
        code = 0
        try:
            main(["analyze", "comprehensive", *argv, "--result-file", str(document)])
        except SystemExit as error:
            code = int(error.code or 0)
        result = json.loads(document.read_text()) if document.exists() else {}
        steps = (result.get("details") or {}).get("steps") or []
        ran = [s["step"] for s in steps if s["action"] == "run"]
        return Run(code, result, ran)


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeSteps:
    return FakeSteps(tmp_path / "fake-steps", monkeypatch)


def _bam(path: Path, sample: str = "S") -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    return sam_to_bam(
        ["@HD\tVN:1.6\tSO:coordinate", "@SQ\tSN:chr1\tLN:10000",
         f"@RG\tID:rg\tSM:{sample}"],
        path,
    )  # fmt: skip


@pytest.fixture
def bam(tmp_path: Path) -> str:
    """A header-only BAM; the fake steps never read its reads."""
    return _bam(tmp_path / "in" / "sample.bam")


_ALL_STEPS = [
    "info", "mt-haplogroup", "y-haplogroup", "snp", "indel",
    "annotate:snp-indel_snps", "discovery:snp-indel_snps",
    "annotate:snp-indel_indels", "discovery:snp-indel_indels",
]  # fmt: skip


# --- analyze comprehensive: orchestration ---------------------------------------


@needs_bcftools
def test_bam_only_runs_metrics_lineage_calling_then_annotates_both_call_sets(
    fake: FakeSteps, bam: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "out"
    run = fake.run("--input", bam, "--outdir", str(out))

    assert run.code == 0
    assert run.ran == _ALL_STEPS
    # contract: metrics, lineage and calling read the alignment the user gave.
    assert [c[c.index("--input") + 1] for c in fake.calls[:5]] == [bam] * 5
    assert list(fake.annotated()) == [str(out / "snps.vcf.gz"),
                                      str(out / "indels.vcf.gz")]  # fmt: skip
    # Annotated per call set in its own work dir; rare + pathogenic kept.
    for base in ("snps", "indels"):
        sig = out / f"significant_snp-indel_{base}.vcf.gz"
        assert ids(sig) == ["rare", "patho"]
        assert (out / f"work_snp-indel_{base}" / "chain_annotated.vcf.gz").exists()
    report = capsys.readouterr().out
    assert "TOTAL SIGNIFICANT VARIANTS FOUND: 4" in report
    assert "[SNP-INDEL] snps.vcf.gz" in report


def test_female_genome_skips_y_lineage(
    fake: FakeSteps, bam: str, tmp_path: Path
) -> None:
    fake.gender = "Female"
    run = fake.run("--input", bam, "--outdir", str(tmp_path / "o"), "--skip-calling")
    assert run.ran == ["info", "mt-haplogroup"]


def test_unreadable_info_cache_still_runs_y_lineage(
    fake: FakeSteps, bam: str, tmp_path: Path
) -> None:
    fake.broken_info = True
    run = fake.run("--input", bam, "--outdir", str(tmp_path / "o"), "--skip-calling")
    assert run.ran == ["info", "mt-haplogroup", "y-haplogroup"]


@needs_bcftools
def test_vcf_inputs_replace_calling_and_get_annotations_for_their_type(
    fake: FakeSteps, bam: str, tmp_path: Path
) -> None:
    vcfs = {
        name: write_vcf(tmp_path / name, [(100, "x", "50", ".")])
        for name in ("s.vcf.gz", "s_sv.vcf.gz", "s.cnv.vcf.gz")
    }
    out = tmp_path / "out"
    run = fake.run("--input", bam, "--vcf-inputs", *vcfs.values(), "--outdir", str(out))

    assert "snp" not in run.ran and "indel" not in run.ran
    # contract: each variant type gets the annotations that score it.
    assert fake.annotated() == {
        vcfs["s.vcf.gz"]: "clinvar,revel,phylop,cadd,gerp,gnomad,spliceai,"
        "alphamissense,pharmgkb,vep",
        vcfs["s_sv.vcf.gz"]: "clinvar,gnomad",
        vcfs["s.cnv.vcf.gz"]: "clinvar,gnomad",
    }
    assert sorted(p.name for p in out.glob("significant_*")) == [
        "significant_cnv_s.cnv.vcf.gz",
        "significant_cnv_s.cnv.vcf.gz.tbi",
        "significant_snp-indel_s.confidence.tsv",
        "significant_snp-indel_s.vcf.gz",
        "significant_snp-indel_s.vcf.gz.tbi",
        "significant_sv_s_sv.vcf.gz",
        "significant_sv_s_sv.vcf.gz.tbi",
    ]


@needs_bcftools
def test_only_installed_annotations_are_requested(
    fake: FakeSteps, tmp_path: Path
) -> None:
    fake.install_only("clinvar", "gnomad")
    vcf = write_vcf(tmp_path / "s.vcf.gz", [(100, "x", "50", ".")])
    fake.run("--vcf-inputs", vcf, "--outdir", str(tmp_path / "out"))
    # contract: an annotation without its data isn't asked for.
    assert fake.annotated() == {vcf: "clinvar,gnomad"}


@needs_bcftools
def test_no_annotation_data_skips_discovery_instead_of_reporting_zero(
    fake: FakeSteps, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.install_only()
    vcf = write_vcf(tmp_path / "s.vcf.gz", [(100, "x", "50", ".")])
    out = tmp_path / "out"
    run = fake.run("--vcf-inputs", vcf, "--outdir", str(out))
    assert run.code == 0 and fake.calls == []
    # Nothing but the step journal and its hidden lock.
    shown = [p.name for p in out.iterdir() if not p.name.startswith(".")]
    assert shown == ["analyze.manifest.json"]
    assert "TOTAL SIGNIFICANT VARIANTS FOUND: 0" in capsys.readouterr().out


@needs_bcftools
def test_annotation_that_writes_nothing_is_left_out_of_the_report(
    fake: FakeSteps, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    del fake.outputs["vcf chain-annotate"]
    vcf = write_vcf(tmp_path / "s.vcf.gz", [(100, "x", "50", ".")])
    run = fake.run("--vcf-inputs", vcf, "--outdir", str(tmp_path / "out"))
    assert run.ran == ["annotate:snp-indel_s"]
    assert not list((tmp_path / "out").glob("significant_*"))
    report = capsys.readouterr().out
    assert "[SNP-INDEL]" not in report
    assert "TOTAL SIGNIFICANT VARIANTS FOUND: 0" in report


@needs_bcftools
def test_confidence_thresholds_given_on_the_command_line_judge_discovery(
    fake: FakeSteps, tmp_path: Path
) -> None:
    plain = tmp_path / "annotated.vcf"
    plain.write_text(
        "##fileformat=VCFv4.2\n##contig=<ID=chr1,length=10000>\n"
        f"{_INFO_HEADERS['CLNSIG']}\n"
        '##FORMAT=<ID=GT,Number=1,Type=String,Description="g">\n'
        '##FORMAT=<ID=AD,Number=R,Type=Integer,Description="a">\n'
        '##FORMAT=<ID=DP,Number=1,Type=Integer,Description="d">\n'
        '##FORMAT=<ID=GQ,Number=1,Type=Integer,Description="q">\n'
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS\n"
        "chr1\t100\tp\tA\tG\t50\tPASS\tCLNSIG=Pathogenic\t"
        "GT:AD:DP:GQ\t0/1:7,7:14:99\n"
    )
    fake.outputs["vcf chain-annotate"] = bgzip_index(plain, tmp_path / "ann.vcf.gz")
    vcf = write_vcf(tmp_path / "s.vcf.gz", [(100, "x", "50", ".")])
    out = tmp_path / "out"
    # 14 reads: moderate at the default depth thresholds (10, 20).
    fake.run("--vcf-inputs", vcf, "--outdir", str(out),
             "--confidence-depth", "5", "10")  # fmt: skip
    table = (out / "significant_snp-indel_s.confidence.tsv").read_text()
    row = next(line for line in table.splitlines() if line.startswith("chr1"))
    assert "\thigh\t" in row


@pytest.mark.parametrize(
    ("failing", "step"),
    [("info", "info"), ("lineage mt-haplogroup", "mt-haplogroup"),
     ("vcf snp", "snp")],
)  # fmt: skip
def test_failing_step_stops_the_pipeline_and_names_the_command(
    fake: FakeSteps, bam: str, tmp_path: Path, failing: str, step: str
) -> None:
    fake.fail = {failing}
    run = fake.run("--input", bam, "--outdir", str(tmp_path / "o"))
    assert run.code == 1
    assert re.search(f"Subcommand failed: .* {failing} ", run.error)
    assert run.ran[-1] == step


# --- analyze comprehensive: resuming ---------------------------------------------


def _significant(out: Path) -> dict[str, list[str]]:
    return {p.name: ids(p) for p in sorted(out.glob("significant_*.vcf.gz"))}


def _summary(text: str, out: Path) -> str:
    return text.split("GENOMIC ANALYSIS SUMMARY")[1].replace(str(out), "")


@needs_bcftools
def test_a_killed_run_resumes_at_the_step_it_was_in(
    fake: FakeSteps, bam: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clean = tmp_path / "clean"
    fake.run("--input", bam, "--outdir", str(clean))
    clean_report = _summary(capsys.readouterr().out, clean)

    out = tmp_path / "out"
    fake.die = "vcf indel"
    run = fake.run("--input", bam, "--outdir", str(out))
    assert run.code != 0
    assert run.ran == ["info", "mt-haplogroup", "y-haplogroup", "snp", "indel"]
    capsys.readouterr()

    fake.die = None
    run = fake.run("--input", bam, "--outdir", str(out))
    # The finished steps are skipped; the half-written indels are redone.
    assert run.ran == _ALL_STEPS[4:]
    assert _significant(out) == _significant(clean)
    assert _summary(capsys.readouterr().out, out) == clean_report

    run = fake.run("--input", bam, "--outdir", str(out))
    assert run.ran == [] and fake.calls == []
    assert _significant(out) == _significant(clean)


@needs_bcftools
def test_a_rerun_waits_for_a_killed_runs_tools_and_never_keeps_their_output(
    fake: FakeSteps, bam: str, tmp_path: Path
) -> None:
    clean = tmp_path / "clean"
    fake.run("--input", bam, "--outdir", str(clean))
    out = tmp_path / "out"
    indels = out / "indels.vcf.gz"
    # The caller's tool runs in its own session, so it outlives the step; the
    # kill test in test_realign covers the kill itself.
    fake.die, fake.writer = "vcf indel", True
    # Like callers that keep an existing output.
    fake.skip_existing = {"vcf indel"}
    writer = None
    try:
        assert fake.run("--input", bam, "--outdir", str(out)).code != 0
        writer = psutil.Process(int((fake.home / "writer.pid").read_text()))
        fake.die = None
        run = fake.run("--input", bam, "--outdir", str(out))
        assert run.code != 0
        assert re.search(f"still writing.*pid {writer.pid}", run.error)
        assert fake.calls == []
    finally:
        if writer:
            writer.kill()
            writer.wait()

    run = fake.run("--input", bam, "--outdir", str(out))
    assert run.ran == _ALL_STEPS[4:]
    assert ids(indels) == ids(clean / "indels.vcf.gz")
    assert _significant(out) == _significant(clean)
    assert not list(out.glob(".wgse-step-*"))


@needs_bcftools
def test_changed_inputs_settings_or_outputs_redo_only_the_steps_they_affect(
    fake: FakeSteps, bam: str, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    fake.run("--input", bam, "--outdir", str(out))

    # A truncated call set is redone, with what was made from it.
    snps = out / "snps.vcf.gz"
    snps.write_bytes(snps.read_bytes()[:20])
    run = fake.run("--input", bam, "--outdir", str(out))
    assert run.ran == ["snp", "annotate:snp-indel_snps", "discovery:snp-indel_snps"]
    assert list(fake.annotated()) == [str(snps)]

    # New thresholds only refilter.
    run = fake.run("--input", bam, "--outdir", str(out),
                   "--confidence-depth", "5", "10")  # fmt: skip
    assert run.ran == ["discovery:snp-indel_snps", "discovery:snp-indel_indels"]
    assert fake.calls == []

    # A different input file redoes everything that read it.
    _bam(Path(bam), sample="another sample")
    run = fake.run("--input", bam, "--outdir", str(out),
                   "--confidence-depth", "5", "10")  # fmt: skip
    assert run.ran == _ALL_STEPS


@needs_bcftools
def test_updated_reference_or_annotation_data_redo_the_steps_that_read_them(
    fake: FakeSteps, bam: str, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    fasta = fake.reference = tmp_path / "ref.fa"
    fasta.write_text(">chr1\nACGT\n")
    fake.run("--input", bam, "--outdir", str(out))

    Path(str(fake.settings["clinvar_vcf_path"])).write_text("clinvar release 2")
    run = fake.run("--input", bam, "--outdir", str(out))
    assert [s for s in run.ran if not s.startswith("discovery")] == [
        "annotate:snp-indel_snps", "annotate:snp-indel_indels"
    ]  # fmt: skip

    fasta.write_text(">chr1\nACGA\n")
    run = fake.run("--input", bam, "--outdir", str(out))
    assert [s for s in run.ran if not s.startswith("discovery")] == [
        "snp", "annotate:snp-indel_snps"
    ]  # fmt: skip


@needs_bcftools
def test_dry_run_plans_and_forced_steps_rerun_with_what_uses_them(
    fake: FakeSteps, bam: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "out"
    fake.run("--input", bam, "--outdir", str(out), "--dry-run")
    assert fake.calls == [] and not (out / wf.JOURNAL).exists()
    plan = capsys.readouterr().out
    assert "info" in plan and "annotate:snp-indel_snps" in plan
    assert "not run before" in plan

    fake.run("--input", bam, "--outdir", str(out))
    capsys.readouterr()
    fake.run("--input", bam, "--outdir", str(out), "--force-step", "snp",
             "--dry-run")  # fmt: skip
    plan = capsys.readouterr().out
    assert fake.calls == []
    assert re.search(
        r"discovery:snp-indel_snps +run +step annotate:snp-indel_snps", plan
    )
    assert re.search(r"discovery:snp-indel_indels +skip +complete", plan)

    run = fake.run("--input", bam, "--outdir", str(out), "--force-step", "annotate")
    assert run.ran == _ALL_STEPS[5:]
    run = fake.run("--input", bam, "--outdir", str(out), "--force")
    assert run.ran == _ALL_STEPS


def test_missing_inputs_are_reported_before_anything_runs(
    fake: FakeSteps, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    missing = str(tmp_path / "nope.bam")
    with pytest.raises(WGSExtractError, match="nope.bam") as err:
        run_wgsextract("analyze", "comprehensive", "--input", missing,
                       "--vcf-inputs", str(tmp_path), "--outdir", str(out))  # fmt: skip
    assert "directory" in str(err.value)
    assert fake.calls == [] and not out.exists()


def test_no_input_and_no_vcf_is_an_error(fake: FakeSteps, tmp_path: Path) -> None:
    run = fake.run("--outdir", str(tmp_path / "out"))
    assert run.code != 0
    assert fake.calls == []


@needs_bcftools
def test_configured_vcf_input_paths_are_used_without_vcf_inputs(
    fake: FakeSteps, tmp_path: Path
) -> None:
    a = write_vcf(tmp_path / "a.vcf.gz", [(100, "x", "50", ".")])
    b = write_vcf(tmp_path / "b_sv.vcf.gz", [(100, "x", "50", ".")])
    fake.settings["vcf_input_paths"] = f"{a} {b}"
    out = tmp_path / "out"
    fake.run("--outdir", str(out))
    assert sorted(p.name for p in out.glob("significant_*.vcf.gz")) == [
        "significant_snp-indel_a.vcf.gz",
        "significant_sv_b_sv.vcf.gz",
    ]


@needs_bcftools
def test_vcfs_sharing_a_stem_get_separate_outputs(
    fake: FakeSteps, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vcfs = [
        write_vcf(tmp_path / n, [(100, "x", "50", ".")])
        for n in ("NA12878.snp.vcf.gz", "NA12878.indel.vcf.gz")
    ]
    fake.run("--vcf-inputs", *vcfs, "--outdir", str(tmp_path / "out"))
    shown = re.findall(r"Results file: (\S+)", capsys.readouterr().out)
    assert len(shown) == 2 and len(set(shown)) == 2
    assert all(Path(path).is_file() for path in shown)


# --- discovery filter -----------------------------------------------------------


@needs_bcftools
@pytest.mark.parametrize(
    ("v_type", "kept"),
    [
        ("snp-indel", ["rare", "nofreq", "patho", "revel"]),
        # REVEL scores missense SNVs; it plays no part for SVs and CNVs.
        ("sv", ["rare", "nofreq", "patho"]),
    ],
)
def test_discovery_keeps_rare_pathogenic_or_damaging_variants(
    tmp_path: Path, v_type: str, kept: list[str]
) -> None:
    ann = write_vcf(
        tmp_path / "ann.vcf.gz",
        [
            (100, "rare", "50", "GNOMAD_AF=0.001"),
            (200, "nofreq", "50", "."),
            (300, "common", "50", "GNOMAD_AF=0.3;CLNSIG=Benign;REVEL=0.2"),
            (400, "patho", "50", "GNOMAD_AF=0.3;CLNSIG=Pathogenic"),
            (500, "revel", "50", "GNOMAD_AF=0.3;REVEL=0.9"),
        ],
        ["GNOMAD_AF", "CLNSIG", "REVEL"],
    )
    out = str(tmp_path / "sig.vcf.gz")
    assert wf.run_discovery_filter(ann, v_type, out) == len(kept)
    assert ids(out) == kept
    assert Path(out + ".tbi").exists()


@needs_bcftools
def test_discovery_without_annotations_falls_back_to_quality(tmp_path: Path) -> None:
    ann = write_vcf(
        tmp_path / "ann.vcf.gz", [(100, "low", "10", "."), (200, "high", "60", ".")]
    )
    out = str(tmp_path / "sig.vcf.gz")
    assert wf.run_discovery_filter(ann, "cnv", out) == 1
    assert ids(out) == ["high"]


@needs_bcftools
def test_discovery_reports_zero_for_no_matches(tmp_path: Path) -> None:
    ann = write_vcf(
        tmp_path / "ann.vcf.gz", [(100, "c", "50", "GNOMAD_AF=0.5")], ["GNOMAD_AF"]
    )
    assert wf.run_discovery_filter(ann, "snp-indel", str(tmp_path / "s.vcf.gz")) == 0


def test_discovery_failure_is_a_wgsextract_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.vcf.gz"
    bad.write_text("not a vcf")
    with pytest.raises(WGSExtractError, match="Discovery filter failed for sv"):
        wf.run_discovery_filter(str(bad), "sv", str(tmp_path / "out.vcf.gz"))


@needs_bcftools
def test_discovery_ignores_a_format_af_tag(tmp_path: Path) -> None:
    ann = write_vcf(
        tmp_path / "ann.vcf.gz",
        [
            (100, "patho", "50", "CLNSIG=Pathogenic"),
            (200, "benign", "50", "CLNSIG=Benign"),
        ],
        ["CLNSIG"],
        header='##FORMAT=<ID=AF,Number=A,Type=Float,Description="VAF">',
    )
    out = str(tmp_path / "sig.vcf.gz")
    assert wf.run_discovery_filter(ann, "snp-indel", out) == 1


# --- child subcommands -------------------------------------------------------------


@pytest.mark.parametrize(
    "keep_awake,configured,flag",
    [
        (None, True, "--keep-awake"),  # the configured setting, made explicit
        (True, True, "--keep-awake"),
        (False, True, "--no-keep-awake"),
        (False, False, None),
    ],
)
def test_subcommand_forwards_ref_threads_debug_and_config(
    fake: FakeSteps,
    bam: str,
    tmp_path: Path,
    keep_awake: bool | None,
    configured: bool,
    flag: str | None,
) -> None:
    own = tmp_path / "own.toml"
    own.write_text(f"keep_awake = {str(configured).lower()}\n")
    sleep = {None: [], True: ["--keep-awake"], False: ["--no-keep-awake"]}[keep_awake]
    fake.prepare()
    main(["--config", str(own), *sleep, "analyze", "comprehensive", "--input", bam,
          "--outdir", str(tmp_path / "out"), "--ref", str(tmp_path), "--threads", "2",
          "--debug", "--force", "--skip-calling"])  # fmt: skip
    info = fake.calls[0]
    # contract: each step runs with the run's config, sleep override,
    # reference, threads and debug. --force reruns the steps; the steps
    # themselves have no --force, so it is not passed on.
    assert info[: info.index("info")] == [
        "--config",
        str(own),
        *([flag] if flag else []),
    ]
    assert info[info.index("--ref") :][:6] == [
        "--ref", str(tmp_path), "--threads", "2", "--debug", "--result-file"
    ]  # fmt: skip


def test_subcommand_without_ref_uses_the_configured_library(
    fake: FakeSteps, bam: str, tmp_path: Path
) -> None:
    fake.settings["reference_library"] = str(tmp_path)
    fake.run("--input", bam, "--outdir", str(tmp_path / "a"), "--skip-calling")
    assert fake.calls[0][fake.calls[0].index("--ref") + 1] == str(tmp_path)
    del fake.settings["reference_library"]
    fake.run("--input", bam, "--outdir", str(tmp_path / "b"), "--skip-calling")
    assert "--ref" not in fake.calls[0]


def test_console_text_survives_a_narrow_stdout_encoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Ascii:
        encoding = "ascii"

    monkeypatch.setattr(sys, "stdout", Ascii())
    assert wf._safe_console_text("Sample Zoë") == "Sample Zo?"
    assert wf._safe_console_text("plain") == "plain"


# --- end to end: real chain-annotate subprocess ----------------------------------


@needs_bcftools
def test_vcf_input_end_to_end_with_clinvar(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Annotate with a tiny ClinVar through the real `vcf chain-annotate`."""
    clinvar = write_vcf(
        tmp_path / "clinvar.vcf.gz",
        [(100, "cv1", ".", "CLNSIG=Pathogenic"), (200, "cv2", ".", "CLNSIG=Benign")],
        ["CLNSIG"],
        header='##INFO=<ID=CLNDN,Number=.,Type=String,Description="d">',
    )
    lib = tmp_path / "lib"
    lib.mkdir()
    cfg = tmp_path / "config.toml"
    # Literal strings: TOML would read a Windows path's backslashes as escapes.
    cfg.write_text(
        f"reference_library = '{lib}'\nclinvar_vcf_path = '{clinvar}'\n"
        + state_directory_setting(tmp_path)
    )

    calls = write_vcf(
        tmp_path / "me.vcf.gz",
        [(100, "a", "50", "."), (200, "b", "50", "."), (300, "c", "50", ".")],
    )
    out = tmp_path / "out"
    main(["--config", str(cfg), "analyze", "comprehensive", "--vcf-inputs", calls,
          "--outdir", str(out), "--threads", "1"])  # fmt: skip

    sig = out / "significant_snp-indel_me.vcf.gz"
    assert ids(sig) == ["a"]
    assert "TOTAL SIGNIFICANT VARIANTS FOUND: 1" in capsys.readouterr().out


# --- batch ----------------------------------------------------------------------


def write_batch(path: Path, rows: list[list[str]], delimiter: str = ",") -> str:
    with open(path, "w", newline="") as f:
        csv.writer(f, delimiter=delimiter).writerows(rows)
    return str(path)


def analysed(out: Path) -> set[str]:
    """The sample folders of ``out`` holding an alignment's metrics."""
    return {p.parent.name for p in out.glob("*/*.wgse_info.json")}


def test_batch_runs_each_sample_in_its_own_directory(
    fake: FakeSteps, bam: str, tmp_path: Path
) -> None:
    other = _bam(tmp_path / "other.bam")
    batch = write_batch(
        tmp_path / "b.csv",
        [["name", "input", "vcf"], ["A", bam, ""], ["B", other, ""]],
    )
    out = tmp_path / "out"
    fake.run("--batch", batch, "--outdir", str(out), "--skip-calling")
    assert (out / "A" / "sample.bam.wgse_info.json").exists()
    assert (out / "B" / "other.bam.wgse_info.json").exists()


@needs_bcftools
def test_batch_tsv_with_alternate_headers_and_comma_separated_vcfs(
    fake: FakeSteps, tmp_path: Path
) -> None:
    a = write_vcf(tmp_path / "a.vcf.gz", [(100, "x", "50", ".")])
    b = write_vcf(tmp_path / "a_sv.vcf.gz", [(100, "x", "50", ".")])
    batch = write_batch(
        tmp_path / "b.tsv", [["Sample", "BAM", "VCFs"], ["S1", "", f"{a}, {b}"]], "\t"
    )
    out = tmp_path / "out"
    fake.run("--batch", batch, "--outdir", str(out))
    assert sorted(p.name for p in (out / "S1").glob("significant_*.vcf.gz")) == [
        "significant_snp-indel_a.vcf.gz",
        "significant_sv_a_sv.vcf.gz",
    ]


def test_batch_failing_sample_does_not_stop_the_others(
    fake: FakeSteps, bam: str, tmp_path: Path
) -> None:
    batch = write_batch(
        tmp_path / "b.csv",
        [
            ["name", "input", "vcf"],
            ["gone", str(tmp_path / "missing.bam"), ""],
            ["empty", "", ""],
            ["ok", bam, ""],
        ],
    )
    out = tmp_path / "out"
    run = fake.run("--batch", batch, "--outdir", str(out), "--skip-calling")
    assert run.code == 1
    assert run.error.endswith("sample(s): gone, empty")
    assert analysed(out) == {"ok"}


def test_batch_file_must_exist(fake: FakeSteps, tmp_path: Path) -> None:
    run = fake.run("--batch", str(tmp_path / "none.csv"))
    assert run.code != 0 and "Batch file not found" in run.error


@needs_bcftools
def test_batch_gen_output_drives_a_batch_run(fake: FakeSteps, tmp_path: Path) -> None:
    scan = tmp_path / "scan"
    alice = _bam(scan / "a" / "alice.bam")
    run_command([get_tool_path("samtools"), "index", alice])
    write_vcf(scan / "a" / "alice.snp.vcf.gz", [(100, "x", "50", ".")])
    write_vcf(scan / "a" / "alice_SV.vcf.gz", [(100, "x", "50", ".")])
    (scan / "b").mkdir()
    write_vcf(scan / "b" / "bob.vcf.gz", [(100, "x", "50", ".")])
    Path(scan / "b" / "notes.txt").write_text("x")
    batch = tmp_path / "batch.csv"

    run_wgsextract(
        "analyze", "batch-gen", "--directory", str(scan), "--output", str(batch)
    )
    rows = list(csv.DictReader(batch.open()))
    assert [r["name"] for r in rows] == ["alice", "bob"]
    assert rows[0]["input"] == alice
    assert sorted(rows[0]["vcf"].split(";")) == [
        str(scan / "a" / "alice.snp.vcf.gz"),
        str(scan / "a" / "alice_SV.vcf.gz"),
    ]
    assert rows[1] == {
        "name": "bob",
        "input": "",
        "vcf": str(scan / "b" / "bob.vcf.gz"),
    }

    out = tmp_path / "out"
    fake.run("--batch", str(batch), "--outdir", str(out))
    assert analysed(out) == {"alice"}
    assert sorted(p.relative_to(out).as_posix()
                  for p in out.glob("*/significant_*.vcf.gz")) == [
        "alice/significant_snp-indel_alice.snp.vcf.gz",
        "alice/significant_sv_alice_SV.vcf.gz",
        "bob/significant_snp-indel_bob.vcf.gz",
    ]  # fmt: skip


def test_batch_gen_requires_an_existing_directory(tmp_path: Path) -> None:
    with pytest.raises(WGSExtractError, match="Directory not found"):
        run_wgsextract("analyze", "batch-gen", "--directory", str(tmp_path / "x"))


def test_configured_batch_file_runs_each_sample_once(
    fake: FakeSteps, bam: str, tmp_path: Path
) -> None:
    fake.settings["batch_file_path"] = write_batch(
        tmp_path / "b.csv", [["name", "input", "vcf"], ["A", bam, ""]]
    )
    out = tmp_path / "out"
    fake.run("--outdir", str(out), "--skip-calling")
    assert analysed(out) == {"A"}
    assert [c[0] for c in fake.calls].count("info") == 1


@needs_bcftools
def test_batch_sample_without_vcfs_ignores_configured_vcf_input_paths(
    fake: FakeSteps, bam: str, tmp_path: Path
) -> None:
    fake.settings["vcf_input_paths"] = write_vcf(
        tmp_path / "x.vcf.gz", [(1, "x", "50", ".")]
    )
    batch = write_batch(tmp_path / "b.csv", [["name", "input", "vcf"], ["A", bam, ""]])
    out = tmp_path / "out"
    fake.run("--batch", batch, "--outdir", str(out), "--skip-calling")
    assert analysed(out) == {"A"}
    assert not list(out.glob("**/significant_*"))


def test_batch_sample_names_stay_inside_outdir_and_blank_names_differ(
    fake: FakeSteps, bam: str, tmp_path: Path
) -> None:
    batch = tmp_path / "batch.csv"
    batch.write_text(f"name,input\n../escape,{bam}\n,{bam}\n,{bam}\n")
    out = tmp_path / "out"
    run = fake.run("--batch", str(batch), "--outdir", str(out))
    assert run.code == 1 and "../escape" in run.error
    assert not (tmp_path / "escape").exists()
    assert sorted(p.name for p in out.iterdir() if p.is_dir()) == ["sample2", "sample3"]


def test_batch_gen_refuses_two_alignments_with_one_sample_name(tmp_path: Path) -> None:
    for d in ("d1", "d2"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "child.bam").write_text("x")
    (tmp_path / "d1" / "significant_snp-indel_child.vcf.gz").write_text("x")
    with pytest.raises(WGSExtractError, match="would both be sample child"):
        run_wgsextract(
            "analyze",
            "batch-gen",
            "--directory",
            str(tmp_path),
            "--output",
            str(tmp_path / "b.csv"),
        )
