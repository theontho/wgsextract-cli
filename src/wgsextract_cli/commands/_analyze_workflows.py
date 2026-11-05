import argparse
import contextlib
import dataclasses
import json
import logging
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Sequence
from contextvars import ContextVar
from pathlib import Path
from typing import Any, TypedDict

from wgsextract_cli.core import variant_confidence
from wgsextract_cli.core.config import config_args
from wgsextract_cli.core.haplogrep_trees import configured_tree_spec
from wgsextract_cli.core.reference_resolver import ReferenceLibrary
from wgsextract_cli.core.results import Scalar, current_result, warn
from wgsextract_cli.core.steps import Decision, Step, StepJournal, StepRun
from wgsextract_cli.core.utils import (
    WGSExtractError,
    run_command,
)
from wgsextract_cli.core.variant_files import (
    calculate_bam_md5,
    ensure_vcf_indexed,
)


def _safe_console_text(value: object) -> str:
    text = str(value)
    encoding = sys.stdout.encoding or "utf-8"
    try:
        text.encode(encoding)
    except UnicodeEncodeError:
        return text.encode(encoding, errors="replace").decode(encoding)
    return text


def _print(value: object = "") -> None:
    print(_safe_console_text(value))


class AnalyzeResult(TypedDict):
    type: str
    input: str
    output: str
    count: int


# Step names for --force-step. annotate and discovery run once per VCF, as
# "annotate:<type>_<name>"; forcing the family forces each of them.
STEPS = (
    "info", "mt-haplogroup", "y-haplogroup", "snp", "indel", "annotate", "discovery",
)  # fmt: skip
JOURNAL = "analyze.steps.json"

# The step running now: its subcommands' result documents add the files they
# read (annotation data, the reference) to its inputs.
_RUNNING: ContextVar[StepRun | None] = ContextVar("analyze_step", default=None)


@contextlib.contextmanager
def _captured(handle: StepRun) -> Iterator[None]:
    """Record in ``handle`` the outputs and metrics the step adds to this run's
    result, so a later run that skips the step can report them again."""
    result = current_result()
    before = {record.path for record in result.outputs}
    metrics = dict(result.metrics)
    token = _RUNNING.set(handle)
    try:
        yield
    finally:
        _RUNNING.reset(token)
    for record in result.outputs:
        if record.path not in before and Path(record.path).exists():
            handle.add_output(
                record.path,
                role=record.role,
                result_kind=record.kind,
                result_metrics=record.metrics,
            )
    handle.metrics["result_metrics"] = {
        name: value for name, value in result.metrics.items()
        if metrics.get(name, object()) != value
    }  # fmt: skip


def _replay(journal: StepJournal, decision: Decision) -> None:
    """Report a skipped step's outputs and metrics as its run did."""
    result = current_result()
    for item in decision.outputs:
        if item.get("role"):
            result.add_output(
                journal.resolve(item["path"]),
                item["role"],
                kind=item.get("result_kind"),
                metrics=_scalars(item.get("result_metrics")),
            )
    result.add_metrics(**_scalars(decision.metrics.get("result_metrics")))


def run_step(
    journal: StepJournal,
    step: Step,
    action: Callable[[StepRun], None],
    *,
    after: Sequence[Decision] = (),
    clean: Sequence[Path] = (),
) -> tuple[Decision, dict[str, Any]]:
    """Run ``action`` as ``step`` unless an earlier run completed it; returns
    the decision and the step's metrics (from this run or the recorded one).

    ``clean``: work folders deleted before the step reruns, so a subcommand
    can't reuse what an earlier attempt left there (kept when the same
    attempt is resumed).
    """
    decision = journal.decide(step, after=after)
    if not decision.run:
        _replay(journal, decision)
        return decision, decision.metrics
    if journal.dry_run:
        return decision, decision.metrics
    if not decision.partial:
        for folder in clean:
            shutil.rmtree(folder, ignore_errors=True)
    with journal.running(decision) as handle, _captured(handle):
        action(handle)
    return decision, handle.metrics


def subcommand_action(
    command: list[str], args: argparse.Namespace
) -> Callable[[StepRun], None]:
    """A step action that runs the wgsextract subcommand ``command``."""

    def action(handle: StepRun) -> None:
        run_cli_subcommand(command, args)

    return action


