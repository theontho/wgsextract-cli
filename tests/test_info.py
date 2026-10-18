import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

from wgsextract_cli.commands.info import metrics, render, runner
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.utils import WGSExtractError
from wgsextract_cli.main import main

HG38_MD5 = "bd894134bddba260df88a90123a2ee9c"
BODY_SAMPLE = (1000, 150.0, 2.0, 300.0, 50.0, True, "A00910:1:ABCDEFGHI:1:1:1:1")
IDXSTATS = (
    [
        {"name": "1", "length": 248956422, "mapped": 1000000, "unmapped": 0},
        {"name": "X", "length": 155270560, "mapped": 1000000, "unmapped": 0},
        {"name": "Y", "length": 57227415, "mapped": 500000, "unmapped": 0},
    ],
    461454397,
    2500000,
    0,
)


@pytest.fixture
def info_env(tmp_path, monkeypatch):
    """A real (dummy) input file with all samtools-backed helpers mocked."""
    sample = tmp_path / "sample.bam"
    sample.write_bytes(b"not really a bam")
    mocks = {
        "verify_dependencies": None,
        "log_dependency_info": None,
        "get_bam_header": "@HD\tVN:1.6\tSO:coordinate\n@SQ\tSN:1\tLN:248956422",
        "calculate_bam_md5": HG38_MD5,
        "is_sorted": True,
        "get_file_stats": (50.0, True),
        "run_body_sample": BODY_SAMPLE,
        "parse_idxstats": IDXSTATS,
        "load_n_counts": {},
    }
    calls: dict[str, int] = {}
    for name, value in mocks.items():

        def fake(*_args, _name=name, _value=value, **_kwargs):
            calls[_name] = calls.get(_name, 0) + 1
            return _value

        monkeypatch.setattr(runner, name, fake)
    monkeypatch.setattr(
        "wgsextract_cli.core.variant_files.get_file_version", lambda _p: "BAM"
    )
    monkeypatch.setattr(
        "wgsextract_cli.core.variant_files.resolve_reference", lambda ref, _md5: ref
    )
    return sample, calls


def _run_cli(*args: str) -> str:
    out = io.StringIO()
    with redirect_stdout(out), patch.object(sys, "argv", ["wgsextract-cli", *args]):
        main()
    return out.getvalue()


def _run_info(sample: Path, *args: str) -> str:
    return _run_cli(*args, "--input", str(sample), "--outdir", str(sample.parent))


def test_info_fast_mode(info_env):
    sample, _calls = info_env
    output = _run_info(sample, "info")

    assert "Reference Genome            hg38 (Chr), rCRS, 1 SNs" in output
    assert "File Stats                  Sorted, Indexed, 50.0 GBs" in output
    assert "Avg Read Length             150 bp" in output
    assert "Avg Insert Size             300 bp" in output
    assert "Sequencer                   Illumina NS 6000 (Dante)" in output
    cache = json.loads(Path(str(sample) + ".wgse_info.json").read_text())
    assert cache["input_signature"]["size"] == sample.stat().st_size


def test_info_detailed_mode(info_env):
    sample, _calls = info_env
    output = _run_info(sample, "info", "--detailed")

    assert "hg38 (Chr), rCRS, 1 SNs" in output
    assert "150 bp" in output
    assert "Bio Gender                  Male" in output
    assert "Illumina NS 6000 (Dante)" in output
    assert "File Content                Auto, X, Y" in output


def test_info_csv_mode_is_pure_csv_and_cached(info_env):
    sample, calls = info_env
    output = _run_info(sample, "info", "--detailed", "--csv")

    lines = output.splitlines()
    assert lines[0] == (
        "Seq Name,Model Len,Model N Len,# Segs Map,Map Gbases,Map ARD,Breadth Coverage"
    )
    assert all(line.count(",") == 6 for line in lines)
    assert any(line.startswith("1,248956422") for line in lines)

    # The freshly computed table was cached, so a second run is a cache hit.
    assert _run_info(sample, "info", "--detailed", "--csv") == output
    assert calls["parse_idxstats"] == 1


def test_info_cache_invalidated_when_input_changes(info_env):
    sample, calls = info_env
    _run_info(sample, "info")
    _run_info(sample, "info")
    assert calls["run_body_sample"] == 1

    sample.write_bytes(b"a different, replaced input file")
    _run_info(sample, "info")
    assert calls["run_body_sample"] == 2


@pytest.mark.parametrize("payload", ['{"filename": "x", "avg_read_len": 99}', "{bad"])
def test_info_legacy_or_corrupt_cache_is_recomputed(info_env, payload):
    sample, calls = info_env
    Path(str(sample) + ".wgse_info.json").write_text(payload)

    output = _run_info(sample, "info")

    assert calls["run_body_sample"] == 1
    assert "Avg Read Length             150 bp" in output


