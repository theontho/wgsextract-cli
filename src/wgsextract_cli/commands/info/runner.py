import argparse
import csv
import io
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from wgsextract_cli.api._cli import cli_handler
from wgsextract_cli.api.info import (
    CalculateCoverageParams,
    CoverageSampleParams,
    InfoParams,
)
from wgsextract_cli.core import sample_workspace
from wgsextract_cli.core.alignment_health import alignment_health
from wgsextract_cli.core.alignment_metadata import get_bam_header
from wgsextract_cli.core.builds import (
    build_from_chr1_length,
    build_from_path,
    canonical_build,
)
from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import LOG_MESSAGES, SYSTEM_MESSAGES
from wgsextract_cli.core.paths import absolute_path
from wgsextract_cli.core.reference_assets import is_fasta_path
from wgsextract_cli.core.reference_models import identify_reference, sequence_lengths
from wgsextract_cli.core.results import current_result, human_output
from wgsextract_cli.core.sequencing import platform_from_metadata
from wgsextract_cli.core.table_writer import (
    Column,
    check_format,
    provenance,
    table_path,
    write_table,
)
from wgsextract_cli.core.temp_directory import configured_temp_directory
from wgsextract_cli.core.ultima import read_groups
from wgsextract_cli.core.utils import UsageError, WGSExtractError
from wgsextract_cli.core.variant_files import (
    calculate_bam_md5,
    is_sorted,
    verify_paths_exist,
)

