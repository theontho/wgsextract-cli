"""``report``: a sample's results as one local HTML, Markdown and JSON report.

The report summarises what earlier runs left in the sample folder (their
result manifests and the sample workspace): overview and QC, lineage,
pharmacogenomics, ClinVar pathogenic calls in the ACMG SF genes (or another
gene list) and the microarray export. It runs no analysis; its only network
use is downloading a built-in gene list once into the reference library::

    res = api.report.build(api.ReportParams(workspace=Path("HG002")))
    report = res.details          # the wgsextract.report/1 document

Every finding has a confidence label, its evidence (file relative to the
sample folder, plus region) and the "technical result, not diagnosis"
disclaimer. See docs/personal-report.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wgsextract_cli.core import gene_lists
from wgsextract_cli.core.results import CommandResult
from wgsextract_cli.core.table_writer import FORMATS, table_format_type
from wgsextract_cli.core.utils import UsageError

from ._params import Params, param
from ._runtime import CancelToken, ProgressCallback, execute
from ._shared import optional_input_param, outdir_param

__all__ = ["ReportParams", "build"]


def _workspace_param() -> Any:
    return param(
        None,
        help="Sample folder whose results are reported (default: the --genome "
        "folder, else the --input's folder).",
        path="directory",
        metavar="DIR",
    )


@dataclass(frozen=True, kw_only=True)
class ReportParams(Params):
    """Summarise a sample's results as <sample>_personal_report.html, .md and .json
    plus a findings table, from the result manifests in its folder."""

    COMMAND = "report"

    input: Path | None = optional_input_param(
        "BAM, CRAM or VCF of the sample: only its runs (and runs on files made "
        "from it) are reported. Its folder holds the results unless workspace "
        "is given."
    )
    workspace: Path | None = _workspace_param()
    outdir: Path | None = outdir_param("the sample folder")
    results: tuple[Path, ...] = param(
        (),
        help="Also read result manifests from this folder (repeatable); only "
        "runs on this sample's files are used. The --outdir is always read.",
        flags=("--results",),
        metavar="DIR",
        action="append",
    )
    table_format: str = param(
        "tsv",
        help="Format of the findings table, <sample>_personal_report_findings.tsv or "
        ".parquet (needs pyarrow) (default: tsv)",
        flags=("--format",),
        choices=FORMATS,
        cli_type=table_format_type(),
    )
    gene_list: str | None = param(
        None,
        help="Genes for the ClinVar section: a built-in list "
        f"({', '.join(gene_lists.builtin_ids())}; downloaded once into the "
        "reference library), or a TSV (column gene; optional hg19_/hg38_ or "
        "build-free chrom, start, end spans) or BED file (gene symbol in column "
        f"4) (default: config report_gene_list, else {gene_lists.default_id()})",
        metavar="LIST",
        config_key="report_gene_list",
        may_name_file=True,
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "results", tuple(Path(p) for p in self.results or () if p)
        )
        super().__post_init__()
        if self.input is None and self.workspace is None:
            raise UsageError("Give --genome, --workspace or --input.")

    @property
    def folder(self) -> Path:
        """The sample folder."""
        if self.workspace is not None:
            return self.workspace.expanduser().absolute()
        assert self.input is not None
        return self.input.expanduser().absolute().parent


def build(
    params: ReportParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Write the report. ``details`` is the ``wgsextract.report/1`` document;
    ``outputs`` are the HTML (``report_html``), Markdown
    (``report_markdown``), JSON (``report_json``) and findings table
    (``findings_table``)."""
    from wgsextract_cli.commands.report import run_report

    return execute(params, run_report, progress=progress, cancel=cancel, check=check)
