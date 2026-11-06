import csv
import json
import logging
import math
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple, TypedDict

from wgsextract_cli.core import sample_workspace
from wgsextract_cli.core.builds import build_from_chr1_length, build_from_path
from wgsextract_cli.core.config import settings
from wgsextract_cli.core.constants import (
    N_ADJUST,
    SEQUENCERS,
)
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.paths import absolute_path
from wgsextract_cli.core.reference_assets import n_count_paths
from wgsextract_cli.core.sample_workspace import Confidence
from wgsextract_cli.core.sequencing import platform_from_metadata
from wgsextract_cli.core.ultima import is_ultima_header
from wgsextract_cli.core.utils import WGSExtractError, popen, run_command
from wgsextract_cli.core.variant_files import calculate_bam_md5

ULTIMA_SEQUENCER = "Ultima Genomics UG 100"


class IdxStat(TypedDict):
    name: str
    length: int
    mapped: int
    unmapped: int


def contig_key(name: str) -> str:
    """Normalize a contig name for table lookups (chr1 -> 1, chrMT/MT -> M)."""
    key = name.upper().removeprefix("CHR")
    return "M" if key == "MT" else key


# Reads longer than this on average are long reads (ONT, PacBio).
LONG_READ_MIN_LEN = 410
DEPTH_COUNTED = "mapped reads x mean read length / effective genome length"
DEPTH_SAMPLED = "depth of sampled 1 kb windows / effective genome length"


def is_primary_contig(name: str) -> bool:
    key = contig_key(name)
    return key.isdigit() or key in {"X", "Y"}


def determine_sequencer(qname: str | None, header: str = "") -> str:
    """Identify sequencer type from QNAME using regex patterns, falling back to
    the read groups' platform (PL:ULTIMA) for names it doesn't know."""
    if platform_from_metadata(header, qname) == "ont":
        return "Oxford Nanopore"
    for name, pattern in SEQUENCERS.items():
        if name != "Unknown" and qname and re.search(pattern, qname):
            # Special logic for Dante MGI flow plate ID from legacy
            if "6xxx" in name:
                return name.replace("6xxx", qname[6:10] + " bad")
            return name
    if header and is_ultima_header(header):
        return ULTIMA_SEQUENCER
    # If unrecognized, return "Unknown" so we can show the full QNAME in detailed info
    return "Unknown"


def get_file_stats(filepath: str) -> tuple[float, bool]:
    """Retrieve file size and check for index existence."""
    size_bytes = Path(filepath).stat().st_size
    size_gb = size_bytes / (1024**3)

    indexed = False
    # Check for direct appended index (e.g. file.bam.bai, file.cram.crai)
    if Path(filepath + ".bai").exists() or Path(filepath + ".crai").exists():
        indexed = True
    # Check for replaced extension index (e.g. file.bai, file.crai)
    elif filepath.lower().endswith(".bam") and Path(filepath[:-4] + ".bai").exists():
        indexed = True
    elif filepath.lower().endswith(".cram") and Path(filepath[:-5] + ".crai").exists():
        indexed = True
    # Check for .csi index (sometimes used for large files)
    elif Path(filepath + ".csi").exists():
        indexed = True

    return size_gb, indexed


class BodySample(NamedTuple):
    reads: int
    avg_len: float
    std_len: float
    avg_tlen: float
    std_tlen: float
    is_paired: bool
    first_qname: str | None
    # Lengths of the sampled primary reads.
    lengths: tuple[int, ...] = ()
    # The sample reached the end of the file, so it holds every read.
    complete: bool = False


BODY_SAMPLE_RECORDS = 20000


