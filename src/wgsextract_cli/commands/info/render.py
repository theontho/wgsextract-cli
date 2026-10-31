import csv
import io
import json
import logging
import re
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.paths import absolute_path
from wgsextract_cli.core.utils import WGSExtractError, run_command, run_pipeline
from wgsextract_cli.core.variant_files import chromosome_aliases

from .metrics import IdxStat, contig_key, is_primary_contig


def _health_lines(health: Mapping[str, Any]) -> list[str]:
    """How the alignment was made: duplicates, quality binning, ALT mapping."""
    lines = []
    if health.get("duplicates_marked") is True:
        lines.append(
            f"{'Duplicates Marked':<28}Yes ({health.get('duplicate_pct')}% of reads "
            "near chr1:20 Mb)"
        )
    elif health.get("duplicates_marked") is False:
        lines.append(f"{'Duplicates Marked':<28}No (realign marks them)")
    levels = health.get("quality_levels")
    if levels:
        kind = "binned" if levels <= 8 else "full resolution"
        lines.append(f"{'Base Quality Values':<28}{levels} ({kind})")
    if health.get("alt_aware") is True:
        lines.append(f"{'ALT-aware Mapping':<28}Yes")
    elif health.get("alt_aware") is False:
        lines.append(
            f"{'ALT-aware Mapping':<28}No: reads in ALT-covered regions (HLA among "
            "them) are lost; realign recovers them"
        )
    return lines