def _step_params(args: argparse.Namespace, command: Sequence[str]) -> dict[str, Any]:
    """What shapes a subcommand step's output: its command line and --ref."""
    return {"command": list(command), "ref": args.ref}


_SV_NAME_TOKENS = {"sv", "svs", "pbsv", "sniffles"}


def detect_vcf_type(vcf_path: str) -> str:
    """Guess SNV/InDel, CNV, or SV from file-name tokens (e.g. sample_sv.vcf.gz)."""
    tokens = set(re.split(r"[^a-z0-9]+", Path(vcf_path).name.lower()))
    if "cnv" in tokens:
        return "cnv"
    if tokens & _SV_NAME_TOKENS:
        return "sv"
    return "snp-indel"


# bcftools `~` uses POSIX basic regex: "|" and "()" are literals, so match each
# SpliceAI delta score >= 0.8 (fields are "|0.85|" or "|1.00|") separately.
# INFO annotations copied into the discovery confidence table, when present.
DISCOVERY_COLUMNS = (
    "CLNSIG", "CLNDN", "GNOMAD_AF", "REVEL", "SpliceAI", "am_pathogenicity",
)  # fmt: skip
SPLICEAI_HIGH_EXPR = 'INFO/SpliceAI ~ "[|]0[.][89]" || INFO/SpliceAI ~ "[|]1[.]0"'


def run_discovery_filter(
    ann_vcf: str,
    v_type: str,
    out_vcf: str,
    thresholds: variant_confidence.Thresholds | None = None,
) -> int:
    """Dynamically builds filter based on type and headers."""
    try:
        res_h = run_command(
            ["bcftools", "view", "-h", ann_vcf],
            capture_output=True,
        )
        header = res_h.stdout
    except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
        logging.warning(
            f"Annotation header inspection failed; using baseline filter: {e}"
        )
        header = ""

    # Build Logic: Rare OR Pathogenic OR High Impact
    filters = []

    def has_info(tag: str) -> bool:
        # INFO only: a FORMAT tag of the same name (FORMAT/AF) isn't filterable.
        return f"##INFO=<ID={tag}," in header

    # Rare, by gnomAD's population frequency. (A caller's own INFO/AF is the
    # frequency within its call set, 0.5 or 1 for one person, so it isn't used.)
    if has_info("GNOMAD_AF"):
        filters.append('INFO/GNOMAD_AF < 0.01 || INFO/GNOMAD_AF == "."')

    # Pathogenic
    if has_info("CLNSIG"):
        filters.append(
            'INFO/CLNSIG ~ "Pathogenic" || INFO/CLNSIG ~ "Likely_pathogenic"'
        )

    if v_type == "snp-indel":
        if has_info("REVEL"):
            filters.append("INFO/REVEL > 0.7")
        if has_info("SpliceAI"):
            filters.append(SPLICEAI_HIGH_EXPR)
        if has_info("am_pathogenicity"):
            filters.append("INFO/am_pathogenicity > 0.7")

    # Combine with OR
    if filters:
        filter_expr = " || ".join(filters)
    else:
        filter_expr = "QUAL > 30"

    _print(f"Applying Discovery Filter: {filter_expr}")

    if v_type != "snp-indel":
        # Structural and copy-number calls carry no per-allele read evidence
        # the confidence rules understand, so they are filtered unlabelled.
        try:
            run_command(
                [
                    "bcftools",
                    "filter",
                    "-i",
                    filter_expr,
                    "-Oz",
                    "-o",
                    out_vcf,
                    ann_vcf,
                ],
                capture_output=True,
            )
            ensure_vcf_indexed(out_vcf)
            res = run_command(["bcftools", "view", "-H", out_vcf], capture_output=True)
        except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
            raise WGSExtractError(f"Discovery filter failed for {v_type}: {e}") from e
        count = len(res.stdout.strip().split("\n")) if res.stdout.strip() else 0
        _print(f"Found {count} significant variants.")
        current_result().add_output(
            out_vcf, "significant_variants", metrics={"variants": count}
        )
        return count

    columns = tuple(tag for tag in DISCOVERY_COLUMNS if has_info(tag))
    try:
        summary = variant_confidence.label_vcf(
            ann_vcf,
            out_vcf,
            thresholds or variant_confidence.thresholds_from(),
            include=filter_expr,
            summary_columns=columns,
        )
    except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
        raise WGSExtractError(f"Discovery filter failed for {v_type}: {e}") from e
    count = summary.records
    _print(f"Found {count} significant variants.")
    counts = ", ".join(
        f"{summary.counts[label]} {label}" for label in variant_confidence.LABELS
    )
    _print(f"Confidence of the {summary.carried} carried calls: {counts}.")
    table = re.sub(r"\.vcf\.gz$", ".confidence.tsv", out_vcf)
    variant_confidence.write_table(
        table, summary, columns, f"Significant {v_type} variants"
    )
    variant_confidence.record(
        summary,
        output=out_vcf,
        role="significant_variants",
        what=f"significant {v_type} calls",
        table=table,
        clinical=variant_confidence.clinical_count(summary),
    )
    return count


