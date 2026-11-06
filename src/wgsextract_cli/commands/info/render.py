import csv
import io
import json
import logging
import math
import re
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.paths import absolute_path
from wgsextract_cli.core.results import current_result
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


def _read_length_lines(lengths: Mapping[str, Any] | None, detailed: bool) -> list[str]:
    """Long reads' N50, mapped fraction and (detailed) length histogram."""
    if not lengths:
        return []
    lines = [f"{'Read N50':<28}{lengths['read_n50']:,} bp, {lengths['method']}"]
    if lengths.get("mapped_read_pct") is not None:
        lines.append(
            f"{'Mapped Reads':<28}{lengths['mapped_read_pct']:.1f} % of reads, "
            f"{lengths['mapped_read_method']}"
        )
    elif lengths.get("mapped_read_method"):
        lines.append(f"{'Mapped Reads':<28}{lengths['mapped_read_method']}")
    if detailed:
        lines.append(f"{'Read Lengths':<28}{'reads':>8}{'bases':>8}")
        for row in lengths["length_histogram"]:
            low, high = row["min_bp"] / 1000, row["max_bp"]
            label = f"{low:g}-{high / 1000:g} kb" if high else f">= {low:g} kb"
            lines.append(
                f"  {label:<26}{row['reads_pct']:>7.1f}%{row['bases_pct']:>7.1f}%"
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
        output_lines.extend(_read_length_lines(data.get("read_lengths"), detailed))
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
            "  Read N50        - Long reads: half the bases are in reads at least",
            "                    this long (from the first 20,000 records).",
            "  Mapped Reads    - Long reads: % of reads (not records) that map.",
            "  File Content    - Auto (Autosomes), X/Y, Mito, Other (Alts), Unmap.",
            "  Sequencer       - Identified sequencing platform based on read IDs.",
            "  File Stats      - Binary status (Sorted/Indexed) and file size.",
            "=" * 60,
        ]
        output_lines.extend(glossary)

    return "\n".join(output_lines) + "\n"


