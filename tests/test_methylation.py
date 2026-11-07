import argparse
import gzip
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path

import pytest

from tests.fake_tools import python_tool
from wgsextract_cli import api
from wgsextract_cli.api.methylation import MethylationPileupParams
from wgsextract_cli.cli.parser import build_parser
from wgsextract_cli.cli.platform_support import verify_platform_support
from wgsextract_cli.commands import methylation
from wgsextract_cli.core import dependency_checks, pixi_tools
from wgsextract_cli.core.dependencies import clear_tool_caches, get_tool_path
from wgsextract_cli.core.utils import WGSExtractError

LENGTHS = {"chr20": 64_444_167, "chr21": 46_709_983}


def parse(*argv: str) -> argparse.Namespace:
    return build_parser()[0].parse_args(["methylation", "pileup", *argv])


def options(**fields: object) -> methylation.PileupOptions:
    params = MethylationPileupParams(input=Path("r.bam"), **fields)  # type: ignore[arg-type]
    return methylation.pileup_options(params)


@pytest.fixture
def no_config(monkeypatch):
    for key in (
        "methylation_context",
        "methylation_5hmc",
        "methylation_combine_strands",
    ):
        monkeypatch.delitem(methylation.settings, key, raising=False)


def test_default_command_is_cpg_5mc_combined(no_config):
    intervals = methylation.region_intervals("chr20:10000001-11000000", LENGTHS)
    command = methylation.modkit_command(
        "r.bam", "out.gz", "ref.fa", options(), "8", intervals, "regions.bed"
    )
    assert command[:8] == [
        "modkit", "pileup", "r.bam", "out.gz", "--ref", "ref.fa",
        "--modified-bases", "5mC",
    ]  # fmt: skip
    assert command[command.index("--threads") + 1] == "8"
    assert command[command.index("--io-threads") + 1] == "4"
    assert {"--cpg", "--combine-strands", "--bgzf"} <= set(command)
    # modkit's --region is 0-based, half-open.
    assert command[-2:] == ["--region", "chr20:10000000-11000000"]


def test_options_5hmc_all_context_and_several_regions(no_config):
    chosen = options(hydroxymethyl=True, context="all")
    intervals = methylation.region_intervals("chr20:5-10, chr21", LENGTHS)
    assert intervals == [("chr20", 4, 10), ("chr21", 0, LENGTHS["chr21"])]
    command = methylation.modkit_command(
        "r.bam", "o.gz", "ref.fa", chosen, "2", intervals, "regions.bed"
    )
    modified = command.index("--modified-bases")
    assert command[modified + 1 : modified + 3] == ["5mC", "5hmC"]
    assert "--cpg" not in command and "--combine-strands" not in command
    assert command[-2:] == ["--include-bed", "regions.bed"]


@pytest.mark.parametrize("region", ["chr22:1-5", "chr20:10-5", "chr20:1-99999999"])
def test_bad_regions_are_refused(region):
    with pytest.raises(WGSExtractError):
        methylation.region_intervals(region, LENGTHS)


def test_cli_overrides_config(monkeypatch):
    monkeypatch.setitem(methylation.settings, "methylation_5hmc", True)
    monkeypatch.setitem(methylation.settings, "methylation_combine_strands", False)
    assert options() == methylation.PileupOptions("cpg", True, False)
    assert options(
        hydroxymethyl=False, combine_strands=True
    ) == methylation.PileupOptions("cpg", False, True)

    monkeypatch.setitem(methylation.settings, "methylation_context", "chg")
    with pytest.raises(WGSExtractError, match="methylation_context"):
        options()


def test_combine_strands_needs_cpg(no_config):
    # The default quietly gives way; an explicit request is an error.
    assert not options(context="all").combine_strands
    with pytest.raises(WGSExtractError, match="needs --context cpg"):
        options(context="all", combine_strands=True)


def write_sam(path: Path, tags: list[str], platform: str = "ONT") -> Path:
    lines = [
        "@HD\tVN:1.6\tSO:coordinate",
        "@SQ\tSN:chr20\tLN:1000",
        f"@RG\tID:a\tPL:{platform}",
    ]
    for i, tag in enumerate(tags):
        extra = f"\t{tag}" if tag else ""
        lines.append(
            f"r{i}\t0\tchr20\t{i + 1}\t60\t4M\t*\t0\t0\tACGT\tIIII\tRG:Z:a{extra}"
        )
    # A secondary alignment is not sampled.
    lines.append(
        "s\t256\tchr20\t1\t60\t4M\t*\t0\t0\tACGT\tIIII\tMM:Z:C+m?,0;\tML:B:C,200"
    )
    path.write_text("\n".join(lines) + "\n")
    return path