def test_region_coverage_does_not_poison_whole_genome_cache(info_env, monkeypatch):
    sample, _calls = info_env
    seen = []

    def fake_full_coverage(input_p, ref_p, out_p, region=None, legacy_p=None):
        seen.append((os.path.basename(out_p), legacy_p))
        return {"1": "42 %"}

    monkeypatch.setattr(runner, "run_full_coverage", fake_full_coverage)
    region_out = _run_info(sample, "info", "calculate-coverage", "-r", "1:1-1000")
    assert "42 %" in region_out
    assert seen == [("sample.bam_1_1_1000_coverage.tsv", None)]
    assert not Path(str(sample) + ".wgse_info.json").exists()

    detailed = _run_info(sample, "info", "--detailed")
    assert "42 %" not in detailed


def test_info_errors_exit_nonzero(tmp_path, info_env, monkeypatch):
    sample, _calls = info_env
    monkeypatch.setattr(runner, "get_bam_header", lambda *_a, **_k: "")
    with pytest.raises(SystemExit) as exc:
        _run_info(sample, "info")
    assert exc.value.code != 0


def test_global_options_before_subcommand_are_preserved(tmp_path):
    with patch("wgsextract_cli.commands.info.run") as mock_run:
        _run_cli(
            "--input", "sample.bam", "--outdir", str(tmp_path), "--ref", "ref", "info"
        )

    args = mock_run.call_args.args[0]
    assert args.input == "sample.bam"
    assert args.outdir == str(tmp_path)
    assert args.ref == "ref"


def test_load_n_counts_reads_reference_bundle_format(tmp_path):
    ref = tmp_path / "hs38.fa.gz"
    ref.write_bytes(b"")
    (tmp_path / "hs38_ncnt.csv").write_text(
        "#Processing Ref Model: hs38.fa.gz with >300bp runs of N\n"
        "#Seq\tNumBP\tNumNs\tNumNreg\n"
        "chr1\t248,956,422\t18,475,410\t40\n"
        "chrM\t16,569\t0\t0\n"
        "chrX\t156,040,895\tnot-a-number\t1\n"
        "\n"
        # Genome totals trailer; its first field is a sequence count, not a contig.
        "#TOTALS:\n"
        "2\t249,000,000\t18,475,410\t40\t\t\t0\n"
    )

    assert metrics.load_n_counts(str(ref)) == {"1": 18475410, "M": 0}


def test_load_n_counts_accepts_two_column_csv(tmp_path):
    ref = tmp_path / "custom.fasta"
    ref.write_bytes(b"")
    (tmp_path / "custom_ncnt.csv").write_text('#SN,NumNs\nMT,5\n2,"1,000"\n')

    assert metrics.load_n_counts(str(ref)) == {"M": 5, "2": 1000}


def test_contig_key_normalization():
    assert metrics.contig_key("chr1") == "1"
    assert metrics.contig_key("chrMT") == "M"
    assert metrics.contig_key("MT") == "M"
    assert metrics.contig_key("chrUn_KI270302v1") == "UN_KI270302V1"
    assert metrics.is_primary_contig("chrX")
    assert not metrics.is_primary_contig("chrM")


IDX = [
    {"name": "chr1", "length": 1_000_000, "mapped": 1, "unmapped": 0},
    {"name": "chr2", "length": 200_000, "mapped": 1, "unmapped": 0},
    {"name": "chrM", "length": 16569, "mapped": 1, "unmapped": 0},
    {"name": "tiny", "length": 500, "mapped": 1, "unmapped": 0},
]


def test_sample_targets_resolve_regions_and_aliases():
    assert render._resolve_sample_targets(IDX, None) == [
        ("chr1", 1, 1_000_000),
        ("chr2", 1, 200_000),
    ]
    assert render._resolve_sample_targets(IDX, "1") == [("chr1", 1, 1_000_000)]
    assert render._resolve_sample_targets(IDX, "chr1:1-1000") == [("chr1", 1, 1000)]
    assert render._resolve_sample_targets(IDX, "MT") == [("chrM", 1, 16569)]
    assert render._resolve_sample_targets(IDX, "tiny") == [("tiny", 1, 500)]
    with pytest.raises(WGSExtractError):
        render._resolve_sample_targets(IDX, "chr9")
    with pytest.raises(WGSExtractError):
        render._resolve_sample_targets(IDX, "chr1:500-100")


def test_sample_windows_are_proportional_and_in_bounds():
    targets = [("chr1", 1, 1_000_000), ("chr2", 1, 200_000), ("tiny", 1, 500)]
    windows = render._sample_windows(targets)
    per_contig = {name: 0 for name, _s, _e in targets}
    for name, start, end in windows:
        per_contig[name] += 1
        span_end = next(e for n, _s, e in targets if n == name)
        assert 0 <= start < end <= span_end
    assert per_contig["chr1"] > per_contig["chr2"] >= 10
    assert per_contig["tiny"] == 1
    assert ("tiny", 0, 500) in windows


