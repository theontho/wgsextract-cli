import argparse
import csv
import io
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from wgsextract_cli.core.alignment_health import alignment_health
from wgsextract_cli.core.alignment_metadata import get_bam_header
from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.paths import absolute_path
from wgsextract_cli.core.reference_assets import is_fasta_path
from wgsextract_cli.core.reference_models import identify_reference, sequence_lengths
from wgsextract_cli.core.utils import WGSExtractError
from wgsextract_cli.core.variant_files import (
    calculate_bam_md5,
    is_sorted,
    verify_paths_exist,
)

from .metrics import (
    contig_key,
    determine_sequencer,
    generate_chrom_table,
    get_file_stats,
    infer_sex,
    load_n_counts,
    parse_idxstats,
    run_body_sample,
)
from .render import (
    region_file_tag,
    render_info,
    run_full_coverage,
    run_sampled_coverage,
)

COVERAGE_COMMANDS = ("calculate-coverage", "coverage-sample")


def _input_signature(path: str) -> dict[str, int]:
    st = os.stat(path)
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}


def _load_cache(path: str, signature: dict[str, int]) -> dict[str, Any]:
    """Load the metrics cache, discarding it unless it was built from this exact input.

    Caches written before input signatures were recorded are treated as stale.
    """
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        logging.debug(f"No cache file at {path}")
        return {}
    except (OSError, ValueError) as e:
        logging.debug(f"Ignoring unreadable cache {path}: {e}")
        return {}
    if not isinstance(data, dict) or data.get("input_signature") != signature:
        logging.debug(f"Ignoring stale cache {path} (input changed or old format)")
        return {}
    logging.debug(f"Cache file found: {path}")
    return data


def _save_cache(path: str, data: dict[str, Any]) -> bool:
    try:
        with open(path, "w", newline="\n") as f:
            json.dump(data, f, indent=2)
    except (OSError, TypeError) as exc:
        logging.warning(f"Failed to write metrics cache {path}: {exc}")
        return False
    logging.debug(f"Metrics cached to {path}")
    return True


def _print_detailed(data: dict[str, Any], as_csv: bool) -> None:
    if as_csv:
        print(data["chrom_table_csv"], end="")
    else:
        print(render_info(data, detailed=True))


def _guess_ref_model(
    md5_sig: str | None, header: str, user_fasta: str | None
) -> tuple[str, str]:
    """Return (reference model description, suggested reference filename).

    A FASTA name counts as evidence only when the user named that FASTA; one a
    library search picked says nothing about the input.
    """
    model = identify_reference(header, md5_sig, user_fasta)
    if model is None:
        num_sns = len(sequence_lengths(header))
        return f"Unknown, {num_sns} SNs", ""
    logging.debug(f"Reference identified as {model.name} by {model.evidence}")
    return model.describe(), model.fasta


def _fast_metrics(
    input_p: str,
    header: str,
    md5_sig: str | None,
    resolved_ref: str | None,
    ref_model: tuple[str, str],
) -> dict[str, Any]:
    from wgsextract_cli.core.variant_files import get_file_version

    cram_opt = (
        ["-T", resolved_ref] if resolved_ref and Path(resolved_ref).is_file() else []
    )

    t0 = time.time()
    sorted_status = is_sorted(input_p, cram_opt, header=header)
    logging.debug(f"Sort check: {sorted_status} (took {time.time() - t0:.3f}s)")

    size_gb, indexed = get_file_stats(input_p)
    ref_model_str, ref_fname = ref_model

    t0 = time.time()
    _count, avg_len, std_len, avg_tlen, std_tlen, is_paired, first_qname = (
        run_body_sample(input_p, cram_opt)
    )
    logging.debug(f"Body sampling took {time.time() - t0:.3f}s")

    return {
        "filename": Path(input_p).name,
        "md5_signature": md5_sig,
        "file_stats": {
            "sorted": sorted_status,
            "indexed": indexed,
            "size_gb": size_gb,
            "version": get_file_version(input_p),
        },
        "ref_model_str": ref_model_str,
        "suggested_ref_file": ref_fname,
        "avg_read_len": avg_len,
        "std_read_len": std_len,
        "is_paired": is_paired,
        "avg_insert_size": avg_tlen,
        "std_insert_size": std_tlen,
        "sequencer": determine_sequencer(first_qname),
        "first_qname": first_qname,
        "alignment_health": alignment_health(
            input_p, cram_opt[1] if cram_opt else None
        ),
    }


