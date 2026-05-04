import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wgsextract_cli.core.utils import run_command  # noqa: E402


def run_step(name, cmd, log_dir, stdout_path=None):
    start = time.perf_counter()
    log_path = log_dir / f"{name}.log"
    try:
        if stdout_path:
            with open(stdout_path, "w", encoding="utf-8") as stdout_file:
                result = run_command(cmd, stdout=stdout_file, capture_output=False)
            stdout_text = ""
            stderr_text = ""
        else:
            result = run_command(cmd, capture_output=True)
            stdout_text = result.stdout or ""
            stderr_text = result.stderr or ""

        duration = time.perf_counter() - start
        log_path.write_text(
            "COMMAND: "
            + " ".join(str(part) for part in cmd)
            + "\n"
            + f"RETURN_CODE: {result.returncode}\n"
            + f"DURATION_SECONDS: {duration:.3f}\n\n"
            + "STDOUT:\n"
            + stdout_text
            + "\nSTDERR:\n"
            + stderr_text,
            encoding="utf-8",
        )
        return {"name": name, "status": "pass", "seconds": round(duration, 3)}
    except Exception as exc:
        duration = time.perf_counter() - start
        log_path.write_text(
            "COMMAND: "
            + " ".join(str(part) for part in cmd)
            + "\n"
            + f"DURATION_SECONDS: {duration:.3f}\n"
            + f"ERROR: {exc}\n",
            encoding="utf-8",
        )
        return {
            "name": name,
            "status": "fail",
            "seconds": round(duration, 3),
            "error": str(exc),
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--region", default="chr1:1-500000")
    args = parser.parse_args()

    data_dir = Path(args.data_dir).resolve()
    out_dir = Path(args.out_dir).resolve()
    log_dir = out_dir / "logs"
    result_dir = out_dir / "results"
    log_dir.mkdir(parents=True, exist_ok=True)
    result_dir.mkdir(parents=True, exist_ok=True)

    bam = data_dir / "fake.bam"
    vcf = data_dir / "fake.vcf.gz"
    ref_candidates = sorted(data_dir.glob("fake_ref_hg38_*.fa"))
    if not ref_candidates:
        raise SystemExit(f"No generated reference found in {data_dir}")
    ref = ref_candidates[0]
    r1 = data_dir / "fake_R1.fastq.gz"

    mini_vcf = result_dir / "mini.vcf"
    mini_vcf.write_text(
        "##fileformat=VCFv4.2\n"
        "##contig=<ID=chr1,length=501000>\n"
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
        "chr1\t1000\tstress1\tA\tG\t60\tPASS\t.\n",
        encoding="utf-8",
    )
    mini_vcf_gz = result_dir / "mini.vcf.gz"
    subset_bam = result_dir / "subset.bam"
    subset_cram = result_dir / "subset.cram"
    roundtrip_bam = result_dir / "roundtrip.bam"
    archive = result_dir / "stress_manifest.tar.gz"
    manifest = result_dir / "manifest.txt"
    manifest.write_text(
        f"bam={bam}\nvcf={vcf}\nref={ref}\nregion={args.region}\n",
        encoding="utf-8",
    )

    steps = [
        ("samtools_quickcheck", ["samtools", "quickcheck", str(bam)], None),
        ("samtools_flagstat", ["samtools", "flagstat", str(bam)], None),
        ("samtools_idxstats", ["samtools", "idxstats", str(bam)], None),
        (
            "samtools_region_count",
            ["samtools", "view", "-c", str(bam), args.region],
            None,
        ),
        (
            "samtools_depth_chrM",
            ["samtools", "depth", "-r", "chrM", str(bam)],
            result_dir / "chrM.depth.txt",
        ),
        (
            "samtools_subset_bam",
            ["samtools", "view", "-bh", "-o", str(subset_bam), str(bam), args.region],
            None,
        ),
        ("samtools_index_subset", ["samtools", "index", str(subset_bam)], None),
        (
            "samtools_to_cram",
            [
                "samtools",
                "view",
                "-Ch",
                "-T",
                str(ref),
                "-o",
                str(subset_cram),
                str(subset_bam),
            ],
            None,
        ),
        ("samtools_index_cram", ["samtools", "index", str(subset_cram)], None),
        (
            "samtools_roundtrip_bam",
            [
                "samtools",
                "view",
                "-bh",
                "-T",
                str(ref),
                "-o",
                str(roundtrip_bam),
                str(subset_cram),
            ],
            None,
        ),
        ("bcftools_view_header", ["bcftools", "view", "-h", str(vcf)], None),
        (
            "bcftools_stats",
            ["bcftools", "stats", str(vcf)],
            result_dir / "fake.vcf.stats.txt",
        ),
        (
            "bcftools_region_view",
            ["bcftools", "view", "-r", args.region, str(vcf)],
            result_dir / "region.vcf",
        ),
        ("htsfile_bam", ["htsfile", str(bam)], None),
        ("htsfile_vcf", ["htsfile", str(vcf)], None),
        ("bgzip_mini_vcf", ["bgzip", "-f", str(mini_vcf)], None),
        ("tabix_mini_vcf", ["tabix", "-p", "vcf", str(mini_vcf_gz)], None),
        ("gzip_test_fastq", ["gzip", "-t", str(r1)], None),
        (
            "tar_create",
            ["tar", "-czf", str(archive), "-C", str(result_dir), "manifest.txt"],
            None,
        ),
        ("tar_list", ["tar", "-tzf", str(archive)], None),
    ]

    results = []
    for name, cmd, stdout_path in steps:
        result = run_step(name, cmd, log_dir, stdout_path)
        results.append(result)
        print(f"{result['status'].upper():<4} {name:<24} {result['seconds']:>8.3f}s")

    summary = {
        "runtime": os.environ.get("WGSEXTRACT_TOOL_RUNTIME", ""),
        "bam_size_bytes": bam.stat().st_size,
        "vcf_size_bytes": vcf.stat().st_size,
        "reference": str(ref),
        "results": results,
    }
    summary_path = out_dir / "tool_stress_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    failed = [result for result in results if result["status"] != "pass"]
    if failed:
        raise SystemExit(f"{len(failed)} stress step(s) failed; see {log_dir}")


if __name__ == "__main__":
    main()