needs_samtools = pytest.mark.skipif(
    shutil.which("samtools") is None, reason="samtools required"
)


@needs_samtools
def test_modification_tags_are_sampled(tmp_path):
    simplex = "MM:Z:C+m?,0;\tML:B:C,200"
    duplex = "MM:Z:C+m?,0;G-m?,0;\tML:B:C,200,10\tdx:i:1"
    jasmine = "MM:Z:C+m?,0;C+h?;G-h?;A+a.;T-a.,0;\tML:B:C,200,250"
    reads = [simplex, "", "Mm:Z:C+m,0;\tMl:B:C,9", duplex, jasmine]
    sample = methylation.sample_modification_tags(
        str(write_sam(tmp_path / "r.sam", reads)), None
    )
    assert sample == methylation.TagSample(
        reads=5, tagged=4, opposite_strand=2, duplex=1
    )
    untagged = write_sam(tmp_path / "u.sam", ["", "MM:Z:C+m?,0;"])
    assert methylation.sample_modification_tags(
        str(untagged), None
    ) == methylation.TagSample(2, 0)


@needs_samtools
def test_unreadable_input_is_an_error_not_missing_tags(tmp_path):
    broken = tmp_path / "broken.bam"
    broken.write_bytes(b"not a bam")
    with pytest.raises(WGSExtractError, match="samtools could not read"):
        methylation.sample_modification_tags(str(broken), None)


@needs_samtools
def test_reference_without_fai_is_indexed(tmp_path):
    fasta = tmp_path / "ref.fa"
    fasta.write_text(">chr20\nACGTACGT\n")
    methylation.index_fasta(str(fasta))
    assert (tmp_path / "ref.fa.fai").read_text().startswith("chr20\t8\t")


@pytest.mark.parametrize(
    ("platform", "reason"),
    [
        ("ont", "wgsextract realign` keeps them"),
        ("pacbio", "Jasmine or primrose"),
        (None, "short reads never do"),
    ],
)
def test_reads_without_tags_are_refused(platform, reason):
    with pytest.raises(WGSExtractError, match=f"None of 200 .*MM/ML.*{reason}"):
        methylation.check_modification_tags(methylation.TagSample(200, 0), platform)


def test_partly_tagged_reads_warn(caplog):
    with caplog.at_level(logging.WARNING):
        methylation.check_modification_tags(methylation.TagSample(200, 150), "ont")
        assert not caplog.records
        methylation.check_modification_tags(methylation.TagSample(200, 20), "ont")
    assert "Only 20 of 200" in caplog.text


@pytest.mark.parametrize(
    ("platform", "duplex", "source"),
    [
        ("pacbio", 0, "Jasmine writes them for 5hmC .* only its 5mC model"),
        ("ont", 200, "Dorado writes them for duplex reads"),
        (None, 200, "Dorado writes them for duplex reads"),
        (None, 0, "Remove those modification calls"),
    ],
)
def test_opposite_strand_reads_are_refused_with_their_source(platform, duplex, source):
    sample = methylation.TagSample(200, 200, opposite_strand=200, duplex=duplex)
    with pytest.raises(WGSExtractError, match=f"All 200 .*skips such.*{source}"):
        methylation.check_modification_tags(sample, platform)


def test_some_opposite_strand_reads_warn(caplog):
    with caplog.at_level(logging.WARNING):
        methylation.check_modification_tags(
            methylation.TagSample(200, 200, opposite_strand=30, duplex=30), "ont"
        )
    assert "30 of 200 sampled reads" in caplog.text


@needs_samtools
def test_pacbio_5mc_only_reads_pass(tmp_path):
    primrose = "MM:Z:C+m?,0,1;\tML:B:C,200,5"
    reads = write_sam(tmp_path / "pb.sam", [primrose] * 3, platform="PACBIO")
    sample = methylation.sample_modification_tags(str(reads), None)
    assert sample == methylation.TagSample(3, 3)
    methylation.check_modification_tags(sample, "pacbio")


@pytest.mark.parametrize("platform", ["ont", "pacbio", None])
def test_long_read_platforms_are_accepted(platform):
    methylation.require_modification_platform(platform)


@pytest.mark.parametrize("platform", ["illumina", "ultima", "dnbseq"])
def test_short_read_platforms_are_refused(platform):
    with pytest.raises(WGSExtractError, match=f"looks like {platform} short reads"):
        methylation.require_modification_platform(platform)


