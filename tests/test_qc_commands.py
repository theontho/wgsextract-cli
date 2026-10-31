"""`qc fastp`, `qc fastqc` and `qc vcf` run their tools on tiny inputs."""

import gzip
import json
import random
import subprocess
from pathlib import Path

import pytest

from tests.htslib_helpers import bgzip_index, needs_tools, run_wgsextract
from wgsextract_cli.core import dependency_checks
from wgsextract_cli.core.utils import WGSExtractError


def _fastq(path: Path, names: list[str], seed: int) -> str:
    rng = random.Random(seed)
    path.write_text(
        "".join(
            f"@{name}\n{''.join(rng.choice('ACGT') for _ in range(100))}\n+\n{'I' * 100}\n"
            for name in names
        )
    )
    return str(path)


NAMES = [f"read{i}" for i in range(10)]


def _names(fastq_gz: Path) -> list[str]:
    with gzip.open(fastq_gz, "rt") as f:
        return [line[1:].split()[0] for i, line in enumerate(f) if i % 4 == 0]


@needs_tools("fastp")
def test_fastp_filters_a_pair_and_reports(tmp_path):
    r1 = _fastq(tmp_path / "sample_R1.fastq", NAMES, 1)
    r2 = _fastq(tmp_path / "sample_R2.fastq", NAMES, 2)
    outdir = tmp_path / "out"
    run_wgsextract("qc", "fastp", "--r1", r1, "--r2", r2, "--outdir", str(outdir))

    # Clean, adapter-free reads all pass, in order.
    assert _names(outdir / "sample_fp_1.fastq.gz") == NAMES
    assert _names(outdir / "sample_fp_2.fastq.gz") == NAMES
    report = json.loads((outdir / "sample_fastp.json").read_text())
    assert report["summary"]["before_filtering"]["total_reads"] == 20
    assert report["filtering_result"]["passed_filter_reads"] == 20
    assert (outdir / "sample_fastp.html").stat().st_size > 0


@needs_tools("fastp")
def test_fastp_rejects_a_file_with_no_reads(tmp_path):
    """fastp itself exits 0 on input that isn't FASTQ, reporting no reads."""
    r1 = tmp_path / "notes.fastq"
    r1.write_text("this is not a FASTQ file\n")
    with pytest.raises(WGSExtractError, match="no reads in .*notes.fastq"):
        run_wgsextract("qc", "fastp", "--r1", str(r1))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["notes.fastq"]


@needs_tools("fastp")
def test_fastp_single_end_writes_beside_its_input(tmp_path):
    r1 = _fastq(tmp_path / "sample.fq", NAMES, 1)
    run_wgsextract("qc", "fastp", "--r1", r1)
    assert _names(tmp_path / "sample_fp_1.fastq.gz") == NAMES
    assert not (tmp_path / "sample_fp_2.fastq.gz").exists()
    assert (tmp_path / "sample_fastp.json").is_file()


def test_fastp_requires_r1():
    with pytest.raises(WGSExtractError, match="--r1 is required"):
        run_wgsextract("qc", "fastp")


def test_fastp_reports_a_missing_r2(tmp_path):
    r1 = _fastq(tmp_path / "s_1.fq", NAMES, 1)
    with pytest.raises(WGSExtractError, match="--r2"):
        run_wgsextract("qc", "fastp", "--r1", r1, "--r2", str(tmp_path / "s_2.fq"))


@needs_tools("fastp")
def test_fastp_failure_is_reported(tmp_path):
    r1 = _fastq(tmp_path / "sample.fq", NAMES, 1)
    (tmp_path / "sample_fp_1.fastq.gz").mkdir()  # fastp can't open its output
    with pytest.raises(WGSExtractError, match="fastp failed"):
        run_wgsextract("qc", "fastp", "--r1", r1)


@pytest.mark.parametrize(
    ("argv", "tool"),
    [
        (["qc", "fastp", "--r1"], "fastp"),
        (["qc", "fastqc", "--input"], "fastqc"),
        (["qc", "multiqc", "--input"], "multiqc"),
        (["qc", "vcf", "--input"], "bcftools"),
    ],
)
def test_qc_checks_its_tool_before_running(tmp_path, monkeypatch, argv, tool):
    monkeypatch.setattr(
        dependency_checks,
        "check_dependencies",
        lambda tools, *_a: [t for t in tools if t == tool],
    )
    reads = _fastq(tmp_path / "s.fq", NAMES, 1)
    with pytest.raises(WGSExtractError, match=f"Missing required .*{tool}"):
        run_wgsextract(*argv, reads, "--outdir", str(tmp_path / "out"))
    assert not (tmp_path / "out").exists()