def run_body_sample(filepath: str, cram_opt: Sequence[str]) -> BodySample:
    """Sample first 20k reads to calculate length, insert size, and read type."""
    logging.info(LOG_MESSAGES["sampling_metrics"].format(filename=Path(filepath).name))
    cmd = ["samtools", "view", *cram_opt, filepath]

    total_len = count = paired_count = 0
    tlen_values = []
    len_values = []
    first_qname = None
    complete = False

    proc = None
    try:
        proc = popen(
            cmd,
            stdout=subprocess.PIPE,
            text=True,
            stderr=subprocess.DEVNULL,
        )
        assert proc.stdout is not None
        for _ in range(BODY_SAMPLE_RECORDS):
            line = proc.stdout.readline()
            if not line:
                complete = proc.wait() == 0
                break
            fields = line.split("\t")
            if len(fields) < 11:
                continue

            if first_qname is None:
                first_qname = fields[0]

            flag = int(fields[1])
            rnext = fields[6]
            tlen = int(fields[8])
            seq = fields[9]

            # Skip supplementary, secondary, or fail-QC reads (0xB00)
            if flag & 0xB00:
                continue

            if flag & 1:
                paired_count += 1

            seq_len = len(seq)
            if seq_len > 1:
                total_len += seq_len
                len_values.append(seq_len)
                count += 1

            # Only process forward reads with valid next segment on same chrom
            # and limit to 50k to match legacy behavior (avoids chimeric skews)
            if rnext == "=" and 0 < tlen < 50000:
                tlen_values.append(tlen)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        logging.warning("Failed to sample read metrics from %s: %s", filepath, exc)
    finally:
        # We deliberately stop reading early, so samtools is terminated rather
        # than drained; reap it so no zombie or open pipe is left behind.
        if proc is not None:
            if proc.poll() is None:
                proc.terminate()
            if proc.stdout:
                proc.stdout.close()
            proc.wait()

    avg_len = total_len / count if count > 0 else 0
    avg_tlen = sum(tlen_values) / len(tlen_values) if len(tlen_values) > 0 else 0
    std_len = (
        math.sqrt(sum((x - avg_len) ** 2 for x in len_values) / count)
        if count > 1
        else 0
    )
    std_tlen = (
        math.sqrt(sum((x - avg_tlen) ** 2 for x in tlen_values) / len(tlen_values))
        if len(tlen_values) > 1
        else 0
    )
    is_paired = paired_count > (count / 2) if count > 0 else False

    return BodySample(
        count,
        avg_len,
        std_len,
        avg_tlen,
        std_tlen,
        is_paired,
        first_qname,
        tuple(len_values),
        complete,
    )


# Upper bounds of the long-read length histogram's bins, in bp.
READ_LENGTH_BINS = (1_000, 2_000, 5_000, 10_000, 20_000, 50_000, 100_000)
# Library failures show up as short reads (#338). Home buccal-swab ONT runs
# peak near 4 kb, so an N50 under 3 kb is unusually short even for them.
MIN_LONG_READ_N50 = 3_000
MIN_LONG_READ_MAPPED_PCT = 95.0


def n50(lengths: Sequence[int]) -> int:
    """The length L such that reads at least L long hold half the bases."""
    half, running = sum(lengths) / 2, 0
    for length in sorted(lengths, reverse=True):
        running += length
        if running >= half:
            return length
    return 0


def long_read_lengths(sample: BodySample) -> dict[str, Any]:
    """N50 and length histogram of the long reads among the first records of
    the file, which `info` reads anyway: exact when that is every read."""
    lengths = sample.lengths
    total = sum(lengths) or 1
    histogram = []
    low = 0
    for high in (*READ_LENGTH_BINS, None):
        in_bin = [n for n in lengths if n >= low and (high is None or n < high)]
        histogram.append(
            {
                "min_bp": low,
                "max_bp": high,
                "reads": len(in_bin),
                "reads_pct": round(100 * len(in_bin) / (len(lengths) or 1), 2),
                "bases_pct": round(100 * sum(in_bin) / total, 2),
            }
        )
        low = high or 0
    return {
        "read_n50": n50(lengths),
        "sampled_reads": len(lengths),
        "exact": sample.complete,
        "method": f"all {len(lengths):,} primary reads"
        if sample.complete
        else f"estimate from the first {len(lengths):,} primary reads",
        "length_histogram": histogram,
    }


def long_read_mapped_pct(idx_stats: Sequence[IdxStat], primary_share: float) -> float:
    """Percentage of reads (not records) mapped. idxstats counts every mapped
    record, so only the sampled primary share of them are reads."""
    primary = sum(s["mapped"] for s in idx_stats) * primary_share
    unmapped = sum(s["unmapped"] for s in idx_stats)
    return round(100 * primary / (primary + unmapped), 2) if primary else 0.0