def test_missing_modkit_names_its_pixi_environment(tmp_path, monkeypatch):
    reads = tmp_path / "reads.bam"
    reads.write_bytes(b"")
    monkeypatch.setattr(methylation, "require_supported_platform", lambda: None)
    monkeypatch.setattr(dependency_checks, "require_native_tool", lambda tool: None)
    monkeypatch.setattr(pixi_tools, "_locked_package_available", lambda *args: True)
    monkeypatch.setattr(
        dependency_checks, "check_dependencies", lambda tools: [methylation.MODKIT]
    )
    with pytest.raises(WGSExtractError, match=r"modkit.*-e modkit"):
        methylation.run_pileup(MethylationPileupParams(input=reads))


def test_windows_is_refused_before_inputs_are_resolved(monkeypatch):
    args = parse("--input", "reads.bam")
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(WGSExtractError, match="not available on Windows.*WSL2"):
        verify_platform_support(args)


def test_bedmethyl_summary(tmp_path):
    def row(code, start, valid, percent, modified):
        return (
            f"chr20\t{start}\t{start + 1}\t{code}\t{valid}\t.\t{start}\t{start + 1}"
            f"\t255,0,0\t{valid}\t{percent}\t{modified}\t{valid - modified}"
            "\t0\t0\t0\t0\t0\n"
        )

    path = tmp_path / "x.bedmethyl.gz"
    with gzip.open(path, "wt") as out:
        out.write(row("m", 1, 10, "90.00", 9))
        out.write(row("m", 3, 30, "10.00", 3))
        out.write(row("m", 5, 2, "100.00", 2))  # below the coverage floor
        out.write(row("m", 7, 20, "50.00", 10))
        out.write(row("h", 1, 10, "0.00", 0))
    m, h = methylation.summarize_bedmethyl(str(path))
    assert (m.modification, m.sites, m.summary_sites) == ("5mC", 4, 3)
    assert m.weighted_percent == pytest.approx(36.67, abs=0.01)  # 22 / 60
    assert m.mean_site_percent == pytest.approx(50.0)
    assert m.median_coverage == 20
    assert m.high_sites_percent == m.low_sites_percent == pytest.approx(100 / 3)
    assert (h.modification, h.weighted_percent) == ("5hmC", 0.0)


@pytest.mark.parametrize(
    ("counts", "median"), [({}, 0), ({5: 1}, 5), ({5: 1, 9: 1}, 7), ({1: 2, 8: 1}, 1)]
)
def test_coverage_median_from_counts(counts, median):
    assert methylation._counter_median(Counter(counts)) == median


# --- the typed API, against a fake modkit ---------------------------------------
# modkit is an optional environment (and Linux/macOS only), so it is faked;
# samtools reads the tags, and tabix indexes what the fake writes.

_FAKE_MODKIT = """\
import json, subprocess, sys, time
argv = sys.argv[1:]
with open({log!r}, "a") as f:
    f.write(json.dumps(argv) + "\\n")
codes = ["m", "h"] if "5hmC" in argv else ["m"]
rows = "".join(
    f"chr20\\t{{pos}}\\t{{pos + 1}}\\t{{code}}\\t10\\t.\\t{{pos}}\\t{{pos + 1}}\\t255,0,0"
    f"\\t10\\t{{pct}}\\t{{n}}\\t{{10 - n}}\\t0\\t0\\t0\\t0\\t0\\n"
    for pos, n in ((1, 9), (5, 1), (9, 8))
    for code in codes
    for pct in [f"{{n * 10:.2f}}"]
)
with open(argv[2], "wb") as out:
    subprocess.run([{bgzip!r}, "-c"], input=rows.encode(), stdout=out, check=True)
time.sleep({sleep})
"""


@pytest.fixture
def fake_modkit(tmp_path, monkeypatch):
    """Install a fake `modkit` on PATH; returns a function giving its logged
    command lines. ``fake_modkit.install(sleep=...)`` reinstalls it."""
    if sys.platform == "win32":
        pytest.skip("modkit runs on Linux and macOS only")
    log = tmp_path / "modkit_calls.jsonl"
    bindir = tmp_path / "fakebin"

    def install(sleep: float = 0) -> None:
        clear_tool_caches()
        bgzip = get_tool_path("bgzip")
        source = _FAKE_MODKIT.format(log=str(log), bgzip=bgzip, sleep=sleep)
        python_tool(bindir, "modkit", source)
        clear_tool_caches()

    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    install()

    def calls() -> list[list[str]]:
        if not log.exists():
            return []
        lines = log.read_text().splitlines()
        return [argv for argv in map(json.loads, lines) if argv[:1] == ["pileup"]]

    calls.install = install  # type: ignore[attr-defined]
    return calls