from .metrics import (
    DEPTH_COUNTED,
    DEPTH_SAMPLED,
    LONG_READ_MIN_LEN,
    SEX_INFERENCE_VERSION,
    SexCall,
    contig_key,
    determine_sequencer,
    generate_chrom_table,
    get_file_stats,
    infer_sex_call,
    load_n_counts,
    parse_idxstats,
    record_sex,
    run_body_sample,
)
from .render import (
    region_file_tag,
    render_info,
    run_full_coverage,
    run_sampled_coverage,
    sampled_mapped_bases,
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


def _print_detailed(
    data: dict[str, Any], as_csv: bool, sample: tuple[str, str]
) -> None:
    _record_info(data)
    _record_facts(data, *sample)
    if as_csv:
        human_output(data["chrom_table_csv"], end="")
    else:
        human_output(render_info(data, detailed=True))


CHROMOSOME_TABLE = (
    Column("seq_name", "TEXT", "Contig, or Total, or Other (all other contigs)."),
    Column("model_len", "INTEGER", "Reference length in bases."),
    Column("model_n_len", "INTEGER", "N bases in the reference."),
    Column("mapped_segments", "INTEGER", "Mapped reads (segments)."),
    Column("mapped_gbases", "REAL", "Mapped gigabases: segments x read length.", ".2f"),
    Column("mapped_ard", "INTEGER", "Average read depth over the non-N bases."),
    Column(
        "breadth_pct",
        "REAL",
        "Percentage of bases covered by a read (see the breadth metadata).",
        "g",
    ),
)


def _qc_column(name: str, type_: str, description: str) -> Column:
    return Column(name, type_, description, ".6g" if type_ == "REAL" else "")


# One row: the manifest's info metrics, typed.
QC_TABLE = tuple(
    _qc_column(*column)
    for column in (
        ("reference_model", "TEXT", "Reference model the header matches."),
        ("sequencer", "TEXT", "Sequencer guessed from the read names and header."),
        ("sex", "TEXT", "Sex inferred from chrX/chrY coverage."),
        ("avg_read_len", "REAL", "Mean read length (sampled)."),
        ("is_paired", "BOOLEAN", "Paired-end reads."),
        ("avg_insert_size", "REAL", "Mean insert size (sampled)."),
        ("size_gb", "REAL", "File size in GiB."),
        ("sorted", "BOOLEAN", "Coordinate-sorted."),
        ("indexed", "BOOLEAN", "Has an index."),
        ("duplicates_marked", "BOOLEAN", "Duplicates are flagged."),
        ("duplicate_pct", "REAL", "Percentage of sampled reads flagged duplicate."),
        ("alt_aware", "BOOLEAN", "ALT contigs were mapped ALT-aware."),
        ("ard_mapped", "REAL", "Average read depth of the mapped reads."),
        ("ard_raw", "REAL", "Average read depth of all reads."),
        ("gbases_mapped", "REAL", "Mapped gigabases."),
        ("gbases_raw", "REAL", "Gigabases of all reads."),
        ("reads_mapped_m", "REAL", "Mapped reads, in millions."),
        ("reads_raw_m", "REAL", "All reads, in millions."),
        ("reads_mapped_pct", "REAL", "Mapped reads as a percentage of all reads."),
    )
)


def _qc_row(metrics: dict[str, Any]) -> tuple[Any, ...]:
    def value(column: Column) -> Any:
        raw = metrics.get(column.name)
        if raw is None or column.type in ("TEXT", "BOOLEAN"):
            return raw
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    return tuple(value(column) for column in QC_TABLE)


def _chromosome_rows(table_csv: str) -> list[tuple[Any, ...]]:
    """The typed rows of the chromosome table that `--csv` prints."""
    rows = []
    for row in list(csv.reader(io.StringIO(table_csv)))[1:]:
        name, length, n_len, segments, gbases, ard, breadth = row
        percent = breadth.lstrip("~").rstrip("% ").strip()
        rows.append(
            (
                name,
                int(length),
                int(n_len),
                int(segments),
                float(gbases),
                int(float(ard)),
                float(percent) if percent else None,
            )
        )
    return rows


def _export_tables(
    data: dict[str, Any], stem: Path, table_format: str, metadata: dict[str, str]
) -> None:
    """Save the chromosome table and the QC stats as typed tables."""
    _export_chromosomes(
        data, table_path(f"{stem}_chromosomes", table_format), table_format, metadata
    )
    result = current_result()
    qc_path = table_path(f"{stem}_qc", table_format)
    write_table(
        qc_path,
        QC_TABLE,
        [_qc_row(dict(result.metrics))],
        table_format,
        metadata=metadata,
    )
    result.add_output(qc_path, "qc_table", metrics={"rows": 1})


def _export_chromosomes(
    data: dict[str, Any], path: str, table_format: str, metadata: dict[str, str]
) -> None:
    table_csv = data["chrom_table_csv"]
    breadth = (
        "sampled estimate (coverage-sample)"
        if "~" in table_csv
        else "exact (calculate-coverage)"
        if "%" in table_csv
        else "not measured: run info calculate-coverage or coverage-sample"
    )
    rows = _chromosome_rows(table_csv)
    write_table(
        path,
        CHROMOSOME_TABLE,
        rows,
        table_format,
        metadata={**metadata, "breadth": breadth},
    )
    current_result().add_output(path, "chromosome_table", metrics={"rows": len(rows)})
    logging.info(f"Chromosome table saved to {path}")


def _print_fast(data: dict[str, Any], sample: tuple[str, str]) -> None:
    _record_info(data)
    _record_facts(data, *sample)
    human_output(render_info(data, detailed=False))


# Cache bookkeeping that isn't a result.
_PRIVATE_KEYS = {
    "chrom_table_csv",
    "contig_bases",
    "input_signature",
    "first_qname",
    "sex_call",
}


def _record_facts(data: dict[str, Any], input_file: str, header: str) -> None:
    """Record what `info` found in the input's sample workspace."""
    build = canonical_build(build_from_chr1_length(sequence_lengths(header)))
    if build:
        sample_workspace.record(
            "build",
            input_file,
            build,
            method="alignment header contigs",
            confidence="high",
        )
    platform = platform_from_metadata(header, data.get("first_qname"))
    if platform:
        has_pl = any(group.get("PL") for group in read_groups(header))
        sample_workspace.record(
            "platform",
            input_file,
            platform,
            method="read-group PL tag" if has_pl else "first read name pattern",
            confidence="high" if has_pl else "medium",
        )
    if data.get("avg_read_len"):
        sample_workspace.record(
            "read_length",
            input_file,
            round(float(data["avg_read_len"]), 1),
            method="mean length of sampled reads",
            confidence="high",
        )
    if (
        data.get("gender")
        and data.get("sex_inference_version") == SEX_INFERENCE_VERSION
    ):
        call = data.get("sex_call") or (
            data["gender"],
            "cached `info --detailed` call",
            "high",
        )
        record_sex(input_file, SexCall(*call))
    ard = (data.get("metrics") or {}).get("ard_mapped")
    # Only depths from current bookkeeping: not a cache from before #427, nor
    # long reads whose depth couldn't be sampled.
    if ard and data.get("contig_bases"):
        sample_workspace.record(
            "mean_coverage",
            input_file,
            round(float(ard), 2),
            method=data.get("depth_method") or DEPTH_COUNTED,
            confidence="high",
        )


def _record_info(data: dict[str, Any]) -> None:
    """Put the metrics `info` reports on the result manifest."""
    result = current_result()
    details = {k: v for k, v in data.items() if k not in _PRIVATE_KEYS}
    if data.get("chrom_table_csv"):
        details["chromosomes"] = list(
            csv.DictReader(io.StringIO(data["chrom_table_csv"]))
        )
    result.set_details(details)
    stats = data.get("file_stats") or {}
    health = data.get("alignment_health") or {}
    result.add_metrics(
        reference_model=data.get("ref_model_str"),
        sequencer=data.get("sequencer"),
        avg_read_len=data.get("avg_read_len"),
        is_paired=data.get("is_paired"),
        avg_insert_size=data.get("avg_insert_size"),
        size_gb=stats.get("size_gb"),
        sorted=stats.get("sorted"),
        indexed=stats.get("indexed"),
        duplicates_marked=health.get("duplicates_marked"),
        duplicate_pct=health.get("duplicate_pct"),
        alt_aware=health.get("alt_aware"),
    )
    if "gender" in data:
        result.add_metrics(sex=data["gender"], **data.get("metrics", {}))
        if data["gender"] == "Unknown":
            result.warn(
                "sex_unknown",
                "The sample's sex could not be inferred from its chrX/chrY reads.",
            )
        if data.get("avg_read_len", 0) > LONG_READ_MIN_LEN:
            result.warn(
                "long_reads",
                SYSTEM_MESSAGES["LongReadSequenceWarning"],
                avg_read_len=data["avg_read_len"],
            )
        ard = data.get("metrics", {}).get("ard_mapped")
        if ard is not None and ard < 10:
            result.warn(
                "low_coverage",
                SYSTEM_MESSAGES["LowCoverageWarning"],
                target="genome",
                depth=round(ard, 3),
                threshold=10,
            )


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
        "sequencer": determine_sequencer(first_qname, header),
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
    path = _coverage_path(info_cmd, input_p, outdir, region)
    stem = str(Path(outdir, Path(input_p).name))
    if info_cmd == "calculate-coverage":
        return run_full_coverage(
            input_p,
            resolved_ref,
            path,
            region=region,
            # Whole-file results from older releases (samtools depth | awk).
            legacy_p=None if region else f"{stem}_bincvg.csv",
        )
    if info_cmd == "coverage-sample":
        return run_sampled_coverage(input_p, resolved_ref, idx_stats, path, region)
    return {}


def _coverage_path(
    info_cmd: str | None, input_p: str, outdir: str, region: str | None
) -> str:
    stem = str(Path(outdir, Path(input_p).name)) + region_file_tag(region)
    if info_cmd == "calculate-coverage":
        return f"{stem}_coverage.tsv"
    return f"{stem}_samplecvg.json"


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
    mapped_bases: dict[str, float] | None = None,
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
    call = infer_sex_call(idx_stats)
    gender = call.sex

    chrom_table = generate_chrom_table(
        idx_stats,
        avg_len,
        gender,
        ref_model_name,
        coverage_map,
        n_counts=n_counts,
        mapped_bases=mapped_bases,
    )
    total_row = next(r for r in chrom_table if r[1] == "Total")
    other_row = next((r for r in chrom_table if r[1] == "Other"), None)
    mapped_segs = total_row[4] + (other_row[4] if other_row else 0)
    total_bases = total_row[8] + (other_row[8] if other_row else 0)
    eff_len = total_row[2] - total_row[3] + 0.0001
    # Measured mapped bases exclude soft clips, so the unmapped reads are added
    # to them, rather than every record counted at the mean read length.
    raw_bases = (
        total_bases + total_unmapped * avg_len
        if mapped_bases
        else total_reads * avg_len
    )

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
        "sex_call": list(call),
        "sex_inference_version": SEX_INFERENCE_VERSION,
        "refined_ns": bool(n_counts),
        "file_content": ", ".join(k for k, v in content.items() if v),
        "depth_method": DEPTH_SAMPLED if mapped_bases else DEPTH_COUNTED,
        # Marks the depths current; left out when long reads couldn't be
        # sampled, so the next run (or genotype QC) tries again.
        "contig_bases": {row[1]: round(row[8]) for row in chrom_table}
        if mapped_bases or avg_len <= LONG_READ_MIN_LEN
        else None,
        "metrics": {
            "ard_mapped": total_row[8] / eff_len,
            "ard_raw": raw_bases / eff_len,
            "gbases_mapped": total_bases / (10**9),
            "gbases_raw": raw_bases / (10**9),
            "reads_mapped_m": mapped_segs / 1_000_000,
            "reads_raw_m": total_reads / 1_000_000,
            "reads_mapped_pct": (mapped_segs / total_reads * 100)
            if total_reads > 0
            else 0,
            "reads_raw_pct": 100.0,
        },
        "chrom_table_csv": si.getvalue(),
    }