def render_info(data: Mapping[str, Any], detailed: bool = False) -> str:
    """Generate the formatted console output string."""
    output_lines = []
    if detailed and data.get("chrom_table_csv"):
        reader = list(csv.DictReader(io.StringIO(data["chrom_table_csv"])))

        # Check if we actually have any coverage data to show
        has_coverage = any(
            r.get("Breadth Coverage") for r in reader if r.get("Breadth Coverage")
        )

        header_line1 = LOG_MESSAGES["info_header1"]
        header_line2 = LOG_MESSAGES["info_header2"]
        if has_coverage:
            header_line1 += LOG_MESSAGES["info_header_breadth"]
            header_line2 += LOG_MESSAGES["info_header_coverage"]

        output_lines.append(
            f"{LOG_MESSAGES['info_rendering_name']}\n{header_line1}\n{header_line2}"
        )

        def fmt_num(val: str | int | float) -> str:
            val = int(float(val))
            if val == 0:
                return "0 K"
            if val >= 1_000_000:
                return f"{val / 1_000_000:.0f} M"
            return f"{val / 1_000:.0f} K" if val >= 1_000 else str(val)

        for r in reader:
            row = f"{r['Seq Name']:<7} {fmt_num(r['Model Len']):>8} {fmt_num(r['Model N Len']):>8} {fmt_num(r['# Segs Map']):>9} {float(r['Map Gbases']):>8.2f} {float(r['Map ARD']):>6.0f} x"
            if has_coverage:
                row += f" {r['Breadth Coverage']:>8}"
            output_lines.append(row)
        output_lines.append("\n" + "-" * 60 + "\n")

    m = data.get("metrics", {})
    if detailed:
        output_lines.append(
            f"{data.get('filename', 'Unknown')}\n{'':<28}{'MAPPED':<15}{'RAW'}"
        )
        output_lines.append(
            f"{LOG_MESSAGES['info_avg_read_depth']:<28}{m.get('ard_mapped', 0):.0f} x{'':<11}{m.get('ard_raw', 0):.0f} x"
        )
        output_lines.append(
            f"{LOG_MESSAGES['info_avg_read_depth_wes']:<28}{m.get('ard_wes_mapped', '')}{'':<11}{m.get('ard_wes_raw', '')}"
        )
        output_lines.append(
            f"{LOG_MESSAGES['info_gigabases']:<28}{m.get('gbases_mapped', 0):.2f}{'':<11}{m.get('gbases_raw', 0):.2f}"
        )
        output_lines.append(
            f"{LOG_MESSAGES['info_read_segs']:<28}{m.get('reads_mapped_m', 0):.0f} M{'':<11}{m.get('reads_raw_m', 0):.0f} M"
        )
        output_lines.append(
            f"{LOG_MESSAGES['info_reads']:<28}{m.get('reads_mapped_pct', 0):.0f} %{'':<12}{m.get('reads_raw_pct', 100):.0f} %\n"
        )

    output_lines.append(
        f"{LOG_MESSAGES['info_ref_genome']:<28}{data.get('ref_model_str', 'Unknown')}"
    )
    if data.get("md5_signature"):
        output_lines.append(f"{'MD5 Signature:':<28}{data['md5_signature']}")
    if data.get("refined_ns"):
        output_lines.append(
            f"{LOG_MESSAGES['info_refined_ns']:<28}{LOG_MESSAGES['info_refined_ns_active']}"
        )

    if data.get("avg_read_len", 0) > 0:
        output_lines.append(
            f"{LOG_MESSAGES['info_avg_read_len']:<28}{data['avg_read_len']:.0f} bp (SD={data.get('std_read_len', 0):.0f} bp), {'Paired-end' if data.get('is_paired') else 'Single-end'}"
        )
        output_lines.append(
            f"{LOG_MESSAGES['info_avg_insert_size']:<28}{data.get('avg_insert_size', 0):.0f} bp (SD={data.get('std_insert_size', 0):.0f} bp)"
        )
    else:
        output_lines.append(
            f"{LOG_MESSAGES['info_avg_read_len']:<28}{LOG_MESSAGES['info_could_not_compute']}"
        )

    if detailed:
        output_lines.append(
            f"{LOG_MESSAGES['info_file_content']:<28}{data.get('file_content', 'Unknown')}"
        )

    if data.get("gender") and data.get("gender") != "Unknown":
        output_lines.append(
            f"{LOG_MESSAGES['info_bio_gender']:<28}{data.get('gender')}"
        )
    if data.get("sequencer"):
        if data["sequencer"] != "Unknown":
            output_lines.append(
                f"{LOG_MESSAGES['info_sequencer']:<28}{data.get('sequencer')}"
            )
        elif detailed and data.get("first_qname"):
            output_lines.append(
                f"{LOG_MESSAGES['info_sequencer']:<28}Unknown: {data['first_qname']}"
            )

    output_lines.extend(_health_lines(data.get("alignment_health") or {}))

    fstats = data.get("file_stats", {})
    output_lines.append(
        f"{LOG_MESSAGES['info_file_stats']:<28}{'Sorted' if fstats.get('sorted') else 'Unsorted'}, {'Indexed' if fstats.get('indexed') else 'Unindexed'}, {fstats.get('size_gb', 0):.1f} GBs"
    )
    if fstats.get("version"):
        output_lines.append(f"{'File Format':<28}{fstats['version']}")

    if detailed:
        glossary = [
            "\n" + "=" * 60,
            "GLOSSARY",
            "=" * 60,
            "TABLE COLUMNS:",
            "  Seq Name        - Reference sequence or chromosome name.",
            "  Model Len       - Total length of the reference sequence.",
            "  Model 'N' Len   - Number of 'N' bases (gaps) in the reference.",
            "  # Segs Map      - Count of read segments mapped to this sequence.",
            "  Map Gbases      - Billions of bases mapped to this sequence.",
            "  Map ARD         - Average Read Depth (Gbases / Effective Length).",
            "  Breadth Coverage- % of sequence covered by at least one read.",
            "",
            "METRICS SUMMARY:",
            "  MAPPED          - Statistics for reads aligned to the reference.",
            "  RAW             - Statistics for all reads (aligned + unaligned).",
            "  Avg Read Depth  - Mean depth across all positions.",
            "  ARD (WES)       - Estimated depth if limited to exome regions.",
            "  Gigabases       - Total data volume in billions of base pairs.",
            "  Read Segs       - Total number of reads/segments in millions.",
            "  Reads %         - Percentage of total reads successfully mapped.",
            "",
            "REFERENCE GENOME:",
            "  Build Name      - e.g., hs38DH, hg19. (Chr)=Primary only, (Full)=All.",
            "  Mito Type       - rCRS (Modern) or Yoruba (Legacy) mtDNA reference.",
            "  SNs             - Total count of named sequences in the reference.",
            "",
            "OTHER:",
            "  Bio Gender      - Predicted biological sex based on X/Y ratio.",
            "  Avg Read Length - Mean base pairs per read (SD = standard deviation).",
            "  Avg Insert Size - Mean distance between pairs (inner + reads).",
            "  File Content    - Auto (Autosomes), X/Y, Mito, Other (Alts), Unmap.",
            "  Sequencer       - Identified sequencing platform based on read IDs.",
            "  File Stats      - Binary status (Sorted/Indexed) and file size.",
            "=" * 60,
        ]
        output_lines.extend(glossary)

    return "\n".join(output_lines) + "\n"