def run_cli_subcommand(cmd_args: list[str], args: argparse.Namespace) -> None:
    """Helper to run a wgsextract subcommand."""
    cmd = [
        sys.executable,
        "-m",
        "wgsextract_cli.main",
        *config_args(keep_awake=getattr(args, "keep_awake", None)),
        *cmd_args,
    ]

    # Use explicit ref if provided, otherwise resolve from env/defaults
    ref_path = args.ref
    if not ref_path:
        # Try to resolve for BAM/CRAM inputs
        input_path = args.input
        md5_sig = (
            calculate_bam_md5(input_path, None)
            if input_path and input_path.lower().endswith((".bam", ".cram"))
            else None
        )
        lib = ReferenceLibrary(None, md5_sig, input_path=input_path)
        ref_path = lib.root

    if ref_path:
        cmd.extend(["--ref", ref_path])

    if args.threads:
        cmd.extend(["--threads", str(args.threads)])
    if args.debug:
        cmd.append("--debug")
    if getattr(args, "force", False):
        cmd.append("--force")

    # The step writes its result document to a file rather than with --json,
    # which would silence the tables and results it prints for the user.
    outdir = None
    if "--outdir" in cmd_args:
        outdir = Path(cmd_args[cmd_args.index("--outdir") + 1])
        outdir.mkdir(parents=True, exist_ok=True)
    with _stage_dir(outdir) as stage:
        result_file = Path(stage, "result.json")
        cmd.extend(["--result-file", str(result_file)])
        try:
            run_command(cmd)
        except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
            record_step_result(result_file)
            logging.warning(f"Subcommand failed: {' '.join(cmd)}. Error: {e}")
            raise WGSExtractError("Subcommand failed: " + " ".join(cmd)) from e
        record_step_result(result_file)


@contextlib.contextmanager
def _stage_dir(outdir: Path | None) -> Iterator[str]:
    """A folder for a step's result document. Each step has its own, so the
    one a killed run left is removed when that step reruns."""
    running = _RUNNING.get()
    if running is None or outdir is None:
        with tempfile.TemporaryDirectory(dir=outdir, prefix=".wgse-step-") as temp:
            yield temp
        return
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", running.decision.step.name)
    stage = outdir / f".wgse-step-{name}"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir()
    try:
        yield str(stage)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _scalars(values: object) -> dict[str, Scalar]:
    if not isinstance(values, dict):
        return {}
    return {
        str(key): value
        for key, value in values.items()
        if value is None or isinstance(value, str | int | float | bool)
    }


def record_step_result(document: Path) -> None:
    """Carry a step's result document into this run's: its manifest, its
    outputs with their metrics and its warnings with their details."""
    try:
        step = json.loads(document.read_text(encoding="utf-8"))
        command = str(step["command"])
        outputs = list(step.get("outputs") or [])
        warnings = list(step.get("warnings") or [])
        inputs = list(step.get("inputs") or [])
        # Callers record their resolved FASTA as provenance, not as an input.
        reference = (step.get("provenance") or {}).get("reference") or {}
        if isinstance(reference, dict) and reference.get("path"):
            inputs.append({"role": "reference", "path": reference["path"]})
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return
    if running := _RUNNING.get():
        declared = {
            str(Path(p).absolute()) for p in running.decision.step.inputs.values() if p
        }
        for item in inputs:
            path = item.get("path") if isinstance(item, dict) else None
            if path and str(Path(path).absolute()) not in declared:
                running.add_input(f"{item.get('role') or 'input'}:{path}", path)
    result = current_result()
    if step.get("manifest"):
        result.add_output(step["manifest"], "step_manifest", kind="json")
    for output in outputs:
        if isinstance(output, dict) and output.get("path") and output.get("role"):
            result.add_output(
                output["path"],
                output["role"],
                kind=output.get("kind"),
                metrics=_scalars(output.get("metrics")),
            )
    for item in warnings:
        if isinstance(item, dict) and item.get("code"):
            # The step already logged it; record it without logging it twice.
            details = {**_scalars(item.get("details")), "step": command}
            result.warn(str(item["code"]), str(item.get("message", "")), **details)