def load_n_counts(ref_path: str | None) -> dict[str, int]:
    """Load per-contig N counts from the reference's ``<prefix>_ncnt.csv`` sidecar.

    The sidecar is tab-separated with ``#`` comment/header lines and columns
    SN, NumBP, NumNs, ... (numbers may contain thousands separators). A
    two-column ``SN,NumNs`` CSV is also accepted.
    """
    if not ref_path or not Path(ref_path).is_file():
        return {}

    ncnt_file, _ = n_count_paths(ref_path)
    if not Path(ncnt_file).exists():
        return {}

    logging.debug(LOG_MESSAGES["info_loading_n"].format(file=ncnt_file))
    n_counts: dict[str, int] = {}
    try:
        with open(ncnt_file, encoding="utf-8-sig", newline="") as f:
            for line in f:
                line = line.strip()
                if line.startswith("#TOTALS"):
                    break  # the genome-wide totals row that follows is not a contig
                if not line or line.startswith("#"):
                    continue
                if "\t" in line:
                    row = line.split("\t")
                else:
                    row = next(csv.reader([line]))
                if len(row) < 2:
                    continue
                count = row[2] if len(row) >= 3 else row[1]
                try:
                    n_counts[contig_key(row[0].strip())] = int(
                        float(count.strip().replace(",", ""))
                    )
                except ValueError:
                    continue
    except (OSError, UnicodeDecodeError, csv.Error) as e:
        logging.warning(f"Failed to read reference N counts {ncnt_file}: {e}")
        return {}

    return n_counts


# Stored with a cached "gender" so caches made by an older infer_sex are
# recomputed rather than trusted; bump it in sample_workspace.FACTS.
SEX_INFERENCE_VERSION = sample_workspace.FACTS["sex"].version


class SexCall(NamedTuple):
    sex: str
    method: str
    confidence: Confidence


def infer_sex(idx_stats: list[IdxStat]) -> str:
    """ "Male", "Female" or "Unknown" from the X dosage against a similar-sized
    autosome (chr7), else from chrY reads against chrX or chr19 (~ Y's size).
    A file of chrY (and chrM) alone is a male's: a Y extract or a Big Y test."""
    return infer_sex_call(idx_stats).sex


def infer_sex_call(idx_stats: list[IdxStat]) -> SexCall:
    """`infer_sex` with the rule that decided it and how sure that rule is."""
    mapped: dict[str, int] = {}
    for s in idx_stats:
        if s["name"] != "*":
            key = contig_key(s["name"])
            mapped[key] = mapped.get(key, 0) + s["mapped"]
    x_reads, y_reads = mapped.get("X", 0), mapped.get("Y", 0)
    c7_reads, c19_reads = mapped.get("7", 0), mapped.get("19", 0)
    # Thresholds sit between the sexes as seen in real WGS, low-pass and exome
    # samples (hs37, hs38 and T2T). Y/X is 0.15+ for male WGS and at most 0.05
    # for females, so a male-level chrY settles it, even where chrX reads high
    # or a realigned Y extract has a few stray chrX reads. A male exome
    # captures few chrY targets (Y/X 0.04-0.07), so X/chr7 is checked next:
    # 0.77-1.17 for females and 0.38-0.60 for males, exomes included.
    if x_reads > 0 and y_reads > x_reads * 0.10:
        return SexCall("Male", "chrY/chrX mapped-read ratio", "high")
    if x_reads > 0 and c7_reads > 0:
        # Two X copies read about like chr7, one X about half of it
        sex = "Female" if x_reads > c7_reads * 0.65 else "Male"
        return SexCall(sex, "chrX/chr7 mapped-read ratio", "high")
    if x_reads > 0 and y_reads > 0:
        # Females still pick up a few mismapped chrY reads
        return SexCall("Female", "few chrY reads against chrX", "low")
    if y_reads > 0 and c19_reads > 0:
        # About half of chrY is N, so a male's Y is ~0.2-0.5 of chr19
        sex = "Male" if y_reads > c19_reads * 0.12 else "Female"
        return SexCall(sex, "chrY/chr19 mapped-read ratio", "medium")
    if y_reads > 0:
        return SexCall("Male", "chrY reads without chrX", "medium")
    return SexCall("Unknown", "no chrX or chrY reads", "low")