SAMPLE_WINDOW_BP = 1000
# Wider windows for counting the records that start in them: long reads start
# about once per read length, and decoding costs the same either way.
MAPPING_WINDOW_BP = 10_000
# Mapping windows on one contig are at least this far apart (see
# sampled_primary_share); human chromosomes' are about 3 Mb apart anyway.
MAPPING_WINDOW_GAP_BP = 1_000_000
MAPPING_UNAVAILABLE = "not estimated: no read starts in the sampled windows"
SAMPLE_WINDOW_BUDGET = 1000
SAMPLE_MIN_WINDOWS_PER_CONTIG = 10
# Smaller contigs (unplaced, alt, decoy) share one sample of at least this many
# windows when measuring long reads' depth.
POOLED_CONTIG_BP = 1_000_000
POOLED_MIN_WINDOWS = 50


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
                current_result().add_output(cached, "coverage")
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
    current_result().add_output(out_p, "coverage")
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
    budget: int = SAMPLE_WINDOW_BUDGET,
    window_bp: int = SAMPLE_WINDOW_BP,
) -> list[tuple[str, int, int]]:
    """About `budget` evenly spaced, non-overlapping windows (0-based
    half-open, BED style).

    Windows are allocated in proportion to span length so the genome-wide
    estimate is not dominated by short contigs.
    """
    total_len = sum(end - start + 1 for _name, start, end in targets)
    windows = []
    for name, start, end in targets:
        span = end - start + 1
        width = min(window_bp, span)
        count = max(
            SAMPLE_MIN_WINDOWS_PER_CONTIG,
            round(budget * span / total_len),
        )
        count = min(count, span // width)
        spacing = span / count
        for i in range(count):
            win_start = start - 1 + int(i * spacing + (spacing - width) / 2)
            windows.append((name, win_start, win_start + width))
    return windows


def _sampled_bp(
    targets: Sequence[tuple[str, int, int]], windows: Sequence[tuple[str, int, int]]
) -> dict[str, int]:
    sampled = {name: 0 for name, _s, _e in targets}
    for name, start, end in windows:
        sampled[name] += end - start
    return sampled


def _window_depths(
    input_p: str,
    ref_p: str | None,
    windows: Sequence[tuple[str, int, int]],
    temp_parent: str | None,
) -> dict[str, tuple[int, int]]:
    """(covered positions, summed depth) per contig inside BED-style windows.

    Depth is samtools depth's: secondary, duplicate, QC-failed and unmapped
    records are skipped, and only aligned bases count (not soft clips)."""
    found: dict[str, tuple[int, int]] = {}
    with tempfile.TemporaryDirectory(dir=temp_parent) as tmp:
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
                if len(parts) >= 3 and (depth := int(parts[2])) > 0:
                    covered, total = found.get(parts[0], (0, 0))
                    found[parts[0]] = (covered + 1, total + depth)
    return found


def _pooled_windows(
    contigs: Sequence[tuple[str, int]], count: int, window_bp: int = SAMPLE_WINDOW_BP
) -> list[tuple[str, int, int]]:
    """About `count` windows evenly spaced along `contigs` laid end to end, so
    thousands of small contigs share one length-weighted sample."""
    total = sum(length for _name, length in contigs)
    spacing = total / count
    windows: list[tuple[str, int, int]] = []
    offset, i = 0, 0
    for name, length in contigs:
        while (pos := int((i + 0.5) * spacing)) < offset + length:
            width = min(window_bp, length)
            start = min(max(pos - offset - width // 2, 0), length - width)
            if windows and windows[-1][0] == name and windows[-1][2] >= start:
                # Merged, so no base is sampled twice.
                windows[-1] = (name, windows[-1][1], start + width)
            else:
                windows.append((name, start, start + width))
            i += 1
        offset += length
    return windows


def _contigs_with_reads(
    idx_stats: Sequence[IdxStat],
) -> tuple[list[tuple[str, int, int]], list[tuple[str, int]]]:
    """Contigs with mapped reads: those sampled on their own (chromosomes and
    contigs of 1 Mb or more), and the smaller ones that share a pooled sample."""
    with_reads = [s for s in idx_stats if s["mapped"] and s["length"] > 0]
    own = [
        (s["name"], 1, s["length"])
        for s in with_reads
        if is_primary_contig(s["name"])
        or contig_key(s["name"]) == "M"
        or s["length"] >= POOLED_CONTIG_BP
    ]
    named = {name for name, _s, _e in own}
    pooled = [(s["name"], s["length"]) for s in with_reads if s["name"] not in named]
    return own, pooled


def _contig_windows(
    own: Sequence[tuple[str, int, int]],
    pooled: Sequence[tuple[str, int]],
    window_bp: int,
) -> list[tuple[str, int, int]]:
    windows = _sample_windows(own, window_bp=window_bp) if own else []
    pool_bp = sum(length for _name, length in pooled)
    if pooled:
        share = SAMPLE_WINDOW_BUDGET * pool_bp / (pool_bp + sum(e for *_, e in own))
        windows += _pooled_windows(
            pooled, max(POOLED_MIN_WINDOWS, round(share)), window_bp
        )
    return windows


def sampled_mapped_bases(
    input_p: str,
    ref_p: str | None,
    idx_stats: Sequence[IdxStat],
    temp_parent: str | None = None,
) -> dict[str, float]:
    """Aligned bases per contig with reads, estimated from the depth of evenly
    spaced 1 kb windows and scaled to the contig's length. Chromosomes and
    other contigs of 1 Mb or more are sampled on their own; the smaller
    unplaced, alt and decoy contigs share a pooled sample, whose mean depth
    each of them gets.

    Long reads need this: a supplementary or secondary record carries the
    whole read (soft-clipped) but aligns only part of it, so records x read
    length overstates their depth, most on repetitive contigs like chrY."""
    own, pooled = _contigs_with_reads(idx_stats)
    if not own and not pooled:
        return {}
    windows = _contig_windows(own, pooled, SAMPLE_WINDOW_BP)
    logging.info(f"Estimating mean depth from {len(windows)} sampled windows...")
    sampled = _sampled_bp([*own, *((n, 1, length) for n, length in pooled)], windows)
    depths = _window_depths(input_p, ref_p, windows, temp_parent)
    found = {
        name: depths.get(name, (0, 0))[1] / sampled[name] * length
        for name, _start, length in own
        if sampled[name]
    }
    pool_sampled = sum(sampled[name] for name, _length in pooled)
    if pool_sampled:
        pool_depth = sum(depths.get(name, (0, 0))[1] for name, _length in pooled)
        mean = pool_depth / pool_sampled
        found.update((name, mean * length) for name, length in pooled)
    return found


# A CRAM need only decode FLAG, RNAME, POS and CIGAR to count records: 4x
# faster on a 30x ONT genome than decoding whole records.
_COUNT_FIELDS = ["--input-fmt-option", "required_fields=0x2E"]


def _flagstat_counts(
    cmds: list[list[str]], names: Sequence[str], tmp: str, description: str
) -> dict[str, int]:
    """``names``' counts from a pipeline ending in ``samtools flagstat -O tsv``,
    QC-passed and QC-failed records together (as idxstats counts them)."""
    stats_p = Path(tmp, "flagstat.tsv")
    with open(stats_p, "wb") as out:
        run_pipeline(cmds, stdout=out, description=description)
    # The TSV also has percentage lines ("100.00%"), hence picking by name.
    return {
        name: int(passed) + int(failed)
        for passed, failed, name in (
            line.split("\t", 2) for line in stats_p.read_text().splitlines()
        )
        if name in names
    }


def _mapped_and_primary(
    input_p: str, ref_p: str | None, bed_p: str, tmp: str
) -> tuple[int, int]:
    """Mapped and primary mapped records overlapping the BED's regions, each
    record counted once however many regions it overlaps."""
    counts = _flagstat_counts(
        [
            ["samtools", "view", "-u", "-M", "-L", bed_p, *_COUNT_FIELDS,
             *_cram_reference_opts(input_p, ref_p), input_p],
            ["samtools", "flagstat", "-O", "tsv", "-"],
        ],
        ("mapped", "primary mapped"),
        tmp,
        "Sampled mapping counts",
    )  # fmt: skip
    return counts["mapped"], counts["primary mapped"]


def _spaced_windows(
    windows: Sequence[tuple[str, int, int]], gap: int
) -> list[tuple[str, int, int]]:
    """``windows`` (in order along each contig) thinned to at least ``gap``
    apart, so no record shorter than that spans from one into the next."""
    kept: list[tuple[str, int, int]] = []
    for window in windows:
        if not kept or kept[-1][0] != window[0] or window[1] - kept[-1][2] >= gap:
            kept.append(window)
    return kept


def _mapping_strata(
    windows: Sequence[tuple[str, int, int]],
    own: Sequence[tuple[str, int, int]],
    pooled: Sequence[tuple[str, int]],
) -> list[tuple[list[tuple[str, int, int]], float]]:
    """The windows grouped by how densely they sample their contig (chrM's
    one window covers most of it, a chromosome's a fraction of a percent),
    each group with the inverse of its sampled fraction. The pooled small
    contigs count as one. A record starting on a window's first base is
    not counted, so that base is not sampled."""
    pool = {name for name, _length in pooled}
    span = {name: length for name, _s, length in own}
    span[""] = sum(length for _name, length in pooled)
    sampled: dict[str, int] = {}
    for name, start, end in windows:
        unit = "" if name in pool else name
        sampled[unit] = sampled.get(unit, 0) + end - start - 1
    buckets: dict[int, list[str]] = {}
    for unit, bp in sampled.items():
        if bp > 0:
            buckets.setdefault(round(math.log(bp / span[unit], 1.25)), []).append(unit)
    strata = []
    for units in buckets.values():
        chosen = set(units)
        strata.append(
            (
                [w for w in windows if ("" if w[0] in pool else w[0]) in chosen],
                sum(span[u] for u in units) / sum(sampled[u] for u in units),
            )
        )
    return strata


def sampled_primary_share(
    input_p: str,
    ref_p: str | None,
    idx_stats: Sequence[IdxStat],
    temp_parent: str | None = None,
) -> tuple[float, int] | None:
    """The share of mapped records that are primary, and how many records it
    is measured from, sampled from evenly spaced 10 kb windows: idxstats
    counts every record, and long reads have many supplementary ones.

    Each record that starts in a window counts once, whatever its length: the
    records overlapping the windows less those overlapping their first bases.
    (Counting overlaps alone would favour long primary records over short
    split ones.) Windows are kept 1 Mb apart, so only a longer alignment can
    start in one and reach the next's first base, and be missed. Like
    idxstats, it counts QC-failed and duplicate records. Windows that sample
    their contig more densely (chrM) are counted apart and weighted less.

    None when no sampled primary record starts in a window: a mapped read
    always has a mapped primary record, so that sample is too sparse."""
    own, pooled = _contigs_with_reads(idx_stats)
    windows = _spaced_windows(
        _contig_windows(own, pooled, MAPPING_WINDOW_BP), MAPPING_WINDOW_GAP_BP
    )
    if not windows:
        return None
    logging.info(f"Estimating mapped reads from {len(windows)} sampled windows...")
    starts = est_mapped = est_primary = 0.0
    primary_starts = 0
    with tempfile.TemporaryDirectory(dir=temp_parent) as tmp:
        overlaps, first_bases = Path(tmp, "windows.bed"), Path(tmp, "first.bed")
        for stratum, weight in _mapping_strata(windows, own, pooled):
            overlaps.write_text(
                "".join(f"{n}\t{s}\t{e}\n" for n, s, e in stratum), newline="\n"
            )
            first_bases.write_text(
                "".join(f"{n}\t{s}\t{s + 1}\n" for n, s, _ in stratum), newline="\n"
            )
            mapped, primary = _mapped_and_primary(input_p, ref_p, str(overlaps), tmp)
            on_first, primary_on_first = _mapped_and_primary(
                input_p, ref_p, str(first_bases), tmp
            )
            starts += mapped - on_first
            primary_starts += primary - primary_on_first
            est_mapped += (mapped - on_first) * weight
            est_primary += (primary - primary_on_first) * weight
    if primary_starts <= 0:
        return None
    return est_primary / est_mapped, int(starts)


def exact_mapped_read_pct(
    input_p: str, ref_p: str | None, temp_parent: str | None = None
) -> float:
    """Percentage of primary records (reads) mapped, counting the whole file,
    QC-failed reads included."""
    with tempfile.TemporaryDirectory(dir=temp_parent) as tmp:
        counts = _flagstat_counts(
            [["samtools", "flagstat", "-O", "tsv", *_COUNT_FIELDS,
              *_cram_reference_opts(input_p, ref_p), input_p]],
            ("primary", "primary mapped"),
            tmp,
            "Mapping counts",
        )  # fmt: skip
    reads = counts["primary"]
    return round(100 * counts["primary mapped"] / reads, 2) if reads else 0.0


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

    sampled = _sampled_bp(targets, windows)
    covered_bp = {
        name: covered
        for name, (covered, _depth) in _window_depths(
            input_p, ref_p, windows, str(absolute_path(out_p).parent)
        ).items()
    }
    # Scale each contig's sampled fraction to its full span so the total is
    # length-weighted rather than window-weighted.
    spans = {name: end - start + 1 for name, start, end in targets}
    covered = {
        name: (
            round(covered_bp.get(name, 0) / sampled[name] * spans[name]),
            spans[name],
        )
        for name in sampled
    }
    final_map = _breadth_map(covered, with_total=not region, approx=True)
    with open(out_p, "w", newline="\n") as f:
        json.dump(final_map, f)
    current_result().add_output(out_p, "coverage")
    return final_map