@pytest.fixture
def tagged(tmp_path, no_config) -> tuple[Path, Path]:
    """An indexed ONT BAM whose reads carry 5mC tags, and its reference."""
    ref = tmp_path / "ref" / "ref.fa"
    ref.parent.mkdir()
    ref.write_text(">chr20\n" + "ACGT" * 250 + "\n")
    sam = write_sam(tmp_path / "reads.sam", ["MM:Z:C+m?,0;\tML:B:C,230"] * 3)
    bam = tmp_path / "reads" / "reads.bam"
    bam.parent.mkdir()
    subprocess.run(["samtools", "sort", "-o", str(bam), str(sam)], check=True)
    subprocess.run(["samtools", "index", str(bam)], check=True)
    return bam, ref


@needs_samtools
def test_api_pileup_writes_the_bedmethyl_summary_and_metrics(
    tmp_path, fake_modkit, tagged
):
    bam, ref = tagged
    result = api.methylation.pileup(
        MethylationPileupParams(
            input=bam,
            ref=ref,
            outdir=tmp_path / "out",
            hydroxymethyl=True,
            region="chr20:1-500",
        )
    )

    assert result.status == "ok"
    out = tmp_path / "out"
    assert {o.role: Path(o.path) for o in result.outputs} == {
        "bedmethyl": out / "reads.cpg.bedmethyl.gz",
        "methylation_summary": out / "reads.cpg.methylation.tsv",
    }
    assert (out / "reads.cpg.bedmethyl.gz.tbi").is_file()
    summary = (out / "reads.cpg.methylation.tsv").read_text().splitlines()
    assert [line.split("\t")[0] for line in summary[1:]] == ["5mC", "5hmC"]
    assert result.metrics["platform"] == "ont"
    assert result.metrics["5mC_sites"] == 3
    assert result.metrics["5mC_weighted_percent_modified"] == 60.0
    [command] = fake_modkit()
    # contract: modkit's --region is 0-based, half-open.
    assert command[command.index("--region") + 1] == "chr20:0-500"


@needs_samtools
def test_api_expands_home_in_every_path(tmp_path, fake_modkit, tagged, monkeypatch):
    """#478: `~` paths reach modkit expanded, with no literal `~` folder."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    api.methylation.pileup(
        MethylationPileupParams(
            input=Path("~/reads/reads.bam"),
            ref=Path("~/ref/ref.fa"),
            outdir=Path("~/out"),
        )
    )

    assert (tmp_path / "out" / "reads.cpg.bedmethyl.gz.tbi").is_file()
    [command] = fake_modkit()
    assert command[1] == str(tmp_path / "reads" / "reads.bam")
    assert command[command.index("--ref") + 1] == str(tmp_path / "ref" / "ref.fa")
    assert not (tmp_path / "~").exists()


@needs_samtools
def test_cancelling_during_modkit_leaves_no_partial_output(
    tmp_path, fake_modkit, tagged
):
    """#478: a cancel while modkit runs stops the run and deletes its output."""
    fake_modkit.install(sleep=60)
    bam, ref = tagged
    out = tmp_path / "out"
    token = api.CancelToken()

    def cancel_once_modkit_writes() -> None:
        deadline = time.monotonic() + 30
        while not list(out.glob("*bedmethyl*")):
            if time.monotonic() > deadline:
                return
            time.sleep(0.05)
        token.cancel()

    watcher = threading.Thread(target=cancel_once_modkit_writes)
    watcher.start()
    started = time.monotonic()
    result = api.methylation.pileup(
        MethylationPileupParams(input=bam, ref=ref, outdir=out),
        cancel=token,
        check=False,
    )
    watcher.join()

    assert (result.status, result.exit_code) == ("cancelled", 130)
    assert time.monotonic() - started < 30
    assert [p.name for p in out.iterdir() if "manifest" not in p.name] == []


@needs_samtools
def test_parquet_format_also_writes_the_sites(tmp_path, fake_modkit, tagged):
    pq = pytest.importorskip("pyarrow.parquet")
    bam, ref = tagged
    result = api.methylation.pileup(
        MethylationPileupParams(input=bam, ref=ref, table_format="parquet")
    )

    outputs = {o.role: o for o in result.outputs}
    assert Path(outputs["methylation_summary"].path).suffix == ".parquet"
    sites = pq.read_table(outputs["bedmethyl_table"].path)
    assert sites.column("n_mod").to_pylist() == [9, 1, 8]
    assert outputs["bedmethyl_table"].metrics["rows"] == 3


