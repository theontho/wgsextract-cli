"""Warnings for inputs too shallow, or too heavily imputed, for reliable genotypes.

Depth comes from what is already known or cheap: `info`'s cached chromosome
table, CRAM container headers (no decoding) or a BAM index. Genotype rates come
from the files just written. Both only log; outputs are never changed.
"""

import argparse
import csv
import gzip
import io
import logging
import re
import tempfile
from collections.abc import Collection, Iterable, Mapping
from pathlib import Path

from wgsextract_cli.core import sample_workspace
from wgsextract_cli.core.config import KNOWN_SETTINGS, settings
from wgsextract_cli.core.cram_counts import cram_container_totals, multi_ref_contigs
from wgsextract_cli.core.microarray_utils import GenotypeTally, is_autosome
from wgsextract_cli.core.paths import PathInput
from wgsextract_cli.core.results import warn
from wgsextract_cli.core.samtools_commands import alignment_index
from wgsextract_cli.core.temp_directory import configured_temp_directory
from wgsextract_cli.core.utils import WGSExtractError, run_command

AUTOSOMES, CHR_X, CHR_Y, MTDNA = "autosomes", "chrX", "chrY", "mtDNA"
TARGETS = (AUTOSOMES, CHR_X, CHR_Y, MTDNA)
# Fewer called sites than this say too little about a sample's genotype rates.
MIN_QC_SITES = 1000
GUIDE = "docs/genotype-qc.md"


def _setting(key: str, override: float | None = None) -> float:
    """A threshold; config loading has already checked its range."""
    if override is not None:
        return override
    value = settings.get(key)
    if not isinstance(value, int | float):
        value = KNOWN_SETTINGS[key][0]
    return float(value) if isinstance(value, int | float) else 0.0


DEPTH_HELP = (
    "Warn below this mean depth for this run; 0 disables (default: config "
    "qc_min_diploid_depth 15, or qc_min_haploid_depth 3 for chrY/mtDNA)"
)


def _non_negative(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        value = -1.0
    if not value >= 0:
        raise argparse.ArgumentTypeError(f"must be a non-negative number: {text!r}")
    return value


def add_depth_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--qc-min-depth",
        type=_non_negative,
        default=None,
        metavar="DEPTH",
        help=DEPTH_HELP,
    )


def contig_target(contig: str) -> str | None:
    key = contig.upper().removeprefix("CHR")
    if key.isdigit():
        return AUTOSOMES if is_autosome(key) else None
    return {"X": CHR_X, "Y": CHR_Y, "M": MTDNA, "MT": MTDNA}.get(key)


# Regions are separated by whitespace or a comma, but not by a thousands comma.
_REGION_SEPARATOR = re.compile(r"\s+|,(?!\d{3}(?:[-,\s]|$))")


def region_parts(region: str) -> list[str]:
    """The regions of a `-r` selection: `chr20:1,500,000-1,600,000 chrX` or
    `chr20,chrY` (contig[:start[-end]] each)."""
    return [p for p in _REGION_SEPARATOR.split(region.strip()) if p]


def region_targets(region: str | None) -> set[str]:
    """The targets a `-r` region (contig[:start-end][,...]) covers; autosomes
    for a whole-genome run."""
    if not region:
        return {AUTOSOMES}
    contigs = (part.rpartition(":")[0] or part for part in region_parts(region))
    return {target for contig in contigs if (target := contig_target(contig))}


def _aggregate(
    rows: Iterable[tuple[str, int, int, float]], unknown: Collection[str] = ()
) -> dict[str, float]:
    """Mean depth per target from (contig, length, N count, bases) rows of the
    contigs with reads; a contig without reads doesn't dilute its target."""
    bases = dict.fromkeys(TARGETS, 0.0)
    length = dict.fromkeys(TARGETS, 0)
    for contig, contig_length, n_count, contig_bases in rows:
        target = contig_target(contig)
        if target and contig_bases > 0:
            bases[target] += contig_bases
            length[target] += max(contig_length - n_count, 1)
    return {
        target: bases[target] / length[target] if length[target] else 0.0
        for target in TARGETS
        if target not in unknown
    }


def _cached_depths(alignment: str, outdir: str | None) -> dict[str, float] | None:
    from wgsextract_cli.commands.info.metrics import SEX_INFERENCE_VERSION, cached_info

    info = cached_info(alignment, outdir, "chrom_table_csv")
    # The table hides chrY for a Female, so one from an older infer_sex is
    # stale, as is one from before long reads' depth was measured.
    if (
        not info
        or not isinstance(bases := info.get("contig_bases"), dict)
        or info.get("sex_inference_version") != SEX_INFERENCE_VERSION
    ):
        return None
    try:
        table = csv.DictReader(io.StringIO(info["chrom_table_csv"]))
        return _aggregate(
            (
                row["Seq Name"],
                int(row["Model Len"]),
                int(row["Model N Len"]),
                float(bases[row["Seq Name"]]),
            )
            for row in table
        )
    except (KeyError, TypeError, ValueError):
        return None