def _coverage_map(
    info_cmd: str | None,
    input_p: str,
    resolved_ref: str | None,
    idx_stats: list[Any],
    outdir: str,
    region: str | None,
) -> dict[str, str]:
    stem = str(Path(outdir, Path(input_p).name))
    tag = region_file_tag(region)
    if info_cmd == "calculate-coverage":
        return run_full_coverage(
            input_p,
            resolved_ref,
            f"{stem}{tag}_coverage.tsv",
            region=region,
            # Whole-file results from older releases (samtools depth | awk).
            legacy_p=None if region else f"{stem}_bincvg.csv",
        )
    if info_cmd == "coverage-sample":
        return run_sampled_coverage(
            input_p, resolved_ref, idx_stats, f"{stem}{tag}_samplecvg.json", region
        )
    return {}


def reference_model_name(described: str) -> str:
    """The model in a `ReferenceModel.describe()` line: "hg38 (Chr), rCRS, 195
    SNs" and "Cat_Fca126, , 71 SNs" (no chr1, so no naming) alike."""
    return described.split(",", 1)[0].split(" (", 1)[0]


def _detailed_metrics(
    data: dict[str, Any],
    idx_stats: list[Any],
    total_unmapped: int,
    coverage_map: dict[str, str],
    n_counts: dict[str, int],
) -> dict[str, Any]:
    total_reads = sum(s["mapped"] + s["unmapped"] for s in idx_stats)
    avg_len = data["avg_read_len"]
    ref_model_str = data["ref_model_str"]
    ref_model_name = reference_model_name(ref_model_str)

    mapped_by_key: dict[str, int] = {}
    for s in idx_stats:
        if s["name"] != "*":
            key = contig_key(s["name"])
            mapped_by_key[key] = mapped_by_key.get(key, 0) + s["mapped"]
    gender = infer_sex(idx_stats)

    chrom_table = generate_chrom_table(
        idx_stats, avg_len, gender, ref_model_name, coverage_map, n_counts=n_counts
    )
    total_row = next(r for r in chrom_table if r[1] == "Total")
    mapped_segs = total_row[4] + next((r[4] for r in chrom_table if r[1] == "Other"), 0)
    eff_len = total_row[2] - total_row[3] + 0.0001

    present = {k for k, v in mapped_by_key.items() if v > 0}
    content = {
        "Auto": any(k.isdigit() for k in present),
        "X": "X" in present,
        "Y": "Y" in present,
        "Mito": "M" in present,
        "Other": any(not k.isdigit() and k not in {"X", "Y", "M"} for k in present),
        "Unmap": total_unmapped > 0,
    }

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
            [row[1], row[2], row[3], row[4], f"{row[5]:.2f}", f"{row[6]:.0f}", row[7]]
        )

    return {
        "gender": gender,
        "refined_ns": bool(n_counts),
        "file_content": ", ".join(k for k, v in content.items() if v),
        "metrics": {
            "ard_mapped": (total_row[4] * avg_len) / eff_len,
            "ard_raw": (total_reads * avg_len) / eff_len,
            "gbases_mapped": (mapped_segs * avg_len) / (10**9),
            "gbases_raw": (total_reads * avg_len) / (10**9),
            "reads_mapped_m": mapped_segs / 1_000_000,
            "reads_raw_m": total_reads / 1_000_000,
            "reads_mapped_pct": (mapped_segs / total_reads * 100)
            if total_reads > 0
            else 0,
            "reads_raw_pct": 100.0,
        },
        "chrom_table_csv": si.getvalue(),
    }


