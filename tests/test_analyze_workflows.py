"""In-process tests for `analyze comprehensive` and `analyze batch-gen`.

Subcommands the pipeline shells out to (info, lineage, vcf snp/indel,
chain-annotate) are replaced by a recorder that writes the files they would
write; the discovery filter and batch handling run for real.
"""

import argparse
import csv
import json
import shutil
from collections.abc import Iterable
from pathlib import Path

import pytest

from tests.htslib_helpers import bgzip_index, needs_tools, run_wgsextract
from wgsextract_cli.commands import _analyze_workflows as wf
from wgsextract_cli.commands import analyze
from wgsextract_cli.commands.vcf import chain
from wgsextract_cli.core import config
from wgsextract_cli.core.config import reload_settings, settings
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.utils import WGSExtractError, run_command

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


def ids(vcf: str) -> list[str]:
    out: str = run_command(
        [get_tool_path("bcftools"), "query", "-f", "%ID\n", vcf], capture_output=True
    ).stdout
    return out.split()


# Annotated output the fake chain-annotate writes: id1 rare, id2 common and
# benign, id3 common but pathogenic.
_ANNOTATED = [
    (100, "rare", "50", "GNOMAD_AF=0.001"),
    (200, "common", "50", "GNOMAD_AF=0.3;CLNSIG=Benign"),
    (300, "patho", "50", "GNOMAD_AF=0.3;CLNSIG=Pathogenic"),
]


class FakeSubcommands:
    """Records each `run_cli_subcommand` call and writes what it would write."""

    def __init__(self, gender: str = "Male", fail: Iterable[str] = ()) -> None:
        self.calls: list[list[str]] = []
        self.gender = gender
        self.fail = set(fail)

    @staticmethod
    def name(cmd: list[str]) -> str:
        return " ".join(cmd[:2]) if cmd[0] in ("lineage", "vcf") else cmd[0]

    def __call__(self, cmd: list[str], args: argparse.Namespace) -> None:
        self.calls.append(cmd)
        name = self.name(cmd)
        if name in self.fail:
            raise WGSExtractError("Subcommand failed: " + " ".join(cmd))
        opt = dict(zip(cmd, cmd[1:], strict=False))
        outdir = Path(opt["--outdir"])
        if name == "info":
            cache = outdir / f"{Path(opt['--input']).name}.wgse_info.json"
            cache.write_text(json.dumps({"gender": self.gender}))
        elif name == "vcf snp":
            write_vcf(outdir / "snps.vcf.gz", [(100, "s", "50", ".")])
        elif name == "vcf indel":
            write_vcf(outdir / "indels.vcf.gz", [(100, "i", "50", ".")])
        elif name == "vcf chain-annotate":
            write_vcf(
                outdir / "chain_annotated.vcf.gz",
                _ANNOTATED,
                ["GNOMAD_AF", "CLNSIG"],
            )

    def names(self) -> list[str]:
        return [self.name(c) for c in self.calls]

    def annotated(self) -> dict[str, str]:
        """chain-annotate --input -> --annotations."""
        return {
            c[c.index("--input") + 1]: c[c.index("--annotations") + 1]
            for c in self.calls
            if self.name(c) == "vcf chain-annotate"
        }


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeSubcommands:
    rec = FakeSubcommands()
    monkeypatch.setattr(wf, "run_cli_subcommand", rec)
    monkeypatch.setattr(analyze, "run_cli_subcommand", rec)
    # Every annotation's data is "installed".
    monkeypatch.setattr(chain, "skip_reason", lambda ann, lib: None)
    return rec


@pytest.fixture
def bam(tmp_path: Path) -> str:
    """A path that exists; the fake subcommands never read it."""
    path = tmp_path / "in" / "sample.bam"
    path.parent.mkdir()
    path.write_bytes(b"")
    return str(path)


def comprehensive(*argv: str) -> None:
    run_wgsextract("analyze", "comprehensive", *argv)


# --- analyze comprehensive: orchestration ---------------------------------------