def _counted_depths(
    alignment: str, outdir: str | None, ref: str | None
) -> tuple[dict[str, float], str] | None:
    """Depths from CRAM container base totals or BAM index read counts, with
    `info`'s N adjustment."""
    from wgsextract_cli.commands.info.metrics import (
        LONG_READ_MIN_LEN,
        IdxStat,
        generate_chrom_table,
        load_n_counts,
        parse_idxstats,
    )
    from wgsextract_cli.commands.info.render import sampled_mapped_bases
    from wgsextract_cli.core.alignment_metadata import get_bam_header
    from wgsextract_cli.core.builds import build_from_chr1_length
    from wgsextract_cli.core.reference_models import sequence_lengths

    unknown: set[str] = set()
    if alignment.lower().endswith(".cram"):
        offsets: set[int] = set()
        totals = cram_container_totals(alignment, offsets)
        if totals is None:
            return None
        lengths = sequence_lengths(get_bam_header(alignment, ref))
        if not lengths or max(totals) >= len(lengths):
            return None
        names = list(lengths)
        # Every contig is kept so that info's N adjustment recognises the build.
        stats = [
            IdxStat(
                name=name, length=length, mapped=totals.get(i, (0, 0))[1], unmapped=0
            )
            for i, (name, length) in enumerate(lengths.items())
        ]
        # Bases stand in for reads, so each counts once.
        avg_len, source = 1.0, "CRAM container headers"
        records = sum(n for n, _bases in totals.values())
        read_len = sum(b for _n, b in totals.values()) / records if records else 0.0
        if offsets:
            # Only their slices, listed in the index, say which contigs a
            # multi-reference container's reads are on.
            index = alignment_index(alignment)
            shared = (
                multi_ref_contigs(index, offsets)
                if index and index.endswith(".crai")
                else None
            )
            unknown = (
                set(TARGETS)
                if shared is None
                else {
                    t
                    for i in shared
                    if i < len(names) and (t := contig_target(names[i]))
                }
            )
    elif alignment.lower().endswith(".bam") and alignment_index(alignment):
        stats, *_ = parse_idxstats(alignment)
        lengths = {s["name"]: s["length"] for s in stats if s["name"] != "*"}
        avg_len = read_len = read_length(alignment, outdir)
        source = "the BAM index"
    else:
        return None
    if not avg_len:
        return None
    measured = None
    if read_len > LONG_READ_MIN_LEN:
        # Records x read length overstates long reads' depth (#427), so it is
        # sampled instead, on every contig a target is made of.
        stats = [
            IdxStat(**{**s, "mapped": int(bool(contig_target(s["name"])))})
            for s in stats
        ]
        try:
            measured = sampled_mapped_bases(
                alignment, ref, stats, configured_temp_directory() or outdir
            )
        except (OSError, WGSExtractError) as e:
            logging.debug(f"Could not sample the depth of {alignment}: {e}")
            return None
        unknown, source = set(), "sampled read depth"
    table = generate_chrom_table(
        stats,
        avg_len,
        "Unknown",
        build_from_chr1_length(lengths) or "",
        n_counts=load_n_counts(ref),
        mapped_bases=measured,
    )
    rows = ((row[1], row[2], row[3], row[8]) for row in table)
    return _aggregate(rows, unknown), source


def read_length(alignment: str, outdir: str | None = None) -> float:
    """Mean read length: recorded in the sample workspace, else from `info`'s
    cache, else from sampled reads (then recorded). 0.0 when unknown."""
    from wgsextract_cli.commands.info.metrics import cached_info, run_body_sample

    fact = sample_workspace.known("read_length", alignment)
    if fact is not None:
        return float(fact.value)
    cached = cached_info(alignment, outdir, "avg_read_len")
    avg_len = float(cached["avg_read_len"]) if cached else 0.0
    if not avg_len:
        avg_len = float(run_body_sample(alignment, [])[1])
    if avg_len:
        sample_workspace.record(
            "read_length",
            alignment,
            round(avg_len, 1),
            method="mean length of sampled reads",
            confidence="high",
        )
    return avg_len


