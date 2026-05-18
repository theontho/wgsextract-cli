import csv
import io
import json
import logging
import os
import subprocess
from typing import Any

from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.utils import run_command
from wgsextract_cli.core.variant_files import popen


def render_info(data, detailed=False):
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

        def fmt_num(val):
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


def run_full_coverage(input_p, ref_p, out_p, region=None):
    """Long-running full breadth coverage pipeline."""
    if os.path.exists(out_p) and os.path.getsize(out_p) > 120:
        return
    print(LOG_MESSAGES["info_full_coverage"].format(path=out_p))
    awk = '{ names[$1]=$1 ; if($3==0){zero[$1]++} else {nz[$1]++ ; sumnz[$1]+=$3 ; if($3>7){nI[$1]++ ; sumnI[$1]+=$3} else {if($3>3){n7[$1]++ ; sumn7[$1]+=$3} else {n3[$1]++ ; sumn3[$1]+=$3} } } } END { printf("%s\\t%s\\t%s\\t%s\\t%s\\t%s\\t%s\\t%s\\t%s\\t%s\\t%s\\t%s\\t%s\\t%s\\n","chr","zero","nonzero","sum nz","fract nz","avg nz","avg all","TotalBC","Bet1-3","sum Bet1-3","Bet4-7","sum Bet4-7","Gtr7","sum Gtr7"); for (x in names) { totalbc = zero[x]+nz[x]+1 ; printf("%s\\t%d\\t%d\\t%d\\t%f\\t%f\\t%f\\t%d\\t%d\\t%d\\t%d\\t%d\\t%d\\t%d\\n",x,zero[x],nz[x],sumnz[x],nz[x]/totalbc,sumnz[x]/(nz[x]+1),sumnz[x]/totalbc,totalbc-1,n3[x],sumn3[x],n7[x],sumn7[x],nI[x],sumnI[x]) } }'

    opts = []
    if input_p.lower().endswith(".cram") and ref_p:
        if os.path.isfile(str(ref_p)):
            opts = ["--reference", str(ref_p)]

    try:
        region_args = ["-r", region] if region else []
        cmd = ["samtools", "depth", "-aa"] + region_args + opts + [input_p]
        cmd = [x for x in cmd if x is not None]
        p1 = popen(cmd, stdout=subprocess.PIPE)
        p2 = popen(["awk", awk], stdin=p1.stdout, stdout=open(out_p, "w"))
        if p1.stdout:
            p1.stdout.close()
        p2.communicate()
    except Exception as e:
        logging.error(f"Coverage failed: {e}")


def run_sampled_coverage(input_p, ref_p, idx_stats, out_p, region=None):
    """Fast sampling-based coverage estimation."""
    import random

    print(LOG_MESSAGES["info_sampling_coverage"])
    sample_results: dict[str, Any] = {}
    total_b, covered_b = 0, 0

    if region:
        chroms = [
            (
                s["name"],
                s["length"],
                s["name"].upper().replace("CHR", "").replace("MT", "M"),
            )
            for s in idx_stats
            if s["name"] == region
        ]
    else:
        chroms = [
            (
                s["name"],
                s["length"],
                s["name"].upper().replace("CHR", "").replace("MT", "M"),
            )
            for s in idx_stats
            if s["length"] > 100_000
            and (
                s["name"].upper().replace("CHR", "").isdigit()
                or s["name"].upper().replace("CHR", "") in ["X", "Y"]
            )
        ]

    if not chroms:
        return

    opts = []
    if input_p.lower().endswith(".cram") and ref_p:
        if os.path.isfile(str(ref_p)):
            opts = ["--reference", str(ref_p)]

    for _ in range(100):
        cname, clen, cnum = random.choice(chroms)
        start = random.randint(1, clen - 1000)
        cmd = (
            [
                "samtools",
                "depth",
                "-a",
                "-G",
                "0",
                "-Q",
                "0",
                "-r",
                f"{cname}:{start}-{start + 999}",
            ]
            + opts
            + [input_p]
        )
        cmd = [x for x in cmd if x is not None]
        res = run_command(cmd, capture_output=True)
        win_total = win_covered = 0
        if res.stdout:
            for line in res.stdout.splitlines():
                parts = line.split("\t")
                if len(parts) < 3:
                    continue
                win_total += 1
                if int(parts[2]) > 0:
                    win_covered += 1
        if win_total > 0:
            if cnum not in sample_results:
                sample_results[cnum] = []
            sample_results[cnum].append(win_covered / win_total)
            total_b += win_total
            covered_b += win_covered
    final_map = {
        cnum: f"~{sum(scores) / len(scores) * 100:.0f} %"
        for cnum, scores in sample_results.items()
    }
    if total_b > 0:
        final_map["TOTAL_EST"] = f"~{covered_b / total_b * 100:.0f} %"
    with open(out_p, "w") as f:
        json.dump(final_map, f)