@needs_bcftools
def test_bam_only_runs_metrics_lineage_calling_then_annotates_both_call_sets(
    fake: FakeSubcommands, bam: str, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    out = tmp_path / "out"
    comprehensive("--input", bam, "--outdir", str(out))

    assert fake.names() == [
        "info",
        "lineage mt-haplogroup",
        "lineage y-haplogroup",
        "vcf snp",
        "vcf indel",
        "vcf chain-annotate",
        "vcf chain-annotate",
    ]
    assert all(c[c.index("--input") + 1] == bam for c in fake.calls[:5])
    assert list(fake.annotated()) == [
        str(out / "snps.vcf.gz"),
        str(out / "indels.vcf.gz"),
    ]
    # Annotated per call set in its own work dir; rare + pathogenic kept.
    for base in ("snps", "indels"):
        sig = out / f"significant_snp-indel_{base}.vcf.gz"
        assert ids(str(sig)) == ["rare", "patho"]
        assert (out / f"work_snp-indel_{base}" / "chain_annotated.vcf.gz").exists()
    report = capsys.readouterr().out
    assert "TOTAL SIGNIFICANT VARIANTS FOUND: 4" in report
    assert "[SNP-INDEL] snps.vcf.gz" in report


def test_female_genome_skips_y_lineage(
    fake: FakeSubcommands, bam: str, tmp_path: Path
) -> None:
    fake.gender = "Female"
    comprehensive("--input", bam, "--outdir", str(tmp_path), "--skip-calling")
    assert fake.names() == ["info", "lineage mt-haplogroup"]


def test_unreadable_info_cache_still_runs_y_lineage(
    fake: FakeSubcommands, bam: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_info(cmd: list[str], args: argparse.Namespace) -> None:
        fake.calls.append(cmd)
        if cmd[0] == "info":
            Path(tmp_path, "sample.bam.wgse_info.json").write_text("{not json")

    monkeypatch.setattr(wf, "run_cli_subcommand", broken_info)
    comprehensive("--input", bam, "--outdir", str(tmp_path), "--skip-calling")
    assert fake.names() == ["info", "lineage mt-haplogroup", "lineage y-haplogroup"]


@needs_bcftools
def test_vcf_inputs_replace_calling_and_get_annotations_for_their_type(
    fake: FakeSubcommands, bam: str, tmp_path: Path
) -> None:
    vcfs = {
        name: write_vcf(tmp_path / name, [(100, "x", "50", ".")])
        for name in ("s.vcf.gz", "s_sv.vcf.gz", "s.cnv.vcf.gz")
    }
    out = tmp_path / "out"
    comprehensive("--input", bam, "--vcf-inputs", *vcfs.values(), "--outdir", str(out))

    assert "vcf snp" not in fake.names() and "vcf indel" not in fake.names()
    assert fake.annotated() == {
        vcfs["s.vcf.gz"]: "clinvar,revel,phylop,cadd,gerp,gnomad,spliceai,"
        "alphamissense,pharmgkb,vep",
        vcfs["s_sv.vcf.gz"]: "clinvar,gnomad",
        vcfs["s.cnv.vcf.gz"]: "clinvar,gnomad",
    }
    assert sorted(p.name for p in out.glob("significant_*")) == [
        "significant_cnv_s.cnv.vcf.gz",
        "significant_cnv_s.cnv.vcf.gz.tbi",
        "significant_snp-indel_s.vcf.gz",
        "significant_snp-indel_s.vcf.gz.tbi",
        "significant_sv_s_sv.vcf.gz",
        "significant_sv_s_sv.vcf.gz.tbi",
    ]


@needs_bcftools
def test_only_installed_annotations_are_requested(
    fake: FakeSubcommands, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed = {"clinvar", "gnomad"}
    monkeypatch.setattr(
        chain, "skip_reason", lambda ann, lib: None if ann in installed else "no"
    )
    vcf = write_vcf(tmp_path / "s.vcf.gz", [(100, "x", "50", ".")])
    comprehensive("--vcf-inputs", vcf, "--outdir", str(tmp_path / "out"))
    assert fake.annotated() == {vcf: "clinvar,gnomad"}


@needs_bcftools
def test_no_annotation_data_skips_discovery_instead_of_reporting_zero(
    fake: FakeSubcommands,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.setattr(chain, "skip_reason", lambda ann, lib: "not installed")
    vcf = write_vcf(tmp_path / "s.vcf.gz", [(100, "x", "50", ".")])
    out = tmp_path / "out"
    comprehensive("--vcf-inputs", vcf, "--outdir", str(out))
    assert fake.calls == []
    assert sorted(p.name for p in out.iterdir()) == []
    assert "TOTAL SIGNIFICANT VARIANTS FOUND: 0" in capsys.readouterr().out


@needs_bcftools
def test_annotation_that_writes_nothing_is_left_out_of_the_report(
    fake: FakeSubcommands,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    def quiet(cmd: list[str], args: argparse.Namespace) -> None:
        fake.calls.append(cmd)

    monkeypatch.setattr(wf, "run_cli_subcommand", quiet)
    vcf = write_vcf(tmp_path / "s.vcf.gz", [(100, "x", "50", ".")])
    comprehensive("--vcf-inputs", vcf, "--outdir", str(tmp_path / "out"))
    assert fake.names() == ["vcf chain-annotate"]
    assert not list((tmp_path / "out").glob("significant_*"))
    report = capsys.readouterr().out
    assert "[SNP-INDEL]" not in report
    assert "TOTAL SIGNIFICANT VARIANTS FOUND: 0" in report


@pytest.mark.parametrize("failing", ["info", "lineage mt-haplogroup", "vcf snp"])
def test_failing_step_stops_the_pipeline_and_names_the_command(
    fake: FakeSubcommands, bam: str, tmp_path: Path, failing: str
) -> None:
    fake.fail = {failing}
    with pytest.raises(WGSExtractError, match=f"Subcommand failed: {failing}"):
        comprehensive("--input", bam, "--outdir", str(tmp_path))
    assert fake.names()[-1] == failing


def test_missing_inputs_are_reported_before_anything_runs(
    fake: FakeSubcommands, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    missing = str(tmp_path / "nope.bam")
    with pytest.raises(WGSExtractError, match="nope.bam") as err:
        comprehensive(
            "--input", missing, "--vcf-inputs", str(tmp_path), "--outdir", str(out)
        )
    assert "directory" in str(err.value)
    assert fake.calls == [] and not out.exists()


def test_no_input_and_no_vcf_is_an_error(fake: FakeSubcommands) -> None:
    with pytest.raises(WGSExtractError):
        comprehensive()
    assert fake.calls == []


@needs_bcftools
def test_configured_vcf_input_paths_are_used_without_vcf_inputs(
    fake: FakeSubcommands, tmp_path: Path
) -> None:
    a = write_vcf(tmp_path / "a.vcf.gz", [(100, "x", "50", ".")])
    b = write_vcf(tmp_path / "b_sv.vcf.gz", [(100, "x", "50", ".")])
    settings["vcf_input_paths"] = f"{a} {b}"
    comprehensive("--outdir", str(tmp_path / "out"))
    assert list(fake.annotated()) == [a, b]


@needs_bcftools
def test_vcfs_sharing_a_stem_get_separate_outputs(
    fake: FakeSubcommands, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    results: list[wf.AnalyzeResult] = []
    monkeypatch.setattr(
        analyze, "generate_summary_report", lambda r, o: results.extend(r)
    )
    vcfs = [
        write_vcf(tmp_path / n, [(100, "x", "50", ".")])
        for n in ("NA12878.snp.vcf.gz", "NA12878.indel.vcf.gz")
    ]
    comprehensive("--vcf-inputs", *vcfs, "--outdir", str(tmp_path / "out"))
    assert len({r["output"] for r in results}) == 2


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


# --- run_cli_subcommand ---------------------------------------------------------


def _args(**kw: object) -> argparse.Namespace:
    base = {"ref": None, "input": None, "threads": None, "debug": False}
    return argparse.Namespace(**{**base, **kw})


@pytest.mark.parametrize(
    "keep_awake,configured,flag",
    [
        (None, True, None),
        (True, True, "--keep-awake"),
        (False, True, "--no-keep-awake"),
        (False, False, None),
    ],
)
def test_subcommand_forwards_ref_threads_debug_force_and_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    keep_awake: bool | None,
    configured: bool,
    flag: str | None,
) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(wf, "run_command", lambda cmd: seen.append(cmd))
    own = tmp_path / "own.toml"
    monkeypatch.setattr(config, "_config_path_override", own)
    monkeypatch.setitem(config.settings, "keep_awake", configured)
    args = _args(ref="/refs", threads=2, debug=True, force=True, keep_awake=keep_awake)
    wf.run_cli_subcommand(["info", "--input", "x.bam"], args)
    assert seen[0][1:] == [
        "-m",
        "wgsextract_cli.main",
        "--config",
        str(own),
        *([flag] if flag else []),
        "info",
        "--input",
        "x.bam",
        "--ref",
        "/refs",
        "--threads",
        "2",
        "--debug",
        "--force",
    ]


def test_subcommand_without_ref_uses_the_configured_library(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(wf, "run_command", lambda cmd: seen.append(cmd))
    settings["reference_library"] = str(tmp_path)
    wf.run_cli_subcommand(["vcf", "snp"], _args(input="calls.vcf.gz"))
    del settings["reference_library"]
    wf.run_cli_subcommand(["vcf", "snp"], _args(input="calls.vcf.gz"))
    assert seen[0][-2:] == ["--ref", str(tmp_path)]
    assert "--ref" not in seen[1]


def test_subcommand_failure_raises_with_the_command_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(cmd: list[str]) -> None:
        raise OSError("exec failed")

    monkeypatch.setattr(wf, "run_command", boom)
    with pytest.raises(WGSExtractError, match="Subcommand failed: .* lineage"):
        wf.run_cli_subcommand(["lineage", "mt-haplogroup"], _args(ref="/r"))


def test_console_text_survives_a_narrow_stdout_encoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Ascii:
        encoding = "ascii"

    monkeypatch.setattr(wf.sys, "stdout", Ascii())
    assert wf._safe_console_text("Sample Zoë") == "Sample Zo?"
    assert wf._safe_console_text("plain") == "plain"


# --- end to end: real chain-annotate subprocess ----------------------------------


@needs_bcftools
def test_vcf_input_end_to_end_with_clinvar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
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
    cfg.write_text(f"reference_library = '{lib}'\nclinvar_vcf_path = '{clinvar}'\n")
    monkeypatch.setattr(config, "_config_path_override", cfg)
    reload_settings()

    calls = write_vcf(
        tmp_path / "me.vcf.gz",
        [(100, "a", "50", "."), (200, "b", "50", "."), (300, "c", "50", ".")],
    )
    out = tmp_path / "out"
    comprehensive("--vcf-inputs", calls, "--outdir", str(out), "--threads", "1")

    sig = out / "significant_snp-indel_me.vcf.gz"
    assert ids(str(sig)) == ["a"]
    assert "TOTAL SIGNIFICANT VARIANTS FOUND: 1" in capsys.readouterr().out


# --- batch ----------------------------------------------------------------------


def write_batch(path: Path, rows: list[list[str]], delimiter: str = ",") -> str:
    with open(path, "w", newline="") as f:
        csv.writer(f, delimiter=delimiter).writerows(rows)
    return str(path)


def recorded_samples(fake: FakeSubcommands) -> dict[str, str]:
    """--outdir -> --input of each info call."""
    return {
        Path(c[c.index("--outdir") + 1]).name: c[c.index("--input") + 1]
        for c in fake.calls
        if c[0] == "info"
    }


def test_batch_runs_each_sample_in_its_own_directory(
    fake: FakeSubcommands, bam: str, tmp_path: Path
) -> None:
    other = shutil.copy(bam, tmp_path / "other.cram")
    batch = write_batch(
        tmp_path / "b.csv",
        [["name", "input", "vcf"], ["A", bam, ""], ["B", str(other), ""]],
    )
    out = tmp_path / "out"
    comprehensive("--batch", batch, "--outdir", str(out), "--skip-calling")
    assert recorded_samples(fake) == {"A": bam, "B": str(other)}
    assert (out / "A" / "sample.bam.wgse_info.json").exists()
    assert (out / "B" / "other.cram.wgse_info.json").exists()


@needs_bcftools
def test_batch_tsv_with_alternate_headers_and_comma_separated_vcfs(
    fake: FakeSubcommands, tmp_path: Path
) -> None:
    a = write_vcf(tmp_path / "a.vcf.gz", [(100, "x", "50", ".")])
    b = write_vcf(tmp_path / "a_sv.vcf.gz", [(100, "x", "50", ".")])
    batch = write_batch(
        tmp_path / "b.tsv", [["Sample", "BAM", "VCFs"], ["S1", "", f"{a}, {b}"]], "\t"
    )
    out = tmp_path / "out"
    comprehensive("--batch", batch, "--outdir", str(out))
    assert fake.annotated() == {
        a: "clinvar,revel,phylop,cadd,gerp,gnomad,spliceai,alphamissense,pharmgkb,vep",
        b: "clinvar,gnomad",
    }
    assert (out / "S1" / "significant_sv_a_sv.vcf.gz").exists()


def test_batch_failing_sample_does_not_stop_the_others(
    fake: FakeSubcommands, bam: str, tmp_path: Path
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
    with pytest.raises(WGSExtractError, match="sample\\(s\\): gone, empty$"):
        comprehensive("--batch", batch, "--outdir", str(tmp_path), "--skip-calling")
    assert recorded_samples(fake) == {"ok": bam}


def test_batch_file_must_exist(fake: FakeSubcommands, tmp_path: Path) -> None:
    with pytest.raises(WGSExtractError, match="Batch file not found"):
        comprehensive("--batch", str(tmp_path / "none.csv"))


@needs_bcftools
def test_batch_gen_output_drives_a_batch_run(
    fake: FakeSubcommands, tmp_path: Path
) -> None:
    scan = tmp_path / "scan"
    (scan / "a").mkdir(parents=True)
    (scan / "b").mkdir()
    Path(scan / "a" / "alice.bam").write_bytes(b"")
    Path(scan / "a" / "alice.bam.bai").write_bytes(b"")
    write_vcf(scan / "a" / "alice.snp.vcf.gz", [(100, "x", "50", ".")])
    write_vcf(scan / "a" / "alice_SV.vcf.gz", [(100, "x", "50", ".")])
    write_vcf(scan / "b" / "bob.vcf.gz", [(100, "x", "50", ".")])
    Path(scan / "b" / "notes.txt").write_text("x")
    batch = tmp_path / "batch.csv"

    run_wgsextract(
        "analyze", "batch-gen", "--directory", str(scan), "--output", str(batch)
    )
    rows = list(csv.DictReader(batch.open()))
    assert [r["name"] for r in rows] == ["alice", "bob"]
    assert rows[0]["input"] == str(scan / "a" / "alice.bam")
    assert sorted(rows[0]["vcf"].split(";")) == [
        str(scan / "a" / "alice.snp.vcf.gz"),
        str(scan / "a" / "alice_SV.vcf.gz"),
    ]
    assert rows[1] == {
        "name": "bob",
        "input": "",
        "vcf": str(scan / "b" / "bob.vcf.gz"),
    }

    comprehensive("--batch", str(batch), "--outdir", str(tmp_path / "out"))
    assert recorded_samples(fake) == {"alice": rows[0]["input"]}
    assert sorted(fake.annotated()) == sorted(
        [*rows[0]["vcf"].split(";"), rows[1]["vcf"]]
    )


def test_batch_gen_requires_an_existing_directory(tmp_path: Path) -> None:
    with pytest.raises(WGSExtractError, match="Directory not found"):
        run_wgsextract("analyze", "batch-gen", "--directory", str(tmp_path / "x"))


def test_configured_batch_file_runs_each_sample_once(
    fake: FakeSubcommands, bam: str, tmp_path: Path
) -> None:
    settings["batch_file_path"] = write_batch(
        tmp_path / "b.csv", [["name", "input", "vcf"], ["A", bam, ""]]
    )
    comprehensive("--outdir", str(tmp_path / "out"), "--skip-calling")
    assert recorded_samples(fake) == {"A": bam}


@needs_bcftools
def test_batch_sample_without_vcfs_ignores_configured_vcf_input_paths(
    fake: FakeSubcommands, bam: str, tmp_path: Path
) -> None:
    settings["vcf_input_paths"] = write_vcf(
        tmp_path / "x.vcf.gz", [(1, "x", "50", ".")]
    )
    batch = write_batch(tmp_path / "b.csv", [["name", "input", "vcf"], ["A", bam, ""]])
    comprehensive("--batch", batch, "--outdir", str(tmp_path / "out"), "--skip-calling")
    assert fake.annotated() == {}


def test_batch_sample_names_stay_inside_outdir_and_blank_names_differ(
    fake: FakeSubcommands, bam: str, tmp_path: Path
) -> None:
    batch = tmp_path / "batch.csv"
    batch.write_text(f"name,input\n../escape,{bam}\n,{bam}\n,{bam}\n")
    out = tmp_path / "out"
    with pytest.raises(WGSExtractError, match=r"\.\./escape"):
        comprehensive("--batch", str(batch), "--outdir", str(out))
    assert not (tmp_path / "escape").exists()
    assert sorted(p.name for p in out.iterdir()) == ["sample2", "sample3"]


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