SAMPLE_WINDOW_BP = 1000
SAMPLE_WINDOW_BUDGET = 1000
SAMPLE_MIN_WINDOWS_PER_CONTIG = 10


def region_file_tag(region: str | None) -> str:
    """Filename-safe tag that keys per-region coverage outputs."""
    if not region:
        return ""
    return "_" + (re.sub(r"[^A-Za-z0-9.]+", "_", region).strip("_") or "region")


def _cram_reference_opts(input_p: str, ref_p: str | None) -> list[str]:
    if input_p.lower().endswith(".cram") and ref_p and Path(ref_p).is_file():
        return ["--reference", ref_p]
    return []


def _is_fresh_output(path: str, input_p: str) -> bool:
    """True when ``path`` is a non-empty file written after ``input_p`` last changed."""
    try:
        return (
            Path(path).stat().st_size > 0
            and Path(path).stat().st_mtime >= Path(input_p).stat().st_mtime
        )
    except OSError:
        return False


def _breadth_map(
    covered: Mapping[str, tuple[int, int]], with_total: bool, approx: bool = False
) -> dict[str, str]:
    """Format per-contig (covered, total) base counts as breadth percentages."""
    prefix = "~" if approx else ""
    result = {
        contig_key(name): f"{prefix}{cov / total * 100:.0f} %"
        for name, (cov, total) in covered.items()
        if total > 0
    }
    if with_total:
        primary = [v for k, v in covered.items() if is_primary_contig(k)]
        total_bp = sum(total for _cov, total in primary)
        if total_bp:
            covered_bp = sum(cov for cov, _total in primary)
            result["TOTAL_EST"] = f"{prefix}{covered_bp / total_bp * 100:.0f} %"
    return result


def _parse_samtools_coverage(path: str) -> dict[str, tuple[int, int]]:
    covered: dict[str, tuple[int, int]] = {}
    with open(path) as f:
        for line in f:
            if line.startswith("#"):
                continue
            p = line.rstrip("\n").split("\t")
            if len(p) < 5:
                continue
            start, end, covbases = int(p[1]), int(p[2]), int(p[4])
            covered[p[0]] = (covbases, end - start + 1)
    return covered


def _parse_legacy_bincvg(path: str) -> dict[str, tuple[int, int]]:
    """Parse the older ``samtools depth | awk`` ``_bincvg.csv`` summary."""
    covered: dict[str, tuple[int, int]] = {}
    with open(path) as f:
        next(f, None)
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) > 7:
                covered[p[0]] = (int(p[2]), int(p[7]))
    return covered


def run_full_coverage(
    input_p: str,
    ref_p: str | None,
    out_p: str,
    region: str | None = None,
    legacy_p: str | None = None,
) -> dict[str, str]:
    """Exact per-contig breadth of coverage via ``samtools coverage``.

    A previous result is reused only when it is newer than the input. For
    whole-file runs an up-to-date legacy ``_bincvg.csv`` is also accepted.
    """
    with_total = not region
    candidates = [(out_p, _parse_samtools_coverage)]
    if legacy_p:
        candidates.append((legacy_p, _parse_legacy_bincvg))
    for cached, parser in candidates:
        if _is_fresh_output(cached, input_p):
            try:
                covered = parser(cached)
            except (OSError, ValueError, IndexError) as exc:
                logging.warning(f"Ignoring unreadable coverage file {cached}: {exc}")
                continue
            if covered:
                logging.info(f"Using existing coverage results from {cached}")
                return _breadth_map(covered, with_total)

    logging.info(f"Calculating breadth of coverage... saving to {out_p}")
    tmp_p = out_p + ".tmp"
    cmd = ["samtools", "coverage", *_cram_reference_opts(input_p, ref_p)]
    if region:
        cmd += ["-r", region]
    try:
        run_command(cmd + ["-o", tmp_p, input_p])
        Path(tmp_p).replace(out_p)
    except (OSError, subprocess.SubprocessError) as exc:
        raise WGSExtractError(f"samtools coverage failed for {input_p}: {exc}") from exc
    finally:
        if Path(tmp_p).exists():
            Path(tmp_p).unlink()
    return _breadth_map(_parse_samtools_coverage(out_p), with_total)