def run_vcf_workflow(
    args: argparse.Namespace,
    vcf_path: str,
    v_type: str,
    outdir: str,
    journal: StepJournal,
    after: Sequence[Decision] = (),
) -> AnalyzeResult | None:
    """Annotates and filters a single VCF file based on its type, as the
    steps annotate and discovery. ``after``: the step that made the VCF."""
    # The name without its VCF suffix: NA12878.snp and NA12878.indel stay apart.
    base_name = re.sub(r"\.(vcf(\.b?gz)?|bcf)$", "", Path(vcf_path).name, flags=re.I)
    _print(f"\nSub-Stage: Processing {v_type.upper()} ({Path(vcf_path).name})")

    # Define targeted annotations per type
    if v_type == "snp-indel":
        # Full chain for SNPs, now including VEP
        anns = (
            "clinvar,revel,phylop,cadd,gerp,gnomad,spliceai,alphamissense,pharmgkb,vep"
        )
    elif v_type == "cnv":
        # ClinVar and gnomAD are most relevant for CNVs
        anns = "clinvar,gnomad"
    else:  # SV
        anns = "clinvar,gnomad"

    # Imported here: vcf.chain -> _vep_run imports detect_vcf_type from this module.
    from wgsextract_cli.commands.vcf.chain import dataset, skip_reason

    lib = ReferenceLibrary(args.ref, None, input_path=vcf_path)
    available = [a for a in anns.split(",") if not skip_reason(a, lib)]
    if not available:
        # A discovery filter over unannotated variants would report a misleading 0.
        warn(
            "annotation_skipped",
            f"Skipping annotation and discovery for {Path(vcf_path).name}: "
            f"no annotation data is installed for {anns}. Download it with "
            "'wgsextract ref clinvar' (and the other 'ref' annotation commands).",
            annotation=anns,
            reason="no annotation data installed",
            path=str(vcf_path),
        )
        return None
    anns = ",".join(available)

    work_dir = Path(outdir, f"work_{v_type}_{base_name}")
    actual_ann_vcf = str(work_dir / "chain_annotated.vcf.gz")
    command = ["vcf", "chain-annotate", "--input", vcf_path,
               "--outdir", str(work_dir), "--annotations", anns]  # fmt: skip

    def annotate(handle: StepRun) -> None:
        work_dir.mkdir(parents=True, exist_ok=True)
        run_cli_subcommand(command, args)
        if Path(actual_ann_vcf).exists():
            handle.add_output(actual_ann_vcf)

    # Updated annotation data (a new ClinVar release) must redo the step.
    inputs = {"vcf": vcf_path}
    caches: dict[str, list[str]] = {}
    for ann in available:
        path = dataset(ann, lib)
        if path and Path(path).is_dir():
            # VEP's cache: a release is a species/version directory.
            caches[ann] = sorted(
                p.relative_to(path).as_posix() for p in Path(path).glob("*/*")
            )
        elif path:
            inputs[f"dataset:{ann}"] = path
    tag = f"{v_type}_{base_name}"
    annotate_step = Step(
        f"annotate:{tag}",
        inputs=inputs,
        params={**_step_params(args, command), "caches": caches},
        tools=["bcftools", *(["vep"] if "vep" in available else [])],
    )
    annotated, _ = run_step(
        journal, annotate_step, annotate, after=after, clean=[work_dir]
    )

    discovery_vcf = str(Path(outdir, f"significant_{v_type}_{base_name}.vcf.gz"))
    thresholds = variant_confidence.thresholds_from(args)
    discovery_step = Step(
        f"discovery:{tag}",
        inputs={"annotated": actual_ann_vcf},
        outputs=[discovery_vcf],
        params={"type": v_type, "thresholds": dataclasses.asdict(thresholds)},
        tools=["bcftools"],
    )
    if journal.dry_run:
        journal.decide(discovery_step, after=[annotated])
        return None

    # Locate the final annotated file
    if not Path(actual_ann_vcf).exists():
        warn(
            "annotation_failed",
            f"Annotation failed for {vcf_path}",
            path=str(vcf_path),
        )
        return None

    def discover(handle: StepRun) -> None:
        count = run_discovery_filter(actual_ann_vcf, v_type, discovery_vcf, thresholds)
        handle.metrics["variants"] = count
        for index in (".tbi", ".csi"):
            if Path(discovery_vcf + index).exists():
                handle.add_output(discovery_vcf + index)

    _, metrics = run_step(journal, discovery_step, discover, after=[annotated])
    count = int(metrics.get("variants") or 0)
    return {"type": v_type, "input": vcf_path, "output": discovery_vcf, "count": count}


