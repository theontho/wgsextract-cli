"""``wgsextract report-issue``: preview a redacted bug report and open it as a
pre-filled GitHub issue."""

from __future__ import annotations

import argparse
import dataclasses
import sys
import textwrap

from wgsextract_cli.api import support
from wgsextract_cli.api._cli import add_arguments
from wgsextract_cli.api._params import from_namespace
from wgsextract_cli.core.issue_report import IssueReport
from wgsextract_cli.core.results import current_result, human_output, json_mode


def register(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    base_parser: argparse.ArgumentParser,
) -> None:
    parser = subparsers.add_parser(
        "report-issue",
        parents=[base_parser],
        help="Report a problem: preview a redacted bug report, then open it "
        "as a pre-filled GitHub issue.",
        description="Build a bug report about this installation and the last "
        "run (or --run FILE), with names, paths, reads, genotypes and secrets "
        "removed. It is shown first and saved locally; nothing is uploaded. "
        "The browser opens GitHub's new-issue form only after you agree (or "
        "with --open), and you submit it yourself.",
    )
    add_arguments(parser, support.ReportIssueParams)
    browser = parser.add_mutually_exclusive_group()
    browser.add_argument(
        "--print",
        "--dry-run",
        dest="print_only",
        action="store_true",
        help="Only print the report and its link; never open the browser.",
    )
    browser.add_argument(
        "--open",
        dest="open_browser",
        action="store_true",
        help="Open the pre-filled issue in the browser without asking.",
    )
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    params = from_namespace(support.ReportIssueParams, args)
    if "outdir" not in getattr(args, "_explicit_dests", set()):
        # Not the configured output_directory: reports go to the data folder.
        params = dataclasses.replace(params, outdir=None)
    report = support.build_issue_report(params)
    path = support.save_issue_report(report, params.outdir)
    result = current_result()
    result.add_output(path, "issue_report", kind="markdown")

    _preview(report, path)
    opened = False
    if args.print_only:
        pass
    elif args.open_browser or _confirmed():
        opened = support.open_issue(report)
        if not opened:
            human_output("Could not start a browser; open the link above yourself.")
    elif not json_mode():
        human_output(
            "Review the report above, then open the link to submit it, or run "
            "again with --open."
        )
    result.set_details({**report.to_dict(), "file": str(path), "opened": opened})


def _preview(report: IssueReport, path: object) -> None:
    rule = "-" * 72
    removed = ", ".join(f"{n} {kind}" for kind, n in report.redactions.items())
    human_output(f"Bug report about: {report.source}")
    human_output(f"Removed for privacy: {removed or 'nothing found'}")
    for note in report.notes:
        human_output(f"Note: {note}")
    human_output(rule)
    human_output(f"Title: {report.title}\n")
    human_output(report.link_diagnostics.rstrip())
    human_output(rule)
    if report.truncated:
        cuts = "; ".join(report.truncated)
        human_output(
            textwrap.fill(
                f"Shortened to fit in the link ({cuts}). The full report is "
                f"saved at {path}; paste from it into the issue if useful.",
                width=78,
            )
        )
    else:
        human_output(f"Saved at {path}")
    human_output(f"\nLink ({len(report.url)} characters):\n{report.url}\n")


def _confirmed() -> bool:
    """Ask on an interactive terminal; never under --json or in a pipe."""
    if json_mode() or not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    try:
        answer = input("Open this report on GitHub in your browser? [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")