def record_sex(input_path: str, call: SexCall) -> None:
    """Record a known sex in the sample workspace, which warns when it
    contradicts the user's override."""
    if call.sex in sample_workspace.SEXES:
        sample_workspace.record(
            "sex", input_path, call.sex, method=call.method, confidence=call.confidence
        )


def cached_info(
    input_path: str, outdir: str | None = None, key: str = "gender"
) -> dict[str, Any] | None:
    """`info`'s cached metrics for this very file, if a cache with `key` exists
    in the output folder, beside the input or in the configured output folder."""
    name = f"{Path(input_path).name}.wgse_info.json"
    signature = calculate_bam_md5(input_path, None)
    for folder in (
        outdir,
        str(absolute_path(input_path).parent),
        settings.get("output_directory"),
    ):
        cache = str(Path(folder, name)) if folder else ""
        try:
            with open(cache) as f:
                info = json.load(f)
        except (OSError, ValueError):
            continue
        # The header signature is shared by every sample on that reference, so
        # the cache must also be from this very file (size and timestamp).
        st = os.stat(input_path)
        this_file = {"size": st.st_size, "mtime_ns": st.st_mtime_ns}
        if (
            isinstance(info, dict)
            and info.get("md5_signature") == signature
            and info.get("input_signature") == this_file
            and info.get(key)
        ):
            return info
    return None


def sample_sex(input_path: str, outdir: str | None = None) -> str:
    """The sample's sex: the user's override or the recorded call in its sample
    workspace, else `info`'s cached metrics for this file (counting chrY reads
    in a CRAM takes about a minute), else samtools idxstats. A new call is
    recorded in the workspace. An override is still checked against the reads,
    once per file revision, since it changes how chrX and chrY are called."""
    return str(
        sample_workspace.resolve(
            "sex", input_path, lambda: _detect_sex(input_path, outdir)
        )
    )


def _detect_sex(input_path: str, outdir: str | None) -> tuple[str, str | None]:
    info = cached_info(input_path, outdir)
    # A call cached by an older infer_sex is recomputed rather than trusted
    if info and info.get("sex_inference_version") == SEX_INFERENCE_VERSION:
        call = SexCall(str(info["gender"]), "cached `info --detailed` call", "high")
    else:
        logging.info("Checking chrX/chrY coverage to infer the sample's sex...")
        try:
            idx_stats, *_ = parse_idxstats(input_path)
        except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
            logging.warning(f"Could not check chrY coverage of {input_path}: {e}")
            return "Unknown", None
        call = infer_sex_call(idx_stats)
    record_sex(input_path, call)
    return call.sex, call.method


def parse_idxstats(filepath: str) -> tuple[list[IdxStat], int, int, int]:
    """Parse samtools idxstats for mapped/unmapped counts."""
    idx = run_command(["samtools", "idxstats", filepath], capture_output=True)
    stats: list[IdxStat] = []
    genome_len, total_mapped, total_unmapped = 0, 0, 0

    for line in idx.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 4:
            name, length, mapped, unmapped = (
                parts[0],
                int(parts[1]),
                int(parts[2]),
                int(parts[3]),
            )
            stats.append(
                {"name": name, "length": length, "mapped": mapped, "unmapped": unmapped}
            )
            if name != "*":
                genome_len += length
            total_mapped += mapped
            total_unmapped += unmapped

    return stats, genome_len, total_mapped, total_unmapped


