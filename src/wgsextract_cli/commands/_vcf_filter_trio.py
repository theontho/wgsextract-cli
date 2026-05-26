import argparse
import logging
import os
import subprocess

from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.reference_resolver import ReferenceLibrary
from wgsextract_cli.core.utils import (
    WGSExtractError,
    run_command,
)
from wgsextract_cli.core.variant_files import (
    calculate_bam_md5,
    ensure_vcf_indexed,
    ensure_vcf_prepared,
    verify_paths_exist,
)

from ._vcf_basic import (
    _select_vcf_input,
)


def get_gaps_bed(ref_path: str) -> str | None:
    """Try to locate and convert _nbin.csv to a temporary BED file."""
    import re

    prefix = re.sub(r"\.(fasta|fna|fa)(\.gz)?$", "", ref_path)
    nbin_file = prefix + "_nbin.csv"
    if not os.path.exists(nbin_file):
        return None

    import tempfile

    # Use a secure way to create a temp file
    fd, bed_path = tempfile.mkstemp(suffix=".bed")
    try:
        with os.fdopen(fd, "w") as f_out:
            with open(nbin_file) as f_in:
                # Assume format: chrom, start, end (maybe with header)
                for line in f_in:
                    parts = line.strip().split(",")
                    if len(parts) >= 3:
                        try:
                            chrom, start, end = parts[0], int(parts[1]), int(parts[2])
                            f_out.write(f"{chrom}\t{start}\t{end}\n")
                        except ValueError:
                            continue  # Header or invalid row
        return bed_path
    except OSError as e:
        logging.debug(f"Failed to create gaps BED: {e}")
        if os.path.exists(bed_path):
            os.remove(bed_path)
        return None


def cmd_filter(args: argparse.Namespace) -> None:
    input_file = _select_vcf_input(args)
    if not input_file:
        msg = LOG_MESSAGES["input_required"]
        logging.error(msg)
        raise WGSExtractError(msg)

    if not verify_paths_exist({"--input": input_file}):
        return

    verify_dependencies(["bcftools", "tabix"])
    log_dependency_info(["bcftools", "tabix"])

    logging.debug(f"Input file: {os.path.abspath(input_file)}")

    outdir = (
        args.outdir if args.outdir else os.path.dirname(os.path.abspath(input_file))
    )
    logging.debug(f"Output directory: {os.path.abspath(outdir)}")
    out_vcf = os.path.join(outdir, "filtered.vcf.gz")

    # Resolve reference if needed for gap filtering or gene resolution
    md5_sig = calculate_bam_md5(input_file, None)
    lib = ReferenceLibrary(args.ref, md5_sig, input_path=input_file)
    logging.debug(f"Resolved reference: {lib.fasta}")

    # Gene-based region resolution
    region = args.region
    if args.gene:
        from wgsextract_cli.core.gene_map import GeneMap

        gm = GeneMap(
            lib.root if lib.root else os.path.dirname(os.path.abspath(input_file))
        )
        resolved_region = gm.get_coords(args.gene, lib.build or "hg38")

        if resolved_region:
            logging.info(f"Resolved gene {args.gene} to {resolved_region}")
            region = resolved_region
        else:
            logging.error(f"Could not resolve gene name: {args.gene}")
            return

    region_args = ["-r", region] if region else []
    expr_args = ["-i", args.expr] if args.expr else []

    # Gap-aware filtering
    gaps_bed = None
    exclude_args = []
    if getattr(args, "exclude_near_gaps", False):
        if lib.fasta:
            gaps_bed = get_gaps_bed(lib.fasta)
            if gaps_bed:
                logging.info(f"Using gaps BED for exclusion: {gaps_bed}")
                exclude_args = ["-T", f"^{gaps_bed}"]
            else:
                logging.warning(
                    "Gap exclusion requested but Count Ns output (_nbin.csv) not found."
                )
        else:
            logging.warning(
                "Gap exclusion requested but reference genome not resolved."
            )

    input_vcf = ensure_vcf_prepared(input_file)
    logging.info(LOG_MESSAGES["vcf_filtering"].format(input=input_vcf, output=out_vcf))
    try:
        run_command(
            ["bcftools", "view"]
            + region_args
            + expr_args
            + exclude_args
            + ["-Oz", "-o", out_vcf, input_vcf]
        )
        ensure_vcf_indexed(out_vcf)
    except (OSError, subprocess.SubprocessError, RuntimeError, WGSExtractError) as e:
        logging.error(f"❌: Filtering failed: {e}")
        raise WGSExtractError("VCF filtering failed.") from e
    finally:
        if gaps_bed and os.path.exists(gaps_bed):
            os.remove(gaps_bed)


