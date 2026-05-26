import argparse
import csv
import io
import json
import logging
import os
import time

from wgsextract_cli.core.alignment_metadata import get_bam_header
from wgsextract_cli.core.constants import (
    REFERENCE_MODELS,
    REFGEN_BY_SNCOUNT,
)
from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.variant_files import (
    calculate_bam_md5,
    is_sorted,
    verify_paths_exist,
)

from .metrics import (
    determine_sequencer,
    generate_chrom_table,
    get_file_stats,
    load_n_counts,
    parse_idxstats,
    run_body_sample,
)
from .render import (
    render_info,
    run_full_coverage,
    run_sampled_coverage,
)


def run(args: argparse.Namespace) -> None:
    from wgsextract_cli.core.variant_files import resolve_reference

    start_time = time.time()
    verify_dependencies(["samtools"])
    log_dependency_info(["samtools"])
    if not args.input:
        return logging.error("--input required.")

    if not verify_paths_exist({"--input": args.input}):
        return

    logging.debug(f"Input file: {os.path.abspath(args.input)}")

    # Fetch header once to avoid redundant subprocess calls
    t0 = time.time()
    # If it's a CRAM and we have a ref, try to resolve it early to help samtools read the header
    initial_ref = None
    if args.input.lower().endswith(".cram") and args.ref:
        resolved = resolve_reference(args.ref, None)
        if resolved and os.path.isfile(resolved):
            initial_ref = resolved
            logging.debug(
                f"CRAM detected, using resolved reference for header: {initial_ref}"
            )
        else:
            logging.debug("CRAM detected, but reference is not installed or invalid.")

    header = get_bam_header(args.input, cram_opt=initial_ref)
    if not header:
        logging.error(
            f"Could not read header from {args.input}. Samtools might be missing, file is corrupt, or it requires a reference (-T)."
        )
        return
    logging.debug(f"Header fetch took {time.time() - t0:.3f}s")

    # Calculate MD5 from header to identify reference genome
    t0 = time.time()
    md5_sig = calculate_bam_md5(args.input, header=header)
    logging.debug(f"MD5 signature: {md5_sig} (took {time.time() - t0:.3f}s)")

    t0 = time.time()
    resolved_ref = resolve_reference(args.ref, md5_sig)
    logging.debug(f"Resolved reference: {resolved_ref} (took {time.time() - t0:.3f}s)")

    if getattr(args, "info_cmd", None) in ["calculate-coverage", "coverage-sample"]:
        args.detailed = True
    if args.detailed and args.input.lower().endswith(".cram") and not resolved_ref:
        return logging.error("--ref required for detailed mode with CRAM.")

    outdir = (
        args.outdir
        if hasattr(args, "outdir") and args.outdir
        else os.path.dirname(os.path.abspath(args.input))
    )
    # Ensure outdir exists if explicitly provided
    if hasattr(args, "outdir") and args.outdir:
        os.makedirs(outdir, exist_ok=True)

    logging.debug(f"Output directory: {os.path.abspath(outdir)}")
    json_cache = os.path.join(outdir, f"{os.path.basename(args.input)}.wgse_info.json")

    data = {}
    if os.path.exists(json_cache):
        logging.debug(f"Cache file found: {json_cache}")
        try:
            with open(json_cache) as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {}
            # If we only wanted fast mode and we have it, we can return early
            if not args.detailed and data.get("avg_read_len"):
                logging.debug("Cache hit for fast metrics.")
                print(render_info(data, detailed=False))
                return
            # If we wanted detailed mode and we have the chrom table, we can return early
            if (
                args.detailed
                and data.get("chrom_table_csv")
                and getattr(args, "info_cmd", None)
                not in ["calculate-coverage", "coverage-sample"]
            ):
                logging.debug("Cache hit for detailed metrics.")
                if getattr(args, "csv", False):
                    print(data.get("chrom_table_csv", ""), end="")
                else:
                    print(render_info(data, detailed=True))
                return
            logging.debug(
                f"Cache miss or partial: detailed={args.detailed}, has_read_len={bool(data.get('avg_read_len'))}, has_chrom_table={bool(data.get('chrom_table_csv'))}"
            )
        except (OSError, json.JSONDecodeError, TypeError) as e:
            logging.debug(f"Cache read error: {e}")
            pass
    else:
        logging.debug(f"No cache file at {json_cache}")

    # 1. FAST METRICS (Compute if missing from cache)
    if not data.get("avg_read_len"):
        logging.debug("Generating fast metrics...")
        cram_opt = []
        if resolved_ref and os.path.isfile(resolved_ref):
            cram_opt = ["-T", resolved_ref]

        t0 = time.time()
        sorted_status = is_sorted(args.input, cram_opt, header=header)
        logging.debug(f"Sort check: {sorted_status} (took {time.time() - t0:.3f}s)")

        t0 = time.time()
        size_gb, indexed = get_file_stats(args.input)
        logging.debug(
            f"File stats: {size_gb:.2f}GB, indexed={indexed} (took {time.time() - t0:.3f}s)"
        )

        from wgsextract_cli.core.variant_files import get_file_version

        file_version = get_file_version(args.input)

        # Get SN count from header for fast mode
        num_sns = len([line for line in header.splitlines() if line.startswith("@SQ")])

        ref_model_name, ref_mito, ref_fname = REFERENCE_MODELS.get(
            md5_sig, ("Unknown", "", "")
        )

        # Guess from SN count if MD5 is unknown
        if ref_model_name == "Unknown" and num_sns in REFGEN_BY_SNCOUNT:
            # Entry format: [is_primary, filename, mito_tag]
            _, ref_fname_raw, ref_mito_raw = REFGEN_BY_SNCOUNT[num_sns]
            ref_fname = str(ref_fname_raw)
            ref_mito = str(ref_mito_raw)
            # Deduce name from filename
            if "hg19" in ref_fname.lower() or "grch37" in ref_fname.lower():
                ref_model_name = "hg19"
            elif "hg38" in ref_fname.lower() or "grch38" in ref_fname.lower():
                ref_model_name = "hg38"
            else:
                ref_model_name = ref_fname.split(".")[0]

        # Guess from filename if still unknown
        if ref_model_name == "Unknown" and resolved_ref:
            bn = os.path.basename(resolved_ref).upper()
            if any(x in bn for x in ["38", "HG38", "GRCH38"]):
                ref_model_name = "hg38"
            elif any(x in bn for x in ["37", "HG19", "GRCH37"]):
                ref_model_name = "hg19"
            elif any(x in bn for x in ["T2T", "CHM13"]):
                ref_model_name = "t2t"
            elif "19" in bn:
                ref_model_name = "hg19"

        ref_model_str = (
            f"{ref_model_name} (Chr), {ref_mito}, {num_sns} SNs"
            if ref_model_name != "Unknown"
            else f"Unknown, {num_sns} SNs"
        )

        t0 = time.time()
        (
            count,
            avg_len,
            std_len,
            avg_tlen,
            std_tlen,
            is_paired,
            first_qname,
        ) = run_body_sample(args.input, cram_opt)
        logging.debug(f"Body sampling took {time.time() - t0:.3f}s")

        sequencer = determine_sequencer(first_qname)

        # Populate data dict with fast metrics
        data.update(
            {
                "filename": os.path.basename(args.input),
                "md5_signature": md5_sig,
                "file_stats": {
                    "sorted": sorted_status,
                    "indexed": indexed,
                    "size_gb": size_gb,
                    "version": file_version,
                },
                "ref_model_str": ref_model_str,
                "suggested_ref_file": ref_fname,
                "avg_read_len": avg_len,
                "std_read_len": std_len,
                "is_paired": is_paired,
                "avg_insert_size": avg_tlen,
                "std_insert_size": std_tlen,
                "sequencer": sequencer,
                "first_qname": first_qname,
            }
        )

    if not args.detailed:
        print(render_info(data, detailed=False))
        # Save cache even in fast mode
        try:
            with open(json_cache, "w") as f:
                json.dump(data, f, indent=2)
            logging.debug(f"Fast metrics cached to {json_cache}")
        except (OSError, TypeError) as exc:
            logging.warning(
                "Failed to write fast metrics cache %s: %s", json_cache, exc
            )
        logging.debug(f"Total info time: {time.time() - start_time:.3f}s")
        return

    # 2. DETAILED METRICS (Compute if missing from cache)
    if not data.get("chrom_table_csv") or getattr(args, "info_cmd", None) in [
        "calculate-coverage",
        "coverage-sample",
    ]:
        t0 = time.time()
        idx_stats, genome_len, total_mapped, total_unmapped = parse_idxstats(args.input)
        logging.debug(f"Idxstats took {time.time() - t0:.3f}s")

        total_reads = total_mapped + total_unmapped

        # Extract variables from data for calculations
        avg_len = data["avg_read_len"]
        ref_model_name = (
            data["ref_model_str"].split(" (")[0]
            if " (" in data["ref_model_str"]
            else "Unknown"
        )

        cov_file, sample_file = (
            os.path.join(outdir, f"{os.path.basename(args.input)}_bincvg.csv"),
            os.path.join(outdir, f"{os.path.basename(args.input)}_samplecvg.json"),
        )

        info_cmd = getattr(args, "info_cmd", None)
        region = getattr(args, "region", None)

        coverage_map = {}
        if info_cmd == "calculate-coverage":
            run_full_coverage(args.input, resolved_ref, cov_file, region=region)
            if os.path.exists(cov_file) and os.path.getsize(cov_file) > 120:
                with open(cov_file) as f:
                    for line in f.readlines()[1:]:
                        p = line.split("\t")
                        if len(p) > 7:
                            coverage_map[
                                p[0].upper().replace("CHR", "").replace("MT", "M")
                            ] = f"{(int(p[2]) / int(p[7])) * 100:.0f} %"
        elif info_cmd == "coverage-sample":
            run_sampled_coverage(
                args.input, resolved_ref, idx_stats, sample_file, region=region
            )
            if os.path.exists(sample_file):
                try:
                    with open(sample_file) as f:
                        coverage_map = json.load(f)
                    if not isinstance(coverage_map, dict):
                        coverage_map = {}
                except (OSError, json.JSONDecodeError, TypeError) as exc:
                    logging.warning(
                        "Failed to read sampled coverage %s: %s", sample_file, exc
                    )

        y_reads = next(
            (
                s["mapped"]
                for s in idx_stats
                if s["name"].upper().replace("CHR", "") == "Y"
            ),
            0,
        )
        x_reads = next(
            (
                s["mapped"]
                for s in idx_stats
                if s["name"].upper().replace("CHR", "") == "X"
            ),
            0,
        )
        gender = (
            ("Male" if y_reads > (x_reads * 0.05) else "Female")
            if x_reads > 0
            else "Unknown"
        )

        n_counts = load_n_counts(resolved_ref)
        if n_counts:
            data["refined_ns"] = True

        chrom_table = generate_chrom_table(
            idx_stats, avg_len, gender, ref_model_name, coverage_map, n_counts=n_counts
        )
        total_row = next(r for r in chrom_table if r[1] == "Total")
        mapped_segs = total_row[4] + next(
            (r[4] for r in chrom_table if r[1] == "Other"), 0
        )

        # Detailed metrics update
        data.update(
            {
                "gender": gender,
                "file_content": ", ".join(
                    [
                        k
                        for k, v in {
                            "Auto": any(
                                s["mapped"] > 0
                                and s["name"].upper().replace("CHR", "").isdigit()
                                for s in idx_stats
                            ),
                            "X": x_reads > 0,
                            "Y": y_reads > 0,
                            "Mito": any(
                                s["mapped"] > 0
                                and s["name"].upper().replace("CHR", "") in ["M", "MT"]
                                for s in idx_stats
                            ),
                            "Other": any(
                                s["mapped"] > 0
                                and s["name"] != "*"
                                and not s["name"].upper().replace("CHR", "").isdigit()
                                and s["name"].upper().replace("CHR", "")
                                not in ["X", "Y", "M", "MT"]
                                for s in idx_stats
                            ),
                        }.items()
                        if v
                    ]
                    + (["Unmap"] if total_unmapped > 0 else [])
                ),
                "metrics": {
                    "ard_mapped": (total_row[4] * avg_len)
                    / (total_row[2] - total_row[3] + 0.0001),
                    "ard_raw": (total_reads * avg_len)
                    / (total_row[2] - total_row[3] + 0.0001),
                    "gbases_mapped": (mapped_segs * avg_len) / (10**9),
                    "gbases_raw": (total_reads * avg_len) / (10**9),
                    "reads_mapped_m": mapped_segs / 1_000_000,
                    "reads_raw_m": total_reads / 1_000_000,
                    "reads_mapped_pct": (mapped_segs / total_reads * 100)
                    if total_reads > 0
                    else 0,
                    "reads_raw_pct": 100.0,
                },
            }
        )

        si = io.StringIO()
        cw = csv.writer(si)
        cw.writerow(
            [
                "Seq Name",
                "Model Len",
                "Model N Len",
                "# Segs Map",
                "Map Gbases",
                "Map ARD",
                "Breadth Coverage",
            ]
        )
        for row in chrom_table:
            cw.writerow(
                [
                    row[1],
                    row[2],
                    row[3],
                    row[4],
                    f"{row[5]:.2f}",
                    f"{row[6]:.0f}",
                    row[7],
                ]
            )
        data["chrom_table_csv"] = si.getvalue()

    # Common detailed output and final save
    from wgsextract_cli.core.warnings import print_warning

    if data.get("avg_read_len", 0) > 410:
        print_warning("LongReadSequenceWarning")
    if data.get("metrics", {}).get("ard_mapped", 0) < 10:
        print_warning("LowCoverageWarning")

    if getattr(args, "csv", False):
        print(data["chrom_table_csv"], end="")
    else:
        print(render_info(data, detailed=True))
        try:
            with open(json_cache, "w") as f:
                json.dump(data, f, indent=2)
            print(LOG_MESSAGES["info_metrics_cached"].format(path=json_cache))
        except (OSError, TypeError) as exc:
            logging.warning(
                "Failed to write detailed metrics cache %s: %s", json_cache, exc
            )