def generate_summary_report(results: Sequence[AnalyzeResult], outdir: str) -> None:
    """Prints a nice summary of all findings."""
    _print("\n" + "=" * 60)
    _print("GENOMIC ANALYSIS SUMMARY REPORT")
    _print("=" * 60)

    total_sig = 0
    for res in results:
        _print(f"[{res['type'].upper()}] {Path(res['input']).name}")
        _print(f"  Significant variants: {res['count']}")
        _print(f"  Results file: {res['output']}")
        total_sig += res["count"]
        _print("-" * 30)

    _print(f"\nTOTAL SIGNIFICANT VARIANTS FOUND: {total_sig}")
    _print("=" * 60 + "\n")


def _cached_gender(cache_file: str) -> str:
    try:
        with open(cache_file) as f:
            data = json.load(f)
    except FileNotFoundError:
        return "Unknown"
    except (OSError, json.JSONDecodeError) as e:
        logging.debug("Failed to read cached info metrics from %s: %s", cache_file, e)
        return "Unknown"
    return str(data.get("gender", "Unknown")) if isinstance(data, dict) else "Unknown"


def run_bam_chain(
    args: argparse.Namespace, input_file: str, outdir: str, journal: StepJournal
) -> str:
    """Runs the info and lineage steps. Returns detected gender."""
    _print("\nSTAGE: BAM/CRAM Metrics & Lineage")

    def subcommand_step(
        name: str,
        command: list[str],
        params: dict[str, Any] | None = None,
        **extra: Any,
    ) -> Step:
        return Step(name, inputs={"input": input_file},
                    params=_step_params(args, command) | (params or {}),
                    **extra)  # fmt: skip

    # 1. Info (detailed: shows the table and detects the sex)
    info = ["info", "--detailed", "--input", input_file, "--outdir", outdir]
    cache_file = str(Path(outdir, f"{Path(input_file).name}.wgse_info.json"))

    def run_info(handle: StepRun) -> None:
        run_cli_subcommand(info, args)
        # 2. The detected sex drives the lineage steps.
        handle.metrics["gender"] = _cached_gender(cache_file)

    _, metrics = run_step(
        journal,
        subcommand_step("info", info, outputs=[cache_file], tools=["samtools"]),
        run_info,
    )
    gender = str(metrics.get("gender") or "Unknown")

    # 3. mt-haplogroup (Haplogrep 3)
    for name, command in (
        ("mt-haplogroup", ["lineage", "mt-haplogroup"]),
        ("y-haplogroup", ["lineage", "y-haplogroup"]),
    ):
        # 4. Y-lineage only if it's NOT female
        if name == "y-haplogroup" and gender == "Female":
            logging.info("Skipping Y-lineage analysis for female genome.")
            continue
        command = [*command, "--input", input_file, "--outdir", outdir]
        tools = ["samtools", "bcftools"]
        params: dict[str, Any] = {}
        if name == "mt-haplogroup":
            tools.append("haplogrep3")
            # The configured tree and its pin shape the call, too.
            params["tree"] = configured_tree_spec()
        run_step(
            journal,
            subcommand_step(name, command, params, tools=tools),
            subcommand_action(command, args),
        )

    return gender
