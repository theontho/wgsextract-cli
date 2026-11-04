import argparse
import json
import logging
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TypedDict

from wgsextract_cli.core import variant_confidence
from wgsextract_cli.core.config import config_args
from wgsextract_cli.core.reference_resolver import ReferenceLibrary
from wgsextract_cli.core.results import Scalar, current_result, warn
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
        "--json",
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

    try:
        # Only the result document is captured: the step's progress goes to
        # stderr, which still reaches the terminal live.
        proc = run_command(cmd, stdout=subprocess.PIPE)
    except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
        record_step_result(getattr(e, "stdout", None))
        logging.warning(f"Subcommand failed: {' '.join(cmd)}. Error: {e}")
        raise WGSExtractError("Subcommand failed: " + " ".join(cmd)) from e
    record_step_result(proc.stdout)


def _scalars(values: object) -> dict[str, Scalar]:
    if not isinstance(values, dict):
        return {}
    return {
        str(key): value
        for key, value in values.items()
        if value is None or isinstance(value, str | int | float | bool)
    }


def record_step_result(document: str | bytes | None) -> None:
    """Carry a step's ``--json`` result into this run's: its manifest, its
    outputs with their metrics and its warnings with their details."""
    try:
        step = json.loads(document or "")
        command = str(step["command"])
        outputs = list(step.get("outputs") or [])
        warnings = list(step.get("warnings") or [])
    except (ValueError, KeyError, TypeError, AttributeError):
        return
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
    args: argparse.Namespace, vcf_path: str, v_type: str, outdir: str
) -> AnalyzeResult | None:
    """Annotates and filters a single VCF file based on its type."""
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
    from wgsextract_cli.commands.vcf.chain import skip_reason

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

    work_dir = str(Path(outdir, f"work_{v_type}_{base_name}"))
    Path(work_dir).mkdir(parents=True, exist_ok=True)

    run_cli_subcommand(
        [
            "vcf",
            "chain-annotate",
            "--input",
            vcf_path,
            "--outdir",
            work_dir,
            "--annotations",
            anns,
        ],
        args,
    )

    # Locate the final annotated file
    actual_ann_vcf = str(Path(work_dir, "chain_annotated.vcf.gz"))
    if not Path(actual_ann_vcf).exists():
        warn(
            "annotation_failed",
            f"Annotation failed for {vcf_path}",
            path=str(vcf_path),
        )
        return None

    # Run Discovery Filter
    discovery_vcf = str(Path(outdir, f"significant_{v_type}_{base_name}.vcf.gz"))
    count = run_discovery_filter(
        actual_ann_vcf,
        v_type,
        discovery_vcf,
        variant_confidence.thresholds_from(args),
    )

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


def run_bam_chain(args: argparse.Namespace, input_file: str, outdir: str) -> str:
    """Runs info, qc, and lineage steps. Returns detected gender."""
    _print("\nSTAGE: BAM/CRAM Metrics & Lineage")

    # 1. Info (Detailed run to show table and get gender)
    # We call it even if cached to ensure the table is displayed. It's very fast when cached.
    run_cli_subcommand(
        ["info", "--detailed", "--input", input_file, "--outdir", outdir], args
    )

    # 2. Detect gender from the (newly updated) cache to drive subsequent logic
    gender = "Unknown"
    cache_file = str(Path(outdir, f"{Path(input_file).name}.wgse_info.json"))
    if Path(cache_file).exists():
        try:
            with open(cache_file) as f:
                data = json.load(f)
                if isinstance(data, dict):
                    gender = str(data.get("gender", "Unknown"))
        except (OSError, json.JSONDecodeError) as e:
            logging.debug(
                "Failed to read cached info metrics from %s: %s", cache_file, e
            )

    # 3. mt-haplogroup (Haplogrep)
    run_cli_subcommand(
        ["lineage", "mt-haplogroup", "--input", input_file, "--outdir", outdir], args
    )

    # 4. Y-lineage only if it's NOT female
    if gender != "Female":
        run_cli_subcommand(
            ["lineage", "y-haplogroup", "--input", input_file, "--outdir", outdir], args
        )
    else:
        logging.info("Skipping Y-lineage analysis for female genome.")

    return gender
