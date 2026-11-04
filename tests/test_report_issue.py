"""report-issue: redacted, pre-filled GitHub bug reports (CLI and API)."""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path

import pytest

from wgsextract_cli import api
from wgsextract_cli.core import issue_report
from wgsextract_cli.core.issue_report import Redactor
from wgsextract_cli.main import main

USER = "jdoe42"
HOST = "jdoes-macbook.local"
SAMPLE = "Jane_Q_Public"
# A fake token in the shape the redactor must catch.
FAKE_TOKEN = "ghp_0123456789abcdefghijABCDEFGHIJ"  # gitleaks:allow


@pytest.fixture(autouse=True)
def _this_computer(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """A recognisable user and computer name, steady environment lines, no
    tool probing and a browser that records instead of opening."""
    monkeypatch.setattr("getpass.getuser", lambda: USER)
    monkeypatch.setattr("platform.node", lambda: HOST)
    monkeypatch.setattr(
        issue_report, "environment_lines", lambda: ["- OS: TestOS 1.0, x86_64"]
    )
    monkeypatch.setattr(
        issue_report, "tool_versions", lambda extra=(): {"samtools": "1.24"}
    )
    opened: list[str] = []
    monkeypatch.setattr(
        "webbrowser.open", lambda url, new=0: opened.append(url) or True
    )
    return opened


def _cli(capfd: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    try:
        main(list(argv))
        code = 0
    except SystemExit as e:
        code = int(e.code or 0)
    out, err = capfd.readouterr()
    return code, out, err


def _issue_fields(url: str) -> dict[str, str]:
    query = urllib.parse.urlsplit(url).query
    return {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}


def _fail_info(capfd: pytest.CaptureFixture[str], tmp_path: Path) -> Path:
    """Run 'info' on a corrupt BAM in the user's home, named after the sample."""
    folder = Path.home() / "genomes" / SAMPLE
    folder.mkdir(parents=True)
    bam = folder / f"{SAMPLE}.hg38.bam"
    bam.write_bytes(b"not a BAM")
    code, _, err = _cli(capfd, "info", "--input", str(bam), "--outdir", str(tmp_path))
    assert code == 1 and "Invalid alignment input" in err
    return bam


def test_failed_run_report_names_the_failure_but_nothing_personal(
    capfd: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _fail_info(capfd, tmp_path)

    code, out, _ = _cli(capfd, "report-issue", "--json", "--print")

    assert code == 0
    details = json.loads(out)["details"]
    fields = _issue_fields(details["url"])
    assert details["source"] == "last run"
    assert fields["title"].startswith("info: Invalid alignment input <path>/<file>.bam")
    assert "exit code 1" in fields["diagnostics"]
    assert "--input <path>/<file>.bam" in fields["diagnostics"]
    saved = Path(details["file"]).read_text()
    for leak in (str(Path.home()), USER, HOST, "jdoes-macbook", SAMPLE, "genomes"):
        for text in (details["url"], fields["title"], fields["diagnostics"], saved):
            assert leak.lower() not in text.lower(), (leak, text)


def test_last_failed_reports_the_failure_after_a_later_success(
    capfd: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _fail_info(capfd, tmp_path)
    assert _cli(capfd, "deps", "check", "--tool", "tar")[0] == 0

    report = api.support.build_issue_report(api.ReportIssueParams())
    failed = api.support.build_issue_report(api.ReportIssueParams(last_failed=True))

    assert report.title == "Problem with 'deps check'"
    assert any("--last-failed" in note for note in report.notes)
    assert failed.source == "last failed run"
    assert failed.title.startswith("info: Invalid alignment input")


@pytest.mark.parametrize(
    ("text", "secret", "placeholder"),
    [
        ("Cannot open /home/jdoe42/data/x.cram", "/home/jdoe42", "<path>/<file>.cram"),
        (r"Missing C:\Users\jdoe42\JaneQ.vcf.gz", "JaneQ", "<path>/<file>.vcf.gz"),
        (
            "'/Volumes/Jane Q Public/J Q.bam' bad",
            "Jane Q Public",
            "'<path>/<file>.bam'",
        ),
        ("from Jane_Q_Public.bam ok", SAMPLE, "<file>.bam"),
        ("reading out/x/Jane_Q_Public.bam", SAMPLE, "reading <path>/<file>.bam"),
        ("--outdir out/jane/results/x", "jane", "<path>/<name>"),
        ("logged in as jdoe42 on jdoes-macbook", USER, "<user> on <host>"),
        ("mail jane@example.org", "jane@", "<email>"),
        (f"token={FAKE_TOKEN}", "ghp_", "token=<secret>"),
        ("--api-key s3cr3t-value done", "s3cr3t", "--api-key <redacted>"),
        ('password="correct horse battery" next', "horse", 'password="<redacted>"'),
        ('{"api_key": "s3cr3t-value"}', "s3cr3t", '"api_key": "<redacted>"'),
        (
            r"Cannot open C:\Data\Jane Q Public WGS.bam",
            "Public",
            "open <path>/<file>.bam",
        ),
        (
            "Cannot open /data/Jane Q Public.cram now",
            "Public",
            "<path>/<file>.cram now",
        ),
        (
            "get https://data.example.org/Jane%20Q/Jane%20Q_ref.fa failed",
            "Jane",
            "https://<server>/<path>/<file>.fa failed",
        ),
        (
            "get https://ftp.ebi.ac.uk/pub/Jane%5FQ%5FPublic.bam",
            "Jane",
            "https://ftp.ebi.ac.uk/<path>/<file>.bam",
        ),
        ("several samples (Ann_Lee, Bo_Kim); split", "Ann_Lee", "samples (<sample>)"),
        ("@RG ID:1 SM:Ann_Lee", "Ann_Lee", "SM:<sample>"),
        (
            "  HTT (Huntington disease, AD): CAG 17/45 [CI 16-18/44-46] pathogenic",
            "17/45",
            "genetic result removed",
        ),
        ("Sample sex: female (from chrX/chrY coverage)", "female", "genetic result"),
        ("Mitochondrial haplogroup: H16a1", "H16a1", "genetic result removed"),
        ("Authorization: Bearer abc.def.ghi1234", "abc.def", "<secret>"),
        (
            "https://u:pw@host.org/f.vcf?sig=xyz ok",
            "sig=xyz",
            "https://<server>/<path>/<file>.vcf",
        ),
        ("read A00123:8:H7KJ2DSXY:1:1101:1000:2000 bad", "H7KJ2DSXY", "<read>"),
        ("chr1\t10177\trs367896724\tA\tAC\t100\tPASS\t.", "rs367896724", "<data line"),
        ("rs4477212\t1\t82154\tAA", "rs4477212", "<data line"),
        ("chr7 117559590 rs113993960 ATCT A", "rs113993960", "<data line"),
        ("ACGT" * 10, "ACGTACGT", "<data line"),
    ],
)
def test_redactor_removes_identifying_text(
    text: str, secret: str, placeholder: str
) -> None:
    redactor = Redactor(
        home="/home/jdoe42",
        users=[USER],
        hosts=[HOST, "jdoes-macbook"],
        sensitive=[SAMPLE],
    )
    cleaned = redactor.text(text)
    assert secret.lower() not in cleaned.lower()
    assert placeholder in cleaned


def test_redactor_keeps_what_diagnoses_a_problem() -> None:
    redactor = Redactor.for_this_computer(safe_names=["hs38DH"])
    text = (
        'File "/opt/env/lib/python3.12/site-packages/wgsextract_cli/core/x.py", '
        "line 3\nRunning: /usr/bin/samtools view -T /refs/hs38DH.fa.gz in.bam "
        "chrM:1-16569, 8 threads"
    )
    cleaned = redactor.text(text)
    assert "<site-packages>/wgsextract_cli/core/x.py" in cleaned
    assert "<path>/samtools view -T <path>/hs38DH.fa.gz <file>.bam" in cleaned
    assert "chrM:1-16569, 8 threads" in cleaned


def test_command_line_paths_with_spaces_are_reduced_whole() -> None:
    redactor = Redactor(sensitive=["Jane Q Public"])
    argv = ["info", "--input", r"C:\Data\Jane Q Public.bam", "--ref=/r/my ref.fa"]
    assert redactor.argv(argv) == [
        "info",
        "--input",
        "<path>/<file>.bam",
        "--ref=<path>/<file>.fa",
    ]


def test_public_download_urls_keep_their_path() -> None:
    url = "https://ftp.ebi.ac.uk/pub/databases/x/GRCh38_full.fa.gz"
    assert Redactor().text(f"Downloading {url}.") == f"Downloading {url}."


def test_settings_lists_are_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    from wgsextract_cli.core.config import settings

    private = str(Path.home() / "PrivateOwner" / f"{SAMPLE}.vcf.gz")
    monkeypatch.setitem(settings, "vcf_input_paths", [private])
    lines = "\n".join(issue_report.settings_lines(Redactor.for_this_computer()))
    assert 'vcf_input_paths = ["<path>/<file>.vcf.gz"]' in lines
    assert "PrivateOwner" not in lines and SAMPLE not in lines


def test_sample_names_from_the_input_header_are_redacted(tmp_path: Path) -> None:
    from wgsextract_cli.core.dependencies import get_tool_path

    if get_tool_path("samtools") is None:
        pytest.skip("samtools not installed")
    sam = tmp_path / "cohort.sam"
    sam.write_text("@HD\tVN:1.6\n@RG\tID:1\tSM:Ann_Lee\n")
    run = issue_report.RunRecord(
        "last run",
        {
            "command": "vcf clair3",
            "exit_code": 1,
            "status": "failed",
            "error": {"message": "Unexpected read group for Ann_Lee"},
            "provenance": {"params": {"input": str(sam)}},
        },
    )
    report = issue_report.build_report(run=run, probe_tools=lambda used: {})
    assert "Ann_Lee" not in report.url and "Ann_Lee" not in report.diagnostics
    assert "Unexpected read group for <sample>" in report.title


def test_tool_versions_prefer_what_the_run_used() -> None:
    run = issue_report.RunRecord(
        "last run",
        {"command": "info", "provenance": {"tools": {"samtools": "1.20", "x": None}}},
    )
    report = issue_report.build_report(
        run=run, probe_tools=lambda used: {"samtools": "1.24", "x": "2.0"}
    )
    assert "- samtools: 1.20 (used by the run; installed now: 1.24)" in (
        report.diagnostics
    )
    assert "- x: 2.0" in report.diagnostics


def test_link_limit_is_a_bound_or_an_error() -> None:
    with pytest.raises(api.UsageError, match="at least 1000"):
        api.support.build_issue_report(api.ReportIssueParams(max_url_length=500))
    report = api.support.build_issue_report(api.ReportIssueParams())
    url = api.support.issue_url(report, max_length=1000)
    assert len(url) <= 1000
    with pytest.raises(api.UsageError, match="max-url-length"):
        api.support.issue_url(report, max_length=150)


def test_long_report_is_shortened_to_fit_the_link_and_says_so(
    tmp_path: Path,
) -> None:
    from wgsextract_cli.core.last_run import record_last_run
    from wgsextract_cli.core.results import CommandResult

    result = CommandResult(command="vcf snp")
    result.exit_code, result.status = 1, "failed"
    log = [f"INFO step {i}: " + "x" * 150 for i in range(200)]
    record_last_run(result, log_tail=log, log_file=None)

    report = api.support.build_issue_report(
        api.ReportIssueParams(log_lines=100, max_url_length=3000)
    )
    path = api.support.save_issue_report(report, tmp_path)

    assert len(report.url) <= 3000
    assert report.truncated and "log tail cut from 100" in report.truncated[0]
    shown = _issue_fields(report.url)["diagnostics"]
    assert "Shortened to fit in a GitHub link" in shown
    assert report.file_name in shown and path.name == report.file_name
    saved = path.read_text()
    assert "step 199" in saved and "step 100:" in saved and "step 99:" not in saved


def test_edited_report_gets_a_new_link_that_still_fits() -> None:
    import dataclasses

    report = api.support.build_issue_report(api.ReportIssueParams())
    edited = dataclasses.replace(
        report, title="New title", description="I ran it. " * 2000
    )
    url = api.support.issue_url(edited, max_length=4000)
    fields = _issue_fields(url)
    assert fields["title"] == "New title" and len(url) <= 4000


def test_api_and_cli_build_the_same_report(
    capfd: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _fail_info(capfd, tmp_path)
    args = ("--title", "It broke", "--description", "Ran info.", "--log-lines", "5")

    code, out, _ = _cli(capfd, "report-issue", *args, "--json", "--print")
    report = api.support.build_issue_report(
        api.ReportIssueParams(title="It broke", description="Ran info.", log_lines=5)
    )

    cli = json.loads(out)["details"]
    assert code == 0
    assert cli["url"] == report.url
    assert cli["diagnostics"] == report.diagnostics


def test_print_never_opens_the_browser_and_open_does(
    capfd: pytest.CaptureFixture[str], _this_computer: list[str]
) -> None:
    code, out, _ = _cli(capfd, "report-issue", "--print")
    assert code == 0 and _this_computer == []
    assert "https://github.com/theontho/wgsextract-cli/issues/new?" in out

    code, out, _ = _cli(capfd, "report-issue", "--open", "--json")
    assert code == 0
    assert _this_computer == [json.loads(out)["details"]["url"]]
    assert json.loads(out)["details"]["opened"] is True


@pytest.mark.parametrize(("answer", "opens"), [("y", True), ("", False)])
def test_interactive_run_asks_before_opening(
    capfd: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    _this_computer: list[str],
    answer: str,
    opens: bool,
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: answer)

    assert _cli(capfd, "report-issue")[0] == 0
    assert bool(_this_computer) is opens


def test_report_on_a_manifest_or_run_log_and_reject_other_files(
    capfd: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    log = tmp_path / f"{SAMPLE}.log"
    log.write_text(f"INFO Reading {SAMPLE}.bam\nERROR boom in {Path.home()}/x\n")
    log.with_name(log.name + ".provenance.json").write_text(
        json.dumps(
            {
                "argv": ["--log-file", str(log), "vcf", "snp", "-i", "S.bam"],
                "status": "failed",
            }
        )
    )
    report = api.support.build_issue_report(api.ReportIssueParams(run=log))
    assert report.title == "vcf snp failed (exit code 1)"
    assert "ERROR boom in <path>/<name>" in report.diagnostics
    assert SAMPLE not in report.url and SAMPLE not in report.diagnostics

    other = tmp_path / "notes.json"
    other.write_text("{}")
    code, _, err = _cli(capfd, "report-issue", "--run", str(other), "--print")
    assert code == 2 and "not a result manifest" in err