def target_depths(
    alignment: str, outdir: str | None = None, ref: str | None = None
) -> tuple[dict[str, float], str] | None:
    """(mean mapped depth per target, its source), or None when that isn't
    cheaply known. Missing targets are unknown; duplicates are counted, as in
    `info`. Only a map of every target is recorded and reused: a partial one
    (e.g. from a CRAM without its index) is estimated again next time."""
    fact = sample_workspace.known("target_depths", alignment)
    value = fact.value if fact is not None else None
    if fact is not None and isinstance(value, dict) and set(TARGETS) <= value.keys():
        origin = "your override" if fact.source == "override" else fact.method
        return {k: float(v) for k, v in value.items()}, (
            f"{sample_workspace.WORKSPACE_FILE} ({origin})"
        )
    try:
        found = _cached_depths(alignment, outdir)
        if found is not None:
            depths, source = found, "cached `info --detailed` metrics"
        else:
            counted = _counted_depths(alignment, outdir, ref)
            if counted is None:
                return None
            depths, source = counted
    except (OSError, ValueError, WGSExtractError) as e:
        logging.debug(f"Could not estimate the depth of {alignment}: {e}")
        return None
    if set(TARGETS) <= set(depths):
        sample_workspace.record(
            "target_depths",
            alignment,
            {k: round(v, 3) for k, v in depths.items()},
            method=source,
            confidence="high",
        )
    return depths, source


def _format_depth(depth: float) -> str:
    return f"{depth:,.0f}x" if depth >= 100 else f"{depth:.1f}x"


def _depths_to_judge(
    alignment: str,
    targets: list[str],
    outdir: str | None,
    ref: str | None,
    min_depth: float | None,
    region: str | None,
) -> tuple[dict[str, float], str, bool | None] | None:
    """Depths over `region`; else the whole-input estimate, with any target it
    finds low measured again over the bases its reads cover. Also whether the
    whole input looks male, which a region's chrX and chrY can't say."""
    found = target_depths(alignment, outdir, ref)
    depths, source = (dict(found[0]), found[1]) if found else ({}, "")
    male = _is_male(depths) if CHR_X in depths and CHR_Y in depths else None
    if region:
        judged, where = targets, f"in {region}"
    else:
        judged = [t for t, *_ in low_depth_targets(depths, targets, min_depth)]
        where = "with reads"
    try:
        measured = (
            covered_depths(alignment, judged, region=region, ref=ref) if judged else {}
        )
    except (OSError, ValueError, WGSExtractError) as e:
        logging.debug(f"Could not measure the depth of {alignment}: {e}")
        measured = {}
    if measured:
        depths.update(measured)
        sampled = f"{', '.join(measured)} from sampled windows {where}"
        source = f"{source}; {sampled}" if source else sampled
    return (depths, source, male) if depths else None


def _is_male(depths: Mapping[str, float]) -> bool:
    return depths.get(CHR_Y, 0.0) > 0.25 * depths.get(CHR_X, 0.0) > 0


def low_depth_targets(
    depths: Mapping[str, float],
    targets: Iterable[str],
    min_depth: float | None = None,
    male: bool | None = None,
) -> list[tuple[str, float, float, bool]]:
    """(target, depth, threshold, haploid) for each target whose mean depth is
    below what its genotypes need: qc_min_diploid_depth for autosomes and a
    woman's chrX, qc_min_haploid_depth for chrY, mtDNA and a man's chrX;
    `min_depth` overrides both. `male` defaults to what `depths` suggest."""
    diploid = _setting("qc_min_diploid_depth", min_depth)
    haploid = _setting("qc_min_haploid_depth", min_depth)
    if male is None:
        male = _is_male(depths)
    low = []
    for target in (t for t in TARGETS if t in set(targets)):
        depth = depths.get(target)
        is_haploid = target in (CHR_Y, MTDNA) or (target == CHR_X and male)
        threshold = haploid if is_haploid else diploid
        if depth is not None and threshold and depth < threshold:
            low.append((target, depth, threshold, is_haploid))
    return low


Interval = tuple[str, int, int]  # contig, 1-based start, inclusive end
COVERED_WINDOWS = 200


def _region_intervals(region: str, lengths: Mapping[str, int]) -> list[Interval]:
    """The intervals of a `-r` selection on the alignment's contigs, matching
    chr20 to 20 and so on."""
    from wgsextract_cli.core.variant_files import chromosome_aliases

    intervals = []
    for part in region_parts(region):
        name, _, span = part.rpartition(":") if ":" in part else (part, "", "")
        contig = next(
            (a for a in (name, *chromosome_aliases(name)) if a in lengths), None
        )
        if contig is None:
            continue
        start, _, end = span.replace(",", "").replace("_", "").partition("-")
        first = max(int(start), 1) if start.isdigit() else 1
        last = min(int(end), lengths[contig]) if end.isdigit() else lengths[contig]
        if first <= last:
            intervals.append((contig, first, last))
    return intervals


