"""``report``: a sample's results as a local HTML, Markdown and JSON report."""

import argparse
import json
from pathlib import Path

from wgsextract_cli.api._cli import add_arguments, cli_handler
from wgsextract_cli.api.report import ReportParams
from wgsextract_cli.core import personal_report as pr
from wgsextract_cli.core.atomic_output import safe_output
from wgsextract_cli.core.messages import CLI_HELP
from wgsextract_cli.core.results import current_result, human_output
from wgsextract_cli.core.table_writer import (
    check_format,
    provenance,
    table_path,
    write_table,
)
from wgsextract_cli.core.utils import UsageError


def _write_text(path: Path, text: str) -> None:
    with (
        safe_output(path) as staged,
        open(staged, "w", encoding="utf-8", newline="\n") as stream,
    ):
        stream.write(text)


def run_report(params: ReportParams) -> None:
    folder = params.folder
    if not folder.is_dir():
        raise UsageError(f"Sample folder not found: {folder}")
    check_format(params.table_format)
    outdir = (params.outdir or folder).expanduser().absolute()
    outdir.mkdir(parents=True, exist_ok=True)
    extra = list(params.results)
    if outdir != folder:
        extra.append(outdir)
    sources = pr.sample_sources(folder, params.input, extra)
    report = pr.build_report(
        folder, input_file=params.input, sources=sources, gene_list=params.gene_list
    )

    result = current_result()
    for source in sources:
        result.add_input(source.manifest, "manifest")
    stem = outdir / f"{report['sample']['name']}_personal_report"
    base = pr.link_base(folder, outdir)
    html_path = Path(f"{stem}.html")
    md_path = Path(f"{stem}.md")
    json_path = Path(f"{stem}.json")
    table = Path(table_path(outdir / f"{report['sample']['name']}_personal_report_findings",
                            params.table_format))  # fmt: skip
    _write_text(html_path, pr.render_html(report, base))
    _write_text(md_path, pr.render_markdown(report, base))
    _write_text(json_path, json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    rows = write_table(
        table,
        pr.FINDING_COLUMNS,
        pr.table_rows(report),
        params.table_format,
        metadata=provenance([s.manifest for s in sources]),
    )
    result.set_manifest_stem(stem)
    result.add_output(html_path, "report_html")
    result.add_output(md_path, "report_markdown")
    result.add_output(json_path, "report_json")
    result.add_output(table, "findings_table", metrics={"rows": rows})

    sections = report["sections"]
    labels = [f["confidence"] for f in pr.findings(report)]
    result.add_metrics(
        findings=len(labels),
        **{f"findings_{label}": labels.count(label) for label in pr.CONFIDENCE},
        **{f"section_{name}": s["status"] for name, s in sections.items()},
    )
    for name, section in sections.items():
        if section["status"] == "failed":
            result.warn(
                "section_failed",
                f"{section['title']}: {section.get('error', 'the run failed')}",
                section=name,
            )
    result.set_details(report)

    human_output(f"Report of {report['sample']['name']} ({folder})")
    for section in sections.values():
        line = section["summary"] or section["status"].replace("_", " ")
        human_output(f"  {section['title']}: {line}")
        for missing in section["missing"]:
            human_output(f"    not run: {missing['what']}: {missing['command']}")
    human_output(f"\n{pr.NOT_DIAGNOSIS} Open {html_path}")
    human_output(f"Also: {md_path.name}, {json_path.name}, {table.name}")


def register(
    subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",
    base_parser: argparse.ArgumentParser,
) -> None:
    parser = subparsers.add_parser(
        "report",
        parents=[base_parser],
        help=CLI_HELP["cmd_report"],
        description=CLI_HELP["cmd_report"],
    )
    add_arguments(parser, ReportParams)
    parser.set_defaults(func=cli_handler(ReportParams, run_report))


__all__ = ["register", "run_report"]