InfoLikeParams = InfoParams | CalculateCoverageParams | CoverageSampleParams


def run_info(params: InfoLikeParams, *, as_csv: bool = False) -> None:
    """``info`` and its coverage subcommands; ``as_csv`` prints the detailed
    table as CSV (CLI only)."""
    from wgsextract_cli.core.variant_files import resolve_reference
    from wgsextract_cli.core.warnings import print_warning

    start_time = time.time()
    verify_dependencies(["samtools"])
    log_dependency_info(["samtools"])
    input_file = str(params.input)
    ref = str(params.ref) if params.ref else None
    outdir_arg = str(params.outdir) if params.outdir else None

    verify_paths_exist({"--input": input_file})
    logging.debug(f"Input file: {str(absolute_path(input_file))}")

    table_format = params.table_format
    check_format(table_format)
    info_cmd = None if isinstance(params, InfoParams) else params.COMMAND.split()[-1]
    region = None if isinstance(params, InfoParams) else params.region
    detailed = (
        not isinstance(params, InfoParams) or params.detailed or bool(table_format)
    )
    bins = isinstance(params, CalculateCoverageParams) and params.bins
    # Only `info` can be told not to save its cache (as a read-only server does).
    cache = not isinstance(params, InfoParams) or params.cache

    # If it's a CRAM and we have a ref, resolve it early so samtools can read the header
    initial_ref = None
    if input_file.lower().endswith(".cram") and ref:
        resolved = resolve_reference(ref, None)
        if resolved and Path(resolved).is_file():
            initial_ref = resolved
        else:
            logging.debug("CRAM detected, but reference is not installed or invalid.")

    t0 = time.time()
    header = get_bam_header(input_file, cram_opt=initial_ref)
    if not header:
        raise WGSExtractError(
            f"Could not read header from {input_file}. Samtools might be missing, "
            "the file is corrupt, or it requires a reference (--ref)."
        )
    logging.debug(f"Header fetch took {time.time() - t0:.3f}s")

    md5_sig = calculate_bam_md5(input_file, header=header)
    resolved_ref = resolve_reference(ref, md5_sig)
    logging.debug(f"MD5 signature: {md5_sig}; resolved reference: {resolved_ref}")
    build = build_from_chr1_length(sequence_lengths(header))
    if resolved_ref and Path(resolved_ref).is_file():
        build = build or build_from_path(Path(resolved_ref).name)
        current_result().set_reference(resolved_ref, build)
    metadata = provenance(
        [input_file],
        reference_build=build,
        sorted_by="the reference's contig order",
        region=region or "",
    )

    if bins:
        from .bins import run_depth_bins

        if input_file.lower().endswith(".cram") and (
            not resolved_ref or not Path(resolved_ref).is_file()
        ):
            raise UsageError("--ref required for detailed mode with CRAM.")
        bins_tsv = run_depth_bins(
            input_file,
            resolved_ref,
            sequence_lengths(header),
            outdir_arg or str(absolute_path(input_file).parent),
            region,
            table_format or "tsv",
            metadata,
        )
        current_result().add_output(bins_tsv, "coverage_bins")
        return

    if detailed and input_file.lower().endswith(".cram") and not resolved_ref:
        raise UsageError("--ref required for detailed mode with CRAM.")

    outdir = outdir_arg or str(absolute_path(input_file).parent)
    if outdir_arg and (cache or detailed):
        Path(outdir).mkdir(parents=True, exist_ok=True)
    logging.debug(f"Output directory: {str(absolute_path(outdir))}")

    user_fasta = ref if ref and is_fasta_path(ref) else None
    ref_model = _guess_ref_model(md5_sig, header, user_fasta)

    json_cache = str(Path(outdir, f"{Path(input_file).name}.wgse_info.json"))
    table_stem = Path(outdir, Path(input_file).name + region_file_tag(region))
    signature = _input_signature(input_file)
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
    # Likewise the sequencer, from the cached first read name and the header.
    if data.get("avg_read_len"):
        data["sequencer"] = determine_sequencer(data.get("first_qname"), header)

    if data.get("avg_read_len") and "alignment_health" not in data:
        # A cache from before these checks existed.
        cram_ref = (
            resolved_ref if resolved_ref and Path(resolved_ref).is_file() else None
        )
        data["alignment_health"] = alignment_health(input_file, cram_ref)
        if cache:
            _save_cache(json_cache, data)

    if info_cmd is None:
        if not detailed and data.get("avg_read_len"):
            logging.debug("Cache hit for fast metrics.")
            _print_fast(data, (input_file, header))
            return
        # The chromosome table hides chrY for a Female, so it is redone along
        # with the sex when the cached call came from an older infer_sex.
        # Likewise a table from before long reads' depth was measured.
        if (
            detailed
            and data.get("chrom_table_csv")
            and data.get("contig_bases")
            and data.get("sex_inference_version") == SEX_INFERENCE_VERSION
        ):
            logging.debug("Cache hit for detailed metrics.")
            _print_detailed(data, as_csv, (input_file, header))
            if table_format:
                _export_tables(data, table_stem, table_format, metadata)
            return

    if not data.get("avg_read_len"):
        logging.debug("Generating fast metrics...")
        data = _fast_metrics(input_file, header, md5_sig, resolved_ref, ref_model)
    data["input_signature"] = signature

    if not detailed:
        _print_fast(data, (input_file, header))
        if cache:
            _save_cache(json_cache, data)
        logging.debug(f"Total info time: {time.time() - start_time:.3f}s")
        return

    t0 = time.time()
    idx_stats, _genome_len, _total_mapped, total_unmapped = parse_idxstats(input_file)
    logging.debug(f"Idxstats took {time.time() - t0:.3f}s")

    coverage_map = _coverage_map(
        info_cmd, input_file, resolved_ref, idx_stats, outdir, region
    )
    mapped_bases = None
    if data.get("avg_read_len", 0) > LONG_READ_MIN_LEN:
        try:
            mapped_bases = sampled_mapped_bases(
                input_file,
                resolved_ref,
                idx_stats,
                configured_temp_directory() or outdir,
            )
        except (OSError, WGSExtractError) as e:
            logging.warning(
                f"Could not sample the depth of {Path(input_file).name}, so long "
                f"reads' depth is estimated from their records and overstated: {e}"
            )
    data.update(
        _detailed_metrics(
            data,
            idx_stats,
            total_unmapped,
            coverage_map,
            load_n_counts(resolved_ref),
            mapped_bases,
        )
    )

    if data.get("avg_read_len", 0) > LONG_READ_MIN_LEN:
        print_warning("LongReadSequenceWarning")
    if data["metrics"]["ard_mapped"] < 10:
        print_warning("LowCoverageWarning")

    _print_detailed(data, as_csv, (input_file, header))
    if table_format:
        _export_tables(data, table_stem, table_format, metadata)
    # A region-limited coverage table describes only part of the file, so it
    # must not replace the whole-file cache.
    if cache and not region and _save_cache(json_cache, data):
        logging.info(LOG_MESSAGES["info_metrics_cached"].format(path=json_cache))
    logging.debug(f"Total info time: {time.time() - start_time:.3f}s")


INFO_PARAMS: dict[str | None, type[InfoLikeParams]] = {
    None: InfoParams,
    "calculate-coverage": CalculateCoverageParams,
    "coverage-sample": CoverageSampleParams,
}


def run(args: argparse.Namespace) -> None:
    """CLI handler of `info` and its coverage subcommands."""
    cls = INFO_PARAMS[getattr(args, "info_cmd", None)]
    cli_handler(cls, run_info, as_csv="csv")(args)