def _contigs_with_reads(alignment: str, ref: str | None) -> dict[str, int]:
    """Length of each contig the index places mapped reads on."""
    from wgsextract_cli.commands.info.metrics import parse_idxstats
    from wgsextract_cli.core.alignment_metadata import get_bam_header
    from wgsextract_cli.core.reference_models import sequence_lengths

    index = alignment_index(alignment)
    if not index:
        return {}
    if not index.endswith(".crai"):
        stats, *_ = parse_idxstats(alignment)
        return {s["name"]: s["length"] for s in stats if s["mapped"] and s["length"]}
    names = list(sequence_lengths(get_bam_header(alignment, ref)).items())
    with gzip.open(index, "rt") as f:
        ids = {int(line.split("\t", 1)[0]) for line in f if line.strip()}
    return dict(names[i] for i in sorted(ids) if 0 <= i < len(names))


def _bedcov(
    alignment: str, intervals: Iterable[Interval], ref: str | None
) -> list[tuple[Interval, int, int]]:
    """(interval, summed depth, covered bases) from `samtools bedcov`: aligned
    bases of primary and supplementary records, without duplicates."""
    with tempfile.TemporaryDirectory(dir=configured_temp_directory()) as tmp:
        bed = Path(tmp, "intervals.bed")
        bed.write_text(
            "".join(f"{c}\t{s - 1}\t{e}\n" for c, s, e in intervals), newline="\n"
        )
        reference = ["--reference", ref] if ref else []
        out = run_command(
            ["samtools", "bedcov", "-j", "-d", "1", *reference, bed, alignment],
            capture_output=True,
        ).stdout
    rows = []
    for line in out.splitlines():
        contig, start, end, depth, covered = line.split("\t")[:5]
        rows.append(((contig, int(start) + 1, int(end)), int(depth), int(covered)))
    return rows


def covered_depths(
    alignment: str,
    targets: Iterable[str],
    *,
    region: str | None = None,
    ref: str | None = None,
) -> dict[str, float]:
    """Mean depth per target: over `region`, or else over the bases its reads
    cover on the contigs holding them, which is what calling sees in a slice,
    an `extract` output or a panel (#430).

    About 200 evenly spaced 1 kb windows are read, spaced widely enough that
    few long reads are decoded twice. Without a region, only bases with reads
    count, and when no window has any, every read on the target's contigs is
    counted. A target with no reads there has depth 0."""
    from wgsextract_cli.commands.info.render import _sample_windows
    from wgsextract_cli.core.alignment_metadata import get_bam_header
    from wgsextract_cli.core.reference_models import sequence_lengths

    wanted = set(targets)
    if alignment.lower().endswith(".cram") and not (ref and Path(ref).is_file()):
        return {}
    if region:
        lengths = sequence_lengths(get_bam_header(alignment, ref))
        intervals = _region_intervals(region, lengths)
    else:
        intervals = [(c, 1, n) for c, n in _contigs_with_reads(alignment, ref).items()]
    intervals = [i for i in intervals if contig_target(i[0]) in wanted]
    if not intervals:
        return {}
    windows = [(c, s + 1, e) for c, s, e in _sample_windows(intervals, COVERED_WINDOWS)]
    rows = _bedcov(alignment, windows, ref)
    if not region:
        found = {contig_target(c) for (c, *_), _d, covered in rows if covered}
        rows += _bedcov(
            alignment, [i for i in intervals if contig_target(i[0]) not in found], ref
        )
    depth = dict.fromkeys(wanted, 0)
    span = dict.fromkeys(wanted, 0)
    for (contig, start, end), summed, covered in rows:
        target = contig_target(contig)
        if target in wanted:
            depth[target] += summed
            span[target] += end - start + 1 if region else covered
    return {t: depth[t] / span[t] if span[t] else 0.0 for t in wanted}


