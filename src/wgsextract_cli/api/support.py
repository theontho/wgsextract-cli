"""``report-issue``: a pre-filled, redacted GitHub bug report.

``build_issue_report`` collects this installation's versions, OS, CPU, RAM,
tools and settings and, by default, the last run recorded on this computer
(command line, exit status, error, warnings, log tail), redacts them, and
fits them into a GitHub new-issue link::

    report = api.support.build_issue_report(api.ReportIssueParams(last_failed=True))
    print(report.body)                     # review it
    api.support.open_issue(report)         # the user submits it on GitHub

Nothing is uploaded: the link opens GitHub's new-issue form in the browser,
where the user reviews and submits it. Reports never contain genotypes, reads,
file contents, sample, user or computer names, full paths or secrets.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from wgsextract_cli.core.issue_report import (
    DEFAULT_LOG_LINES,
    MAX_URL_LENGTH,
    IssueReport,
    build_report,
    load_run,
    open_in_browser,
)
from wgsextract_cli.core.issue_report import issue_url as _issue_url
from wgsextract_cli.core.results import CommandResult

from ._params import Params, param

__all__ = [
    "IssueReport",
    "ReportIssueParams",
    "build_issue_report",
    "issue_url",
    "open_issue",
    "reports_directory",
    "save_issue_report",
]


@dataclass(frozen=True, kw_only=True)
class ReportIssueParams(Params):
    """Build a redacted GitHub bug report about this installation and a run
    (by default the last one recorded on this computer)."""

    COMMAND = "report-issue"

    run: Path | None = param(
        None,
        help="Report on this run: a result manifest (*.manifest.json), a run "
        "log (*.log) or its *.provenance.json (default: the last run).",
        flags=("--run",),
        path="file",
        exclusive="run",
        metavar="FILE",
    )
    last_failed: bool = param(
        False,
        help="Report on the last failed run instead of the last run.",
        flags=("--last-failed",),
        action="store_true",
        exclusive="run",
    )
    title: str | None = param(
        None,
        help="Issue title (default: the command and its error).",
        flags=("--title",),
    )
    description: str | None = param(
        None,
        help="What you did and what went wrong, for the 'What happened?' field.",
        flags=("--description",),
    )
    log_lines: int = param(
        DEFAULT_LOG_LINES,
        help=f"Log lines to include, from the end (default: {DEFAULT_LOG_LINES}).",
        flags=("--log-lines",),
        metavar="N",
    )
    max_url_length: int = param(
        MAX_URL_LENGTH,
        help="Longest link to produce; diagnostics are shortened to fit "
        f"(default: {MAX_URL_LENGTH}).",
        flags=("--max-url-length",),
        metavar="N",
    )
    outdir: Path | None = param(
        None,
        doc="Folder to save the full report in (default: 'reports' in the "
        "per-user data folder).",
        flags=("--outdir", "-o"),
        path="directory",
        shared=True,
    )


def build_issue_report(
    params: ReportIssueParams, *, result: CommandResult | None = None
) -> IssueReport:
    """Collect, redact and fit a report; writes nothing and opens nothing.

    ``result`` reports on that run (e.g. one the API just returned) instead of
    ``params.run`` or the last recorded run. Raises ``UsageError`` when
    ``params.run`` is not a readable manifest, run log or provenance file."""
    from wgsextract_cli.core.config import ensure_settings_loaded
    from wgsextract_cli.core.issue_report import RunRecord

    ensure_settings_loaded()
    run: RunRecord | None
    if result is not None:
        run = RunRecord(f"result of '{result.command}'", result.to_dict())
    else:
        run = load_run(params.run, last_failed=params.last_failed)
    return build_report(
        run=run,
        title=params.title,
        description=params.description,
        log_lines=params.log_lines,
        max_url_length=params.max_url_length,
    )


def reports_directory() -> Path:
    """Where reports are saved by default: ``reports`` in the data folder, beside
    ``reference`` and ``genomes``, so uninstalling removes them too."""
    from wgsextract_cli.core.runtime import repo_root

    return repo_root() / "reports"


def issue_url(report: IssueReport, *, max_length: int = MAX_URL_LENGTH) -> str:
    """The new-issue link for ``report`` after editing its ``title``,
    ``description`` or ``link_diagnostics`` (``dataclasses.replace``); the
    diagnostics are cut at a line if the link would exceed ``max_length``."""
    return _issue_url(report, max_length=max_length)


def save_issue_report(report: IssueReport, directory: Path | None = None) -> Path:
    """Save the full redacted report (``report.body``) as Markdown in
    ``directory`` (default: ``reports`` in the per-user data folder)."""
    return report.save(directory or reports_directory())


def open_issue(report: IssueReport) -> bool:
    """Open ``report.url`` in the browser; False when no browser could start.
    Only call this after the user has seen the report and agreed."""
    return open_in_browser(report.url)