def _resolve_sample_targets(
    idx_stats: Sequence[IdxStat], region: str | None
) -> list[tuple[str, int, int]]:
    """Return 1-based inclusive (contig, start, end) spans to sample."""
    lengths = {s["name"]: s["length"] for s in idx_stats if s["name"] != "*"}
    if not region:
        return [
            (name, 1, length)
            for name, length in lengths.items()
            if length > 100_000 and is_primary_contig(name)
        ]

    contig, start, end = region, 1, None
    if region not in lengths and ":" in region:
        contig, _sep, span = region.rpartition(":")
        raw_start, _dash, raw_end = span.replace(",", "").partition("-")
        try:
            start = int(raw_start)
            end = int(raw_end) if raw_end else None
        except ValueError as exc:
            raise WGSExtractError(f"Invalid region: {region}") from exc
    name = (
        contig
        if contig in lengths
        else next((a for a in chromosome_aliases(contig) if a in lengths), None)
    )
    if name is None:
        raise WGSExtractError(f"Region contig {contig!r} is not in the input header.")
    end = min(end or lengths[name], lengths[name])
    if start < 1 or start > end:
        raise WGSExtractError(f"Invalid region: {region}")
    return [(name, start, end)]


def _sample_windows(
    targets: Sequence[tuple[str, int, int]],
) -> list[tuple[str, int, int]]:
    """Evenly spaced, non-overlapping windows (0-based half-open, BED style).

    Windows are allocated in proportion to span length so the genome-wide
    estimate is not dominated by short contigs.
    """
    total_len = sum(end - start + 1 for _name, start, end in targets)
    windows = []
    for name, start, end in targets:
        span = end - start + 1
        width = min(SAMPLE_WINDOW_BP, span)
        count = max(
            SAMPLE_MIN_WINDOWS_PER_CONTIG,
            round(SAMPLE_WINDOW_BUDGET * span / total_len),
        )
        count = min(count, span // width)
        spacing = span / count
        for i in range(count):
            win_start = start - 1 + int(i * spacing + (spacing - width) / 2)
            windows.append((name, win_start, win_start + width))
    return windows


def run_sampled_coverage(
    input_p: str,
    ref_p: str | None,
    idx_stats: Sequence[IdxStat],
    out_p: str,
    region: str | None = None,
) -> dict[str, str]:
    """Estimate breadth of coverage from evenly spaced 1 kb windows."""
    targets = _resolve_sample_targets(idx_stats, region)
    if not targets:
        raise WGSExtractError(
            "No primary chromosomes found to sample; use --region to choose one."
        )
    windows = _sample_windows(targets)
    logging.info(
        f"Estimating breadth of coverage from {len(windows)} sampled windows..."
    )

    sampled = {name: 0 for name, _s, _e in targets}
    for name, start, end in windows:
        sampled[name] += end - start
    covered_bp = dict.fromkeys(sampled, 0)
    with tempfile.TemporaryDirectory(dir=str(absolute_path(out_p).parent)) as tmp:
        bed_p = str(Path(tmp, "windows.bed"))
        depth_p = str(Path(tmp, "depth.tsv"))
        with open(bed_p, "w", newline="\n") as f:
            f.writelines(f"{n}\t{s}\t{e}\n" for n, s, e in windows)
        # view -M uses the index to fetch only reads overlapping the windows
        # (depth -b alone scans the whole file). depth then reports covered
        # positions inside the windows; -a/-aa would walk every genome position.
        with open(depth_p, "wb") as depth_out:
            run_pipeline(
                [
                    [
                        "samtools",
                        "view",
                        "-u",
                        "-M",
                        "-L",
                        bed_p,
                        *_cram_reference_opts(input_p, ref_p),
                        input_p,
                    ],
                    ["samtools", "depth", "-b", bed_p, "-"],
                ],
                stdout=depth_out,
                description="Sampled coverage",
            )
        with open(depth_p) as f:
            for line in f:
                parts = line.split("\t")
                if len(parts) >= 3 and parts[0] in covered_bp and int(parts[2]) > 0:
                    covered_bp[parts[0]] += 1

    # Scale each contig's sampled fraction to its full span so the total is
    # length-weighted rather than window-weighted.
    spans = {name: end - start + 1 for name, start, end in targets}
    covered = {
        name: (round(covered_bp[name] / sampled[name] * spans[name]), spans[name])
        for name in sampled
    }
    final_map = _breadth_map(covered, with_total=not region, approx=True)
    with open(out_p, "w", newline="\n") as f:
        json.dump(final_map, f)
    return final_map