def _write_coverage(path: Path, rows: list[tuple[str, int, int]]) -> None:
    lines = ["#rname\tstartpos\tendpos\tnumreads\tcovbases\tcoverage"]
    lines += [f"{n}\t1\t{length}\t10\t{cov}\t0" for n, length, cov in rows]
    path.write_text("\n".join(lines) + "\n")


def test_full_coverage_reuses_only_fresh_results(tmp_path, monkeypatch):
    sample = tmp_path / "s.bam"
    sample.write_bytes(b"x")
    out = tmp_path / "s.bam_coverage.tsv"
    commands = []

    def fake_run_command(cmd, *args, **kwargs):
        commands.append(cmd)
        _write_coverage(Path(cmd[cmd.index("-o") + 1]), [("chr1", 100, 50)])

    monkeypatch.setattr(render, "run_command", fake_run_command)

    _write_coverage(out, [("chr1", 100, 100)])
    stale = sample.stat().st_mtime - 10
    os.utime(out, (stale, stale))  # results older than the input
    assert render.run_full_coverage(str(sample), None, str(out)) == {
        "1": "50 %",
        "TOTAL_EST": "50 %",
    }
    assert len(commands) == 1
    assert render.run_full_coverage(str(sample), None, str(out))["1"] == "50 %"
    assert len(commands) == 1


def test_full_coverage_accepts_fresh_legacy_bincvg(tmp_path, monkeypatch):
    sample = tmp_path / "s.bam"
    sample.write_bytes(b"x")
    legacy = tmp_path / "s.bam_bincvg.csv"
    legacy.write_text(
        "chr\tzero\tnonzero\tsum nz\tfract nz\tavg nz\tavg all\tTotalBC\n"
        "chr1\t25\t75\t0\t0\t0\t0\t100\n"
    )
    monkeypatch.setattr(
        render, "run_command", lambda *a, **k: pytest.fail("should reuse legacy")
    )

    result = render.run_full_coverage(
        str(sample), None, str(tmp_path / "s.bam_coverage.tsv"), legacy_p=str(legacy)
    )
    assert result["1"] == "75 %"


def test_full_coverage_failure_raises_and_leaves_no_output(tmp_path, monkeypatch):
    sample = tmp_path / "s.bam"
    sample.write_bytes(b"x")
    out = tmp_path / "s.bam_coverage.tsv"

    def failing(cmd, *args, **kwargs):
        Path(cmd[cmd.index("-o") + 1]).write_text("#partial\nchr1\t1\t9")
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(render, "run_command", failing)
    with pytest.raises(WGSExtractError):
        render.run_full_coverage(str(sample), None, str(out))
    assert not list(tmp_path.glob("s.bam_coverage*"))


@pytest.fixture
def tiny_bam(tmp_path):
    samtools = get_tool_path("samtools")
    if not samtools:
        pytest.skip("samtools not available")
    sam = tmp_path / "tiny.sam"
    read = "A" * 100
    qual = "I" * 100
    records = [
        f"r{i}\t0\tchr1\t{pos}\t60\t100M\t*\t0\t0\t{read}\t{qual}"
        for i, pos in enumerate(range(1, 5001, 100))
    ]
    sam.write_text(
        "@HD\tVN:1.6\tSO:coordinate\n@SQ\tSN:chr1\tLN:10000\n"
        "@SQ\tSN:chr2\tLN:800\n" + "\n".join(records) + "\n"
    )
    bam = tmp_path / "tiny.bam"
    subprocess.run([samtools, "view", "-b", "-o", str(bam), str(sam)], check=True)
    subprocess.run([samtools, "index", str(bam)], check=True)
    return bam


def test_coverage_commands_against_real_samtools(tiny_bam, tmp_path):
    idx_stats, *_ = metrics.parse_idxstats(str(tiny_bam))

    full = render.run_full_coverage(
        str(tiny_bam), None, str(tmp_path / "full.tsv"), region="chr1"
    )
    assert full == {"1": "50 %"}

    sampled = render.run_sampled_coverage(
        str(tiny_bam), None, idx_stats, str(tmp_path / "s.json"), region="chr1"
    )
    assert sampled == {"1": "~50 %"}
    assert json.loads((tmp_path / "s.json").read_text()) == sampled

    exact = render.run_sampled_coverage(
        str(tiny_bam), None, idx_stats, str(tmp_path / "r.json"), "chr1:4901-5100"
    )
    assert exact == {"1": "~50 %"}
    assert render.run_sampled_coverage(
        str(tiny_bam), None, idx_stats, str(tmp_path / "c2.json"), "chr2"
    ) == {"2": "~0 %"}
