import argparse
import json
import logging
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TypedDict

from wgsextract_cli.core.config import config_args
from wgsextract_cli.core.reference_resolver import ReferenceLibrary
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
SPLICEAI_HIGH_EXPR = 'INFO/SpliceAI ~ "[|]0[.][89]" || INFO/SpliceAI ~ "[|]1[.]0"'


def run_discovery_filter(ann_vcf: str, v_type: str, out_vcf: str) -> int:
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

    # Rare
    if "ID=GNOMAD_AF," in header:
        filters.append('INFO/GNOMAD_AF < 0.01 || INFO/GNOMAD_AF == "."')
    elif "ID=AF," in header:
        filters.append('INFO/AF < 0.01 || INFO/AF == "."')

    # Pathogenic
    if "ID=CLNSIG," in header:
        filters.append('INFO/CLNSIG ~ "Pathogenic"')

    if v_type == "snp-indel":
        if "ID=REVEL," in header:
            filters.append("INFO/REVEL > 0.7")
        if "ID=SpliceAI," in header:
            filters.append(SPLICEAI_HIGH_EXPR)
        if "ID=am_pathogenicity," in header:
            filters.append("INFO/am_pathogenicity > 0.7")

    # Combine with OR
    if filters:
        filter_expr = " || ".join(filters)
    else:
        filter_expr = "QUAL > 30"

    _print(f"Applying Discovery Filter: {filter_expr}")

    try:
        run_command(
            ["bcftools", "filter", "-i", filter_expr, "-Oz", "-o", out_vcf, ann_vcf],
            capture_output=True,
        )
        ensure_vcf_indexed(out_vcf)
        res = run_command(["bcftools", "view", "-H", out_vcf], capture_output=True)
        count = len(res.stdout.strip().split("\n")) if res.stdout.strip() else 0
        _print(f"Found {count} significant variants.")
        return count
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(f"Discovery filter failed for {v_type}: {e}") from e


def run_cli_subcommand(cmd_args: list[str], args: argparse.Namespace) -> None:
    """Helper to run a wgsextract subcommand."""
    cmd = [sys.executable, "-m", "wgsextract_cli.main", *config_args(), *cmd_args]

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
        # Not captured, so the subcommand's progress reaches the terminal live.
        run_command(cmd)
    except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
        logging.warning(f"Subcommand failed: {' '.join(cmd)}. Error: {e}")
        raise WGSExtractError("Subcommand failed: " + " ".join(cmd)) from e


def run_vcf_workflow(
    args: argparse.Namespace, vcf_path: str, v_type: str, outdir: str
) -> AnalyzeResult | None:
    """Annotates and filters a single VCF file based on its type."""
    base_name = Path(vcf_path).name.split(".")[0]
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
        logging.warning(
            f"Skipping annotation and discovery for {Path(vcf_path).name}: "
            f"no annotation data is installed for {anns}. Download it with "
            "'wgsextract ref clinvar' (and the other 'ref' annotation commands)."
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
        logging.warning(f"Annotation failed for {vcf_path}")
        return None

    # Run Discovery Filter
    discovery_vcf = str(Path(outdir, f"significant_{v_type}_{base_name}.vcf.gz"))
    count = run_discovery_filter(actual_ann_vcf, v_type, discovery_vcf)

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