def generate_chrom_table(
    idx_stats: Sequence[IdxStat],
    avg_len: float,
    gender: str,
    ref_model_name: str,
    coverage_map: Mapping[str, str] | None = None,
    n_counts: Mapping[str, int] | None = None,
    mapped_bases: Mapping[str, float] | None = None,
) -> list[list[Any]]:
    """Build the detailed per-chromosome metrics table: [key, name, length,
    N count, mapped segments, mapped Gbases, mean depth, breadth, mapped bases].

    A contig's mapped bases are its segments x `avg_len`, unless
    `mapped_bases` (by contig name) measures them, as long reads need."""
    valid_autos, valid_somal, valid_mito = (
        [str(i) for i in range(1, 23)],
        ["X", "Y"],
        ["M"],
    )
    if "DOG" in ref_model_name.upper():
        valid_autos = [str(i) for i in range(1, 39)]
        valid_somal = ["X"]
        valid_mito = ["M"]
        build = "Dog"
    elif "CAT" in ref_model_name.upper():
        valid_autos = [
            "A1",
            "A2",
            "A3",
            "B1",
            "B2",
            "B3",
            "B4",
            "C1",
            "C2",
            "D1",
            "D2",
            "D3",
            "D4",
            "E1",
            "E2",
            "E3",
            "F1",
            "F2",
        ]
        valid_somal = ["X", "Y"]
        valid_mito = ["M"]
        build = "Cat"
    else:
        build = {"hg38": "38", "hg19": "37"}.get(
            build_from_path(ref_model_name) or "", "99"
        )

    stats_autos: list[list[Any]] = []
    stats_somal: list[list[Any]] = []
    stats_mito: list[list[Any]] = []
    if coverage_map is None:
        coverage_map = {}

    # The hardcoded N counts describe the standard full-length assemblies, so use
    # them only when chr1 has a canonical length; a trimmed or custom reference
    # with the same contig names would otherwise get a negative effective length.
    # N counts measured from the actual reference (ref count-ns) always apply.
    contig_lengths = {s["name"]: s["length"] for s in idx_stats}
    is_standard_assembly = build_from_chr1_length(contig_lengths) is not None
    n_adjust_map = N_ADJUST.get(build, {}).copy() if is_standard_assembly else {}
    if n_counts:
        n_adjust_map.update(n_counts)

    stats_total: list[Any] = ["T", "Total", 0, 0, 0, 0.0, 0.0, "", 0.0]
    stats_altcont: list[Any] = ["O", "Other", 0, 0, 0, 0.0, 0.0, "", 0.0]
    measured = mapped_bases or {}

    for s in idx_stats:
        if s["mapped"] == 0:
            continue
        chromosome = s["name"]
        chromnum = contig_key(chromosome)
        # idxstats' unmapped column counts unmapped mates placed beside their
        # mapped mate on this contig; they aren't mapped segments.
        map_seg, mod_len = s["mapped"], s["length"]
        n_count = n_adjust_map.get(chromnum, 0)
        cvg = coverage_map.get(chromnum, "")
        bases = float(measured.get(chromosome, map_seg * avg_len))
        row = [chromnum, chromosome, mod_len, n_count, map_seg, 0.0, 0.0, cvg, bases]

        if chromnum in valid_autos:
            stats_autos.append(row)
        elif chromnum in valid_somal:
            stats_somal.append(row)
        elif chromnum in valid_mito:
            stats_mito.append(row)
        elif chromnum != "*":
            stats_altcont[2] += mod_len
            stats_altcont[4] += map_seg
            stats_altcont[8] += bases

    if build == "Cat":
        stats_autos.sort(key=lambda x: x[0])
    else:
        stats_autos.sort(key=lambda x: int(x[0]) if x[0].isdigit() else x[0])

    stats_somal.sort(key=lambda x: x[0])
    final_table = stats_autos + stats_somal + stats_mito

    for row in final_table:
        if row[0] == "Y" and gender == "Female" and build not in ["Dog", "Cat"]:
            continue
        stats_total[2] += row[2]
        stats_total[3] += row[3]
        stats_total[4] += row[4]
        stats_total[8] += row[8]

    final_table.append(stats_total)
    if int(stats_altcont[4]) > 0:
        final_table.append(stats_altcont)

    for i in range(len(final_table)):
        eff_len = final_table[i][2] - final_table[i][3] + 0.0001
        temp_mapped_gbases = final_table[i][8]
        final_table[i][5] = round(temp_mapped_gbases / (10**9), 2)
        final_table[i][6] = round(temp_mapped_gbases / eff_len)
        if final_table[i][1] in ["Total", "Other"]:
            final_table[i][7] = coverage_map.get(
                "TOTAL_EST" if final_table[i][1] == "Total" else "OTHER", ""
            )

    return final_table
