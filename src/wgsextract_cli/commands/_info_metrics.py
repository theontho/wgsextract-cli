import csv
import logging
import math
import os
import re
import subprocess
from typing import Any

from wgsextract_cli.core.constants import (
    N_ADJUST,
    SEQUENCERS,
)
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.utils import run_command
from wgsextract_cli.core.variant_files import popen


def determine_sequencer(qname):
    """Identify sequencer type from QNAME using regex patterns."""
    if not qname:
        return "Unknown"
    for name, pattern in SEQUENCERS.items():
        if re.search(pattern, qname):
            # Special logic for Dante MGI flow plate ID from legacy
            if "6xxx" in name:
                return name.replace("6xxx", qname[6:10] + " bad")
            return name
    # If unrecognized, return "Unknown" so we can show the full QNAME in detailed info
    return "Unknown"


def get_file_stats(filepath):
    """Retrieve file size and check for index existence."""
    size_bytes = os.path.getsize(filepath)
    size_gb = size_bytes / (1024**3)

    indexed = False
    # Check for direct appended index (e.g. file.bam.bai, file.cram.crai)
    if os.path.exists(filepath + ".bai") or os.path.exists(filepath + ".crai"):
        indexed = True
    # Check for replaced extension index (e.g. file.bai, file.crai)
    elif filepath.lower().endswith(".bam") and os.path.exists(filepath[:-4] + ".bai"):
        indexed = True
    elif filepath.lower().endswith(".cram") and os.path.exists(filepath[:-5] + ".crai"):
        indexed = True
    # Check for .csi index (sometimes used for large files)
    elif os.path.exists(filepath + ".csi"):
        indexed = True

    return size_gb, indexed


def run_body_sample(filepath, cram_opt):
    """Sample first 20k reads to calculate length, insert size, and read type."""
    logging.info(
        LOG_MESSAGES["sampling_metrics"].format(filename=os.path.basename(filepath))
    )
    cmd = ["samtools", "view"] + (cram_opt if cram_opt else []) + [filepath]

    total_len = count = paired_count = 0
    tlen_values = []
    len_values = []
    first_qname = None

    try:
        proc = popen(cmd, stdout=subprocess.PIPE, text=True, stderr=subprocess.DEVNULL)
        assert proc.stdout is not None
        for _ in range(20000):
            line = proc.stdout.readline()
            if not line:
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

        proc.terminate()
    except Exception:
        pass

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

    return count, avg_len, std_len, avg_tlen, std_tlen, is_paired, first_qname


def load_n_counts(ref_path):
    """Try to load chromosome N counts from a sidecar _ncnt.csv file."""
    if not ref_path or not os.path.isfile(ref_path):
        return {}

    prefix = re.sub(r"\.(fasta|fna|fa)(\.gz)?$", "", ref_path)
    ncnt_file = prefix + "_ncnt.csv"
    if not os.path.exists(ncnt_file):
        return {}

    logging.debug(LOG_MESSAGES["info_loading_n"].format(file=ncnt_file))
    n_counts = {}
    try:
        with open(ncnt_file, newline="") as f:
            # Try to detect if it's tab-separated or comma-separated
            first_line = f.readline()
            f.seek(0)
            delimiter = "\t" if "\t" in first_line else ","
            reader = csv.reader(f, delimiter=delimiter)
            for row in reader:
                if (
                    len(row) >= 3
                ):  # countingNs script uses SN, NumBP, NumNs (so index 2)
                    chrom, count = row[0], row[2]
                    if chrom.startswith("#"):
                        continue
                    # Normalize chrom name like generate_chrom_table does
                    cnum = chrom.upper().replace("CHR", "").replace("MT", "M")
                    try:
                        # Remove commas from number if present (e.g. 1,234,567)
                        clean_count = count.replace(",", "")
                        n_counts[cnum] = int(float(clean_count))
                    except ValueError:
                        pass
                elif len(row) >= 2:
                    chrom, count = row[0], row[1]
                    if chrom.startswith("#"):
                        continue
                    cnum = chrom.upper().replace("CHR", "").replace("MT", "M")
                    try:
                        clean_count = count.replace(",", "")
                        n_counts[cnum] = int(float(clean_count))
                    except ValueError:
                        pass
    except Exception as e:
        logging.debug(f"Failed to read {ncnt_file}: {e}")

    return n_counts


def parse_idxstats(filepath):
    """Parse samtools idxstats for mapped/unmapped counts."""
    idx = run_command(["samtools", "idxstats", filepath], capture_output=True)
    stats, genome_len, total_mapped, total_unmapped = [], 0, 0, 0

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
    idx_stats, avg_len, gender, ref_model_name, coverage_map=None, n_counts=None
):
    """Build the detailed per-chromosome metrics table."""
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
        build = (
            "38"
            if any(x in ref_model_name.upper() for x in ["38", "HG38", "GRCH38"])
            else "37"
            if any(x in ref_model_name.upper() for x in ["37", "HG19", "GRCH37"])
            else "19"
            if "19" in ref_model_name
            else "99"
        )

    stats_autos: list[list[Any]] = []
    stats_somal: list[list[Any]] = []
    stats_mito: list[list[Any]] = []
    if coverage_map is None:
        coverage_map = {}

    # Use loaded N counts if available, otherwise fallback to hardcoded defaults
    n_adjust_map = N_ADJUST.get(build, {}).copy()
    if n_counts:
        n_adjust_map.update(n_counts)

    stats_total: list[Any] = ["T", "Total", 0, 0, 0, 0.0, 0.0, ""]
    stats_altcont: list[Any] = ["O", "Other", 0, 0, 0, 0.0, 0.0, ""]

    for s in idx_stats:
        if s["mapped"] == 0:
            continue
        chromosome = s["name"]
        chromnum = chromosome.upper().replace("CHR", "").replace("MT", "M")
        map_seg, mod_len = s["mapped"] + s["unmapped"], s["length"]
        n_count = n_adjust_map.get(chromnum, 0)
        cvg = coverage_map.get(chromnum, "")
        row = [chromnum, chromosome, mod_len, n_count, map_seg, 0.0, 0.0, cvg]

        if chromnum in valid_autos:
            stats_autos.append(row)
        elif chromnum in valid_somal:
            stats_somal.append(row)
        elif chromnum in valid_mito:
            stats_mito.append(row)
        elif chromnum != "*":
            stats_altcont[2] += mod_len
            stats_altcont[4] += map_seg

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

    final_table.append(stats_total)
    if int(stats_altcont[4]) > 0:
        final_table.append(stats_altcont)

    for i in range(len(final_table)):
        eff_len = final_table[i][2] - final_table[i][3] + 0.0001
        temp_mapped_gbases = float(final_table[i][4] * avg_len)
        final_table[i][5] = round(temp_mapped_gbases / (10**9), 2)
        final_table[i][6] = round(temp_mapped_gbases / eff_len)
        if final_table[i][1] in ["Total", "Other"]:
            final_table[i][7] = coverage_map.get(
                "TOTAL_EST" if final_table[i][1] == "Total" else "OTHER", ""
            )

    return final_table
