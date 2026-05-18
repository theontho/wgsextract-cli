import json
import logging
import os
import subprocess
import sys

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


def detect_vcf_type(vcf_path):
    """Simple heuristic to detect SNV, CNV, or SV."""
    fname = os.path.basename(vcf_path).lower()
    if "cnv" in fname:
        return "cnv"
    if "sv" in fname:
        return "sv"
    return "snp-indel"


def run_discovery_filter(args, ann_vcf, v_type, out_vcf):
    """Dynamically builds filter based on type and headers."""
    try:
        res_h = run_command(
            ["bcftools", "view", "-h", ann_vcf],
            capture_output=True,
        )
        header = res_h.stdout
    except Exception:
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
            filters.append('INFO/SpliceAI ~ "|0.[89]"')
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
    except Exception as e:
        logging.error(f"Discovery filter failed for {v_type}: {e}")
        return 0


def run_cli_subcommand(cmd_args, args):
    """Helper to run a wgsextract subcommand."""
    cmd = [sys.executable, "-m", "wgsextract_cli.main"] + cmd_args

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
        # For curated commands, we want real-time output to show progress/results.
        # For everything else, we capture to keep the UI clean.
        curated_cmds = ["info", "lineage", "vcf", "vep"]
        should_stream = any(c in cmd_args for c in curated_cmds)

        if should_stream:
            # Run without capturing to allow real-time progress updates to reach the terminal.
            # Internal sub-sub-commands (like bcftools) are still captured at their level.
            run_command(cmd)
        else:
            run_command(cmd, capture_output=True)

    except (subprocess.CalledProcessError, Exception) as e:
        logging.warning(f"Subcommand failed: {' '.join(cmd)}. Error: {e}")
        raise WGSExtractError("Subcommand failed: " + " ".join(cmd)) from e


def run_vcf_workflow(args, vcf_path, v_type, outdir):
    """Annotates and filters a single VCF file based on its type."""
    base_name = os.path.basename(vcf_path).split(".")[0]
    _print(f"\nSub-Stage: Processing {v_type.upper()} ({os.path.basename(vcf_path)})")

    # Define targeted annotations per type
    if v_type == "snp-indel":
        # Full chain for SNPs, now including VEP
        anns = "clinvar,revel,phylop,gnomad,spliceai,alphamissense,pharmgkb,vep"
    elif v_type == "cnv":
        # ClinVar and gnomAD are most relevant for CNVs
        anns = "clinvar,gnomad"
    else:  # SV
        anns = "clinvar,gnomad"

    work_dir = os.path.join(outdir, f"work_{v_type}_{base_name}")
    os.makedirs(work_dir, exist_ok=True)

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
    actual_ann_vcf = os.path.join(work_dir, "chain_annotated.vcf.gz")
    if not os.path.exists(actual_ann_vcf):
        logging.warning(f"Annotation failed for {vcf_path}")
        return None

    # Run Discovery Filter
    discovery_vcf = os.path.join(outdir, f"significant_{v_type}_{base_name}.vcf.gz")
    count = run_discovery_filter(args, actual_ann_vcf, v_type, discovery_vcf)

    return {"type": v_type, "input": vcf_path, "output": discovery_vcf, "count": count}


def generate_summary_report(results, outdir):
    """Prints a nice summary of all findings."""
    _print("\n" + "=" * 60)
    _print("GENOMIC ANALYSIS SUMMARY REPORT")
    _print("=" * 60)

    total_sig = 0
    for res in results:
        _print(f"[{res['type'].upper()}] {os.path.basename(res['input'])}")
        _print(f"  Significant variants: {res['count']}")
        _print(f"  Results file: {res['output']}")
        total_sig += res["count"]
        _print("-" * 30)

    _print(f"\nTOTAL SIGNIFICANT VARIANTS FOUND: {total_sig}")
    _print("=" * 60 + "\n")


def run_bam_chain(args, input_file, outdir):
    """Runs info, qc, and lineage steps. Returns detected gender."""
    _print("\nSTAGE: BAM/CRAM Metrics & Lineage")

    # 1. Info (Detailed run to show table and get gender)
    # We call it even if cached to ensure the table is displayed. It's very fast when cached.
    run_cli_subcommand(
        ["info", "--detailed", "--input", input_file, "--outdir", outdir], args
    )

    # 2. Detect gender from the (newly updated) cache to drive subsequent logic
    gender = "Unknown"
    cache_file = os.path.join(outdir, f"{os.path.basename(input_file)}.wgse_info.json")
    if os.path.exists(cache_file):
        try:
            with open(cache_file) as f:
                data = json.load(f)
                gender = data.get("gender", "Unknown")
        except Exception:
            pass

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