def cmd_trio(args: argparse.Namespace) -> None:
    from wgsextract_cli.core.variant_files import (
        get_vcf_samples,
        normalize_vcf_chromosomes,
    )

    verify_dependencies(["bcftools", "tabix"])

    proband = args.proband if args.proband else args.vcf_input
    mother = args.mother
    father = args.father

    if not proband or not mother or not father:
        logging.error("Proband, Mother, and Father VCFs are all required.")
        return

    if not verify_paths_exist(
        {"--proband": proband, "--mother": mother, "--father": father}
    ):
        return

    outdir = args.outdir if args.outdir else os.path.dirname(os.path.abspath(proband))

    # 1. Prepare and Normalize
    p_vcf = ensure_vcf_prepared(proband)
    m_vcf = ensure_vcf_prepared(mother)
    f_vcf = ensure_vcf_prepared(father)

    # Get chrom style from proband
    try:
        res = run_command(["bcftools", "index", "-s", p_vcf], capture_output=True)
        target_chroms = [line.split("\t")[0] for line in res.stdout.strip().split("\n")]
    except (OSError, subprocess.SubprocessError, RuntimeError, WGSExtractError) as e:
        logging.warning(f"Failed to infer trio chromosome style from proband: {e}")
        target_chroms = ["chr1"]  # Default to chr

    m_vcf_norm = normalize_vcf_chromosomes(m_vcf, target_chroms)
    f_vcf_norm = normalize_vcf_chromosomes(f_vcf, target_chroms)

    # 2. Merge
    merged_vcf = os.path.join(outdir, "merged_trio_tmp.vcf.gz")
    region_args = ["-r", args.region] if getattr(args, "region", None) else []

    def cleanup_trio_temp_files() -> None:
        for path in [merged_vcf, m_vcf_norm, f_vcf_norm]:
            if path in {m_vcf, f_vcf}:
                continue
            if os.path.exists(path):
                os.remove(path)
            for ext in [".tbi", ".csi"]:
                if os.path.exists(path + ext):
                    os.remove(path + ext)

    try:
        run_command(
            [
                "bcftools",
                "merge",
                "--force-samples",
                "-Oz",
                "-o",
                merged_vcf,
            ]
            + region_args
            + [
                p_vcf,
                m_vcf_norm,
                f_vcf_norm,
            ]
        )
        ensure_vcf_indexed(merged_vcf)
    except (OSError, subprocess.SubprocessError, RuntimeError, WGSExtractError) as e:
        logging.error(f"❌: VCF merge failed: {e}")
        cleanup_trio_temp_files()
        raise WGSExtractError("VCF trio merge failed.") from e

    # 3. Identify sample order
    samples = get_vcf_samples(merged_vcf)
    # Map roles to indices
    p_idx, m_idx, f_idx = 0, 1, 2  # Defaults based on merge order

    def find_sample_idx(path: str, default: int) -> int:
        s_list = get_vcf_samples(path)
        if not s_list:
            return default
        name = s_list[0]
        try:
            return samples.index(name)
        except ValueError:
            # Try fuzzy match
            for i, s in enumerate(samples):
                if name in s or s in name:
                    return i
            return default

    p_idx = find_sample_idx(p_vcf, 0)
    m_idx = find_sample_idx(m_vcf_norm, 1)
    f_idx = find_sample_idx(f_vcf_norm, 2)

    modes = [args.mode] if args.mode != "all" else ["denovo", "recessive", "comphet"]

    for mode in modes:
        out_vcf = os.path.join(outdir, f"trio_{mode}.vcf.gz")
        logging.info(
            LOG_MESSAGES["vcf_trio_analysis"].format(mode=mode, output=out_vcf)
        )

        filter_expr = ""
        if mode == "denovo":
            # Child is het, parents are ref OR missing
            filter_expr = f'GT[{p_idx}]="het" && (GT[{m_idx}]="ref" || GT[{m_idx}]=".") && (GT[{f_idx}]="ref" || GT[{f_idx}]=".")'
        elif mode == "recessive":
            # Child is hom-alt, parents are het
            filter_expr = f'GT[{p_idx}]="hom" && GT[{m_idx}]="het" && GT[{f_idx}]="het"'
        elif mode == "comphet":
            # Simplified: Child is het, one parent is het, other is ref/missing
            filter_expr = f'GT[{p_idx}]="het" && ( (GT[{m_idx}]="het" && (GT[{f_idx}]="ref" || GT[{f_idx}]=".")) || ((GT[{m_idx}]="ref" || GT[{m_idx}]=".") && GT[{f_idx}]="het") )'

        try:
            run_command(
                [
                    "bcftools",
                    "view",
                    "-i",
                    filter_expr,
                    "-Oz",
                    "-o",
                    out_vcf,
                    merged_vcf,
                ]
            )
            ensure_vcf_indexed(out_vcf)
            logging.info(LOG_MESSAGES["vcf_trio_complete"].format(output=out_vcf))

            # Basic summary
            try:
                # Count total
                total_res = run_command(
                    ["bcftools", "view", "-H", out_vcf], capture_output=True
                )
                total_count = total_res.stdout.count("\n")

                # Check if CSQ exists in header
                has_csq = False
                header_res = run_command(
                    ["bcftools", "view", "-h", out_vcf], capture_output=True
                )
                if "ID=CSQ" in header_res.stdout:
                    has_csq = True

                summary_msg = (
                    f"✅: {mode.upper()} results: {total_count} total variants"
                )

                if has_csq:
                    # Count high impact
                    high_res = run_command(
                        ["bcftools", "view", "-H", "-i", 'CSQ~"HIGH"', out_vcf],
                        capture_output=True,
                    )
                    high_count = high_res.stdout.count("\n")
                    summary_msg += f", {high_count} HIGH impact"

                logging.info(summary_msg)
            except (
                OSError,
                subprocess.SubprocessError,
                RuntimeError,
                WGSExtractError,
            ) as e:
                logging.debug(
                    "Could not summarize trio filter output %s: %s", out_vcf, e
                )

        except (
            OSError,
            subprocess.SubprocessError,
            RuntimeError,
            WGSExtractError,
        ) as e:
            logging.error(f"❌: Filtering for {mode} failed: {e}")
            cleanup_trio_temp_files()
            raise WGSExtractError(f"VCF trio filtering failed for {mode}.") from e

    # Cleanup
    cleanup_trio_temp_files()