@needs_samtools
def test_a_cancelled_rerun_keeps_the_previous_results_with_atomic_outputs(
    tmp_path, fake_modkit, tagged, monkeypatch
):
    """#478 review: with atomic_outputs, a failed rerun must not delete the
    earlier complete bedMethyl, its index and summary."""
    from wgsextract_cli.core.config import settings

    monkeypatch.setitem(settings, "atomic_outputs", True)
    bam, ref = tagged
    out = tmp_path / "out"
    params = MethylationPileupParams(input=bam, ref=ref, outdir=out)
    api.methylation.pileup(params)
    kept = ["reads.cpg.bedmethyl.gz", "reads.cpg.bedmethyl.gz.tbi"]
    kept.append("reads.cpg.methylation.tsv")
    before = {name: (out / name).read_bytes() for name in kept}
    fake_modkit.install(sleep=60)
    token = api.CancelToken()

    def cancel_once_modkit_writes() -> None:
        deadline = time.monotonic() + 30
        while not list(out.glob(".*partial*")):
            if time.monotonic() > deadline:
                return
            time.sleep(0.05)
        token.cancel()

    watcher = threading.Thread(target=cancel_once_modkit_writes)
    watcher.start()
    result = api.methylation.pileup(params, cancel=token, check=False)
    watcher.join()

    assert result.status == "cancelled"
    assert {name: (out / name).read_bytes() for name in kept} == before
    assert not list(out.glob(".*partial*"))


@needs_samtools
def test_a_rerun_failing_before_modkit_keeps_the_previous_results(
    tmp_path, fake_modkit, tagged
):
    """#493 review: a rerun refused in its region check deleted the earlier
    bedMethyl, index and summary before writing anything."""
    bam, ref = tagged
    out = tmp_path / "out"
    api.methylation.pileup(MethylationPileupParams(input=bam, ref=ref, outdir=out))
    before = {p.name: p.read_bytes() for p in out.iterdir()}

    result = api.methylation.pileup(
        MethylationPileupParams(input=bam, ref=ref, outdir=out, region="chr20,chrZ"),
        check=False,
    )

    assert result.status == "failed" and "chrZ" in result.error.message
    assert {p.name: p.read_bytes() for p in out.iterdir()} == before


@needs_samtools
def test_a_rerun_cancelled_after_modkit_publishes_nothing_with_atomic_outputs(
    tmp_path, fake_modkit, tagged, monkeypatch
):
    """#493 review: the new bedMethyl was published before its index and
    summary, so a later failure left it beside the old summary."""
    from wgsextract_cli.core.config import settings

    monkeypatch.setitem(settings, "atomic_outputs", True)
    bam, ref = tagged
    out = tmp_path / "out"
    api.methylation.pileup(MethylationPileupParams(input=bam, ref=ref, outdir=out))
    before = {p.name: p.read_bytes() for p in out.iterdir()}
    token = api.CancelToken()

    def cancel_at_tabix(event: api.ProgressEvent) -> None:
        if event.stage == "tool" and "tabix" in event.message:
            token.cancel()

    result = api.methylation.pileup(
        MethylationPileupParams(input=bam, ref=ref, outdir=out, hydroxymethyl=True),
        progress=cancel_at_tabix,
        cancel=token,
        check=False,
    )

    assert result.status == "cancelled"
    assert {p.name: p.read_bytes() for p in out.iterdir()} == before


@needs_samtools
def test_a_server_job_does_not_follow_a_reference_sibling_out_of_scope(
    tmp_path, fake_modkit, tagged
):
    """#493 review: for ref.fa.gz, the uncompressed ref.fa beside it was used
    even when it links outside the folders a server job may read."""
    from wgsextract_cli.core import read_scope

    bam, ref = tagged
    packed = Path(f"{ref}.gz")
    with open(ref, "rb") as src, gzip.open(packed, "wb") as dst:
        shutil.copyfileobj(src, dst)
    outside = tmp_path / "outside" / "ref.fa"
    outside.parent.mkdir()
    ref.rename(outside)
    ref.symlink_to(outside)
    allowed = (tmp_path / "ref", tmp_path / "reads", tmp_path / "out")

    def allows(path: str) -> bool:
        real = Path(os.path.realpath(path))
        return any(real.is_relative_to(folder) for folder in allowed)

    with read_scope.limited_to(allows):
        api.methylation.pileup(
            MethylationPileupParams(input=bam, ref=packed, outdir=tmp_path / "out")
        )

    [command] = fake_modkit()
    used = Path(command[command.index("--ref") + 1])
    assert allows(str(used)) and not Path(f"{outside}.fai").exists()