@needs_tools("fastqc")
def test_fastqc_writes_its_report(tmp_path):
    reads = _fastq(tmp_path / "sample.fastq", NAMES, 1)
    outdir = tmp_path / "out"
    run_wgsextract("qc", "fastqc", "--input", reads, "--outdir", str(outdir))
    assert sorted(p.name for p in outdir.iterdir()) == [
        "sample_fastqc.html",
        "sample_fastqc.zip",
    ]


@needs_tools("fastqc")
def test_fastqc_failure_is_reported(tmp_path):
    bad = tmp_path / "bad.fastq"
    bad.write_text("this is not a FASTQ\n")
    with pytest.raises(WGSExtractError, match="FastQC failed"):
        run_wgsextract("qc", "fastqc", "--input", str(bad))


def test_fastqc_requires_input():
    with pytest.raises(WGSExtractError, match="--input is required"):
        run_wgsextract("qc", "fastqc")


@needs_tools("fastqc", "multiqc")
def test_multiqc_combines_mates_without_changing_fastqc_reports(tmp_path, monkeypatch):
    from hashlib import sha256

    reports = tmp_path / "fastqc reports"
    for mate in (1, 2):
        reads = _fastq(tmp_path / f"sample_R{mate}.fastq", NAMES, mate)
        run_wgsextract(
            "qc", "fastqc", "--input", reads, "--outdir", str(reports), "--threads", "1"
        )
    before = {p.name: sha256(p.read_bytes()).hexdigest() for p in reports.iterdir()}
    home = tmp_path / "multiqc-home"
    home.mkdir()
    unexpected = tmp_path / "must-not-be-written.html"
    (home / ".multiqc_config.yaml").write_text(
        json.dumps(
            {
                "title": "Inherited QC project",
                "output_fn_name": str(unexpected),
            }
        )
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    output = tmp_path / "combined"
    run_wgsextract("qc", "multiqc", "--input", str(reports), "--outdir", str(output))
    html = (output / "multiqc_report.html").read_text()
    assert "sample_R1" in html and "sample_R2" in html
    assert "Inherited QC project" in html
    assert not unexpected.exists()
    assert sorted(p.name for p in output.iterdir()) == ["multiqc_report.html"]
    assert before == {
        p.name: sha256(p.read_bytes()).hexdigest() for p in reports.iterdir()
    }
    run_wgsextract("qc", "multiqc", "--input", str(reports / "sample_R1_fastqc.zip"))
    single = (reports / "multiqc_report.html").read_text()
    assert "sample_R1" in single and "sample_R2" not in single


@needs_tools("multiqc")
def test_multiqc_rejects_missing_fastqc_reports_and_cleans_staging(tmp_path):
    source = tmp_path / "empty"
    source.mkdir()
    output = tmp_path / "combined"
    with pytest.raises(WGSExtractError, match="MultiQC failed"):
        run_wgsextract("qc", "multiqc", "--input", str(source), "--outdir", str(output))
    assert list(output.iterdir()) == []


def test_multiqc_requires_existing_input(tmp_path):
    with pytest.raises(WGSExtractError, match="--input is required"):
        run_wgsextract("qc", "multiqc")
    with pytest.raises(WGSExtractError, match="--input must be an existing"):
        run_wgsextract("qc", "multiqc", "--input", str(tmp_path / "missing"))


@pytest.fixture
def multiqc_stub(monkeypatch):
    from wgsextract_cli.commands.qc import commands

    monkeypatch.setattr(commands, "verify_dependencies", lambda _tools: None)
    monkeypatch.setattr(commands, "log_dependency_info", lambda _tools: None)
    return lambda command: monkeypatch.setattr(commands, "run_command", command)


def test_multiqc_force_replaces_only_its_report_and_stays_offline(
    tmp_path, multiqc_stub
):
    report = tmp_path / "multiqc_report.html"
    report.write_text("previous")
    unrelated = tmp_path / "keep.txt"
    unrelated.write_text("keep")
    with pytest.raises(WGSExtractError, match="already exists"):
        run_wgsextract("qc", "multiqc", "--input", str(tmp_path))

    def generate(cmd, **kwargs):
        assert {"--no-megaqc-upload", "--no-version-check", "--no-ai"} <= set(cmd)
        assert json.loads(cmd[cmd.index("--cl-config") + 1]) == {
            "ai_summary": False,
            "ai_summary_full": False,
            "output_fn_name": report.name,
        }
        stage = Path(cmd[cmd.index("--outdir") + 1])
        assert kwargs["cwd"] == str(stage)
        assert kwargs["env"]["TMPDIR"] == str(stage)
        (stage / report.name).write_text("new")

    multiqc_stub(generate)
    run_wgsextract("qc", "multiqc", "--input", str(tmp_path), "--force")
    assert report.read_text() == "new"
    assert unrelated.read_text() == "keep"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["keep.txt", report.name]


def test_multiqc_failed_force_preserves_previous_report(tmp_path, multiqc_stub):
    report = tmp_path / "multiqc_report.html"
    report.write_text("previous")

    def fail(cmd, **_kwargs):
        stage = Path(cmd[cmd.index("--outdir") + 1])
        (stage / report.name).write_text("partial")
        raise subprocess.CalledProcessError(1, cmd)

    multiqc_stub(fail)
    with pytest.raises(WGSExtractError, match="MultiQC failed"):
        run_wgsextract("qc", "multiqc", "--input", str(tmp_path), "--force")
    assert report.read_text() == "previous"
    assert sorted(p.name for p in tmp_path.iterdir()) == [report.name]


def test_multiqc_non_force_preserves_a_concurrently_created_report(
    tmp_path, multiqc_stub
):
    report = tmp_path / "multiqc_report.html"

    def concurrent(cmd, **_kwargs):
        report.write_text("another invocation")
        stage = Path(cmd[cmd.index("--outdir") + 1])
        (stage / report.name).write_text("our report")

    multiqc_stub(concurrent)
    with pytest.raises(WGSExtractError, match="already exists"):
        run_wgsextract("qc", "multiqc", "--input", str(tmp_path))
    assert report.read_text() == "another invocation"
    assert sorted(p.name for p in tmp_path.iterdir()) == [report.name]


def test_multiqc_zero_exit_without_report_is_not_success(tmp_path, multiqc_stub):
    multiqc_stub(lambda *_args, **_kwargs: None)
    with pytest.raises(WGSExtractError, match="produced no FastQC report"):
        run_wgsextract("qc", "multiqc", "--input", str(tmp_path))
    assert list(tmp_path.iterdir()) == []


def _vcf(path: Path) -> str:
    path.write_text(
        "##fileformat=VCFv4.2\n##contig=<ID=chr1,length=1000>\n"
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
        "chr1\t100\t.\tA\tG\t50\tPASS\t.\n"
        "chr1\t200\t.\tC\tT\t50\tPASS\t.\n"
        "chr1\t300\t.\tGTT\tG\t50\tPASS\t.\n"
    )
    return bgzip_index(path)


@needs_tools("bcftools", "bgzip", "tabix")
def test_vcf_qc_writes_bcftools_stats(tmp_path):
    vcf = _vcf(tmp_path / "sample.vcf")
    outdir = tmp_path / "out"
    run_wgsextract("qc", "vcf", "--input", vcf, "--outdir", str(outdir))
    stats = (outdir / "sample.vcf.gz.vcfstats.txt").read_text()
    assert "number of SNPs:\t2" in stats
    assert "number of indels:\t1" in stats


def test_vcf_qc_requires_input():
    with pytest.raises(WGSExtractError, match="--input is required"):
        run_wgsextract("qc", "vcf")


@needs_tools("bcftools")
def test_vcf_qc_failure_is_reported(tmp_path):
    bad = tmp_path / "bad.vcf"
    bad.write_text("not a vcf\n")
    with pytest.raises(WGSExtractError, match="VCF stats failed"):
        run_wgsextract("qc", "vcf", "--input", str(bad))


@needs_tools("bcftools")
def test_vcf_qc_failure_leaves_no_partial_report(tmp_path):
    bad = tmp_path / "bad.vcf"
    bad.write_text("not a vcf\n")
    with pytest.raises(WGSExtractError):
        run_wgsextract("qc", "vcf", "--input", str(bad))
    assert not (tmp_path / "bad.vcf.vcfstats.txt").exists()