def run(args: argparse.Namespace) -> None:
    from wgsextract_cli.core.variant_files import resolve_reference
    from wgsextract_cli.core.warnings import print_warning

    start_time = time.time()
    verify_dependencies(["samtools"])
    log_dependency_info(["samtools"])
    if not args.input:
        raise WGSExtractError("--input required.")

    verify_paths_exist({"--input": args.input})
    logging.debug(f"Input file: {str(absolute_path(args.input))}")

    info_cmd = getattr(args, "info_cmd", None)
    region = getattr(args, "region", None) if info_cmd in COVERAGE_COMMANDS else None
    detailed = bool(args.detailed or info_cmd in COVERAGE_COMMANDS)
    as_csv = bool(getattr(args, "csv", False))

    # If it's a CRAM and we have a ref, resolve it early so samtools can read the header
    initial_ref = None
    if args.input.lower().endswith(".cram") and args.ref:
        resolved = resolve_reference(args.ref, None)
        if resolved and Path(resolved).is_file():
            initial_ref = resolved
        else:
            logging.debug("CRAM detected, but reference is not installed or invalid.")

    t0 = time.time()
    header = get_bam_header(args.input, cram_opt=initial_ref)
    if not header:
        raise WGSExtractError(
            f"Could not read header from {args.input}. Samtools might be missing, "
            "the file is corrupt, or it requires a reference (--ref)."
        )
    logging.debug(f"Header fetch took {time.time() - t0:.3f}s")

    md5_sig = calculate_bam_md5(args.input, header=header)
    resolved_ref = resolve_reference(args.ref, md5_sig)
    logging.debug(f"MD5 signature: {md5_sig}; resolved reference: {resolved_ref}")

    if info_cmd == "calculate-coverage" and getattr(args, "bins", False):
        from .bins import run_depth_bins

        if args.input.lower().endswith(".cram") and (
            not resolved_ref or not Path(resolved_ref).is_file()
        ):
            raise WGSExtractError("--ref required for detailed mode with CRAM.")
        run_depth_bins(
            args.input,
            resolved_ref,
            sequence_lengths(header),
            args.outdir or str(absolute_path(args.input).parent),
            region,
        )
        return

    if detailed and args.input.lower().endswith(".cram") and not resolved_ref:
        raise WGSExtractError("--ref required for detailed mode with CRAM.")

    outdir = args.outdir or str(absolute_path(args.input).parent)
    if args.outdir:
        Path(outdir).mkdir(parents=True, exist_ok=True)
    logging.debug(f"Output directory: {str(absolute_path(outdir))}")

    user_fasta = args.ref if args.ref and is_fasta_path(args.ref) else None
    ref_model = _guess_ref_model(md5_sig, header, user_fasta)

    json_cache = str(Path(outdir, f"{Path(args.input).name}.wgse_info.json"))
    signature = _input_signature(args.input)
    data = _load_cache(json_cache, signature)
    # Classifying the header is cheap, so it is redone every run. A cache made
    # when it said otherwise (older detection, or a different --ref) is stale,
    # including its chromosome table, which depends on the build.
    if data and data.get("ref_model_str") != ref_model[0]:
        logging.debug(
            f"Ignoring cache {json_cache}: it was made for reference "
            f"{data.get('ref_model_str')!r}, not {ref_model[0]!r}"
        )
        data = {}

    if data.get("avg_read_len") and "alignment_health" not in data:
        # A cache from before these checks existed.
        cram_ref = (
            resolved_ref if resolved_ref and Path(resolved_ref).is_file() else None
        )
        data["alignment_health"] = alignment_health(args.input, cram_ref)
        _save_cache(json_cache, data)

    if info_cmd is None:
        if not detailed and data.get("avg_read_len"):
            logging.debug("Cache hit for fast metrics.")
            print(render_info(data, detailed=False))
            return
        if detailed and data.get("chrom_table_csv"):
            logging.debug("Cache hit for detailed metrics.")
            _print_detailed(data, as_csv)
            return

    if not data.get("avg_read_len"):
        logging.debug("Generating fast metrics...")
        data = _fast_metrics(args.input, header, md5_sig, resolved_ref, ref_model)
    data["input_signature"] = signature

    if not detailed:
        print(render_info(data, detailed=False))
        _save_cache(json_cache, data)
        logging.debug(f"Total info time: {time.time() - start_time:.3f}s")
        return

    t0 = time.time()
    idx_stats, _genome_len, _total_mapped, total_unmapped = parse_idxstats(args.input)
    logging.debug(f"Idxstats took {time.time() - t0:.3f}s")

    coverage_map = _coverage_map(
        info_cmd, args.input, resolved_ref, idx_stats, outdir, region
    )
    data.update(
        _detailed_metrics(
            data, idx_stats, total_unmapped, coverage_map, load_n_counts(resolved_ref)
        )
    )

    if data.get("avg_read_len", 0) > 410:
        print_warning("LongReadSequenceWarning")
    if data["metrics"]["ard_mapped"] < 10:
        print_warning("LowCoverageWarning")

    _print_detailed(data, as_csv)
    # A region-limited coverage table describes only part of the file, so it
    # must not replace the whole-file cache.
    if not region and _save_cache(json_cache, data):
        logging.info(LOG_MESSAGES["info_metrics_cached"].format(path=json_cache))
    logging.debug(f"Total info time: {time.time() - start_time:.3f}s")