def warn_low_depth(
    alignment: str,
    targets: Iterable[str],
    *,
    outdir: str | None = None,
    ref: str | None = None,
    min_depth: float | None = None,
    region: str | None = None,
) -> dict[str, float] | None:
    """Warn about each of `targets` that low_depth_targets finds too shallow:
    over `region` when given, else over the whole input, re-checked over the
    bases its reads cover before warning."""
    targets = [t for t in TARGETS if t in set(targets)]
    if not targets or not alignment.lower().endswith((".bam", ".cram")):
        return None
    if not _setting("qc_min_diploid_depth", min_depth) and not _setting(
        "qc_min_haploid_depth", min_depth
    ):
        return None
    found = _depths_to_judge(alignment, targets, outdir, ref, min_depth, region)
    if found is None or not any(t in found[0] for t in targets):
        logging.info(
            f"Mean depth of {Path(alignment).name} isn't known without reading it; "
            "`wgsextract info --detailed` caches it for the low-coverage check."
        )
        return None
    depths, source, male = found
    summary = ", ".join(f"{t} {_format_depth(d)}" for t, d in depths.items())
    logging.info(f"Mean depth from {source}: {summary}")
    for target, depth, threshold, is_haploid in low_depth_targets(
        depths, targets, min_depth, male
    ):
        setting = "qc_min_haploid_depth" if is_haploid else "qc_min_diploid_depth"
        consequence = (
            "haplogroups may stop at an upstream branch or be wrong"
            if is_haploid
            else "heterozygous sites are often called homozygous or missed, so "
            "genotypes are unreliable"
        )
        warn(
            "low_coverage",
            f"Low coverage: {Path(alignment).name} has a mean {target} depth of "
            f"about {_format_depth(depth)}, below the {threshold:g}x that "
            f"{'haploid' if is_haploid else 'diploid'} calls need; {consequence}. "
            f"Threshold: {setting} in config.toml, or --qc-min-depth for one run. "
            f"See {GUIDE}.",
            target=target,
            depth=round(depth, 3),
            threshold=threshold,
        )
    return depths


def kit_tally(combined_kit: PathInput) -> GenotypeTally:
    """Autosomal genotypes of a CombinedKit (RSID, CHROM, POS, RESULT)."""
    tally = GenotypeTally()
    with open(combined_kit) as f:
        for line in f:
            if line.startswith("#"):
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 4 and is_autosome(parts[1]):
                tally.add(parts[3])
    return tally


def warn_about_genotypes(
    combined_kit: PathInput,
    outputs: Mapping[str, GenotypeTally] | None = None,
    *,
    check_no_calls: bool = True,
) -> GenotypeTally:
    """Warn when the kit's autosomal homozygosity, or an output's no-call
    rate, is outside what a directly sequenced genome gives."""
    try:
        tally = kit_tally(combined_kit)
    except (OSError, UnicodeDecodeError) as e:
        logging.debug(f"Could not check the genotypes in {combined_kit}: {e}")
        return GenotypeTally()
    if tally.called:
        hom = tally.called - tally.het
        ratio = f"{tally.het / hom:.2f}" if hom else "n/a"
        logging.info(
            f"Autosomal genotypes: {tally.called:,} called, "
            f"{tally.homozygosity:.1%} homozygous, het/hom {ratio}"
        )
    low = _setting("qc_min_autosomal_homozygosity")
    high = _setting("qc_max_autosomal_homozygosity")
    if tally.called >= MIN_QC_SITES:
        if tally.homozygosity > high:
            warn(
                "high_homozygosity",
                f"{tally.homozygosity:.1%} of autosomal genotypes are homozygous "
                f"(above qc_max_autosomal_homozygosity {high:.0%}; directly "
                "sequenced genomes give about 70-80%). The input is probably "
                "low-coverage or heavily imputed, and many tools reject such "
                f"files. See {GUIDE}.",
                homozygosity=round(tally.homozygosity, 4),
                threshold=high,
            )
        elif tally.homozygosity < low:
            warn(
                "low_homozygosity",
                f"Only {tally.homozygosity:.1%} of autosomal genotypes are "
                f"homozygous (below qc_min_autosomal_homozygosity {low:.0%}; "
                "directly sequenced genomes give about 70-80%). Excess "
                "heterozygosity suggests a contaminated or mixed sample. "
                f"See {GUIDE}.",
                homozygosity=round(tally.homozygosity, 4),
                threshold=low,
            )
    max_no_calls = _setting("qc_max_no_call_rate")
    noisy = {
        name: t.no_call_rate
        for name, t in (outputs or {}).items()
        if check_no_calls and t.sites >= MIN_QC_SITES and t.no_call_rate > max_no_calls
    }
    if noisy:
        rates = ", ".join(f"{name} {rate:.1%}" for name, rate in noisy.items())
        warn(
            "high_no_call_rate",
            f"High autosomal no-call rates (above qc_max_no_call_rate "
            f"{max_no_calls:.0%}): {rates}. The input lacks reads or calls at "
            f"many chip positions. See {GUIDE}.",
            threshold=max_no_calls,
            **{f"{name}_no_call_rate": round(rate, 4) for name, rate in noisy.items()},
        )
    return tally
