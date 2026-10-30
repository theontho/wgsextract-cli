"""Time serial/parallel microarray runs and compare final exports (Linux).

Run inside an agent-coord exclusive lease. Inputs must be a real multi-contig
alignment with matching FASTA and target table; --region deliberately is absent.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import psutil

from wgsextract_cli.commands.benchmark.chip import CONSUMER_FORMATS
from wgsextract_cli.core.microarray_utils import get_template_format


def measure(command: list[str], output: Path) -> dict[str, float]:
    output.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    peak_tree_rss = 0
    environment = os.environ.copy()
    source = str(Path(__file__).resolve().parents[1] / "src")
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (source, environment.get("PYTHONPATH", "")) if part
    )
    with (
        (output / "stdout.log").open("w", encoding="utf-8", newline="\n") as stdout,
        (output / "stderr.log").open("w", encoding="utf-8", newline="\n") as stderr,
    ):
        process = subprocess.Popen(
            command,
            stdout=stdout,
            stderr=stderr,
            env=environment,
        )
        root = psutil.Process(process.pid)
        while True:
            pid, status, usage = os.wait4(process.pid, os.WNOHANG)
            if pid:
                process.returncode = os.waitstatus_to_exitcode(status)
                break
            rss = 0
            try:
                children = [root, *root.children(recursive=True)]
            except psutil.NoSuchProcess:
                children = []
            for child in children:
                try:
                    rss += child.memory_info().rss
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            peak_tree_rss = max(peak_tree_rss, rss)
            time.sleep(0.1)
        elapsed = time.perf_counter() - started
        if process.returncode:
            raise RuntimeError(
                f"Benchmark command failed ({process.returncode}); see {output}"
            )
    # wait4 includes resource use of children reaped by the command, unlike
    # sampling live process CPU counters, which misses short-lived descendants.
    user, system, rss_kib = usage.ru_utime, usage.ru_stime, usage.ru_maxrss
    return {
        "wall_seconds": elapsed,
        "user_seconds": user,
        "system_seconds": system,
        "cpu_seconds": user + system,
        "peak_process_rss_bytes": rss_kib * 1024,
        "sampled_peak_tree_rss_bytes": float(peak_tree_rss),
    }


def export_hashes(directory: Path) -> dict[str, str]:
    hashes = {}
    for path in sorted(directory.iterdir()):
        if path.suffix not in {
            ".txt",
            ".csv",
            ".bed",
            ".bim",
            ".fam",
            ".geno",
            ".snp",
            ".ind",
        }:
            continue
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        hashes[path.name] = digest.hexdigest()
    if not hashes:
        raise ValueError(f"No final exports found in {directory}")
    return hashes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--ref", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--ref-vcf-tab", type=Path, required=True)
    parser.add_argument("--outdir", type=Path, required=True)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--repeats", type=int, default=1)
    args = parser.parse_args()
    if sys.platform != "linux" or not hasattr(os, "wait4"):
        parser.error("Resource accounting requires Linux wait4")
    if args.repeats < 1 or args.threads < 2:
        parser.error("Use at least one pair and two threads")
    if args.input.suffix.lower() not in {".bam", ".cram"}:
        parser.error("Parallel calling must be measured from a BAM/CRAM alignment")
    for path in (args.input, args.ref, args.config, args.ref_vcf_tab):
        if not path.is_file():
            parser.error(f"Required input is missing: {path}")
    args.outdir.mkdir(parents=True, exist_ok=True)
    report_path = args.outdir / "parallel-comparison.json"
    if report_path.exists():
        parser.error("Use a fresh output directory to avoid stale comparisons")
    formats = ",".join(
        name.lower().replace("23andme_snps_api", "23andme_api")
        for name in CONSUMER_FORMATS
    )
    report: dict[str, object] = {
        "threads": args.threads,
        "repeats": args.repeats,
        "source_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "rss_definition": (
            "wait4 maximum individual-process RSS; sampled sum of live process-tree "
            "RSS every 100ms is also reported (shared pages counted more than once)."
        ),
    }
    results = []
    reference_hashes = None
    for repeat in range(args.repeats):
        for mode in (
            ("serial", "parallel") if repeat % 2 == 0 else ("parallel", "serial")
        ):
            run = args.outdir / f"{repeat + 1}-{mode}"
            command = [
                sys.executable,
                "-m",
                "wgsextract_cli.main",
                "microarray",
                "--config",
                str(args.config.resolve()),
                "--input",
                str(args.input.resolve()),
                "--ref",
                str(args.ref.resolve()),
                "--ref-vcf-tab",
                str(args.ref_vcf_tab.resolve()),
                "--formats",
                formats,
                "--threads",
                str(args.threads),
                "--outdir",
                str((run / "kit").resolve()),
            ]
            if mode == "parallel":
                command.append("--parallel")
            metrics = measure(command, run)
            if mode == "parallel":
                logs = (run / "stdout.log").read_text() + (
                    run / "stderr.log"
                ).read_text()
                chunks = re.search(r"Merging (\d+) chromosome VCFs", logs)
                if chunks is None or int(chunks[1]) < 2:
                    raise RuntimeError(
                        "The parallel run did not call at least two chromosomes"
                    )
            hashes = export_hashes(run / "kit")
            for name in ("CombinedKit", *CONSUMER_FORMATS):
                info = get_template_format(name)
                suffix = info["suffix"] if info else ".txt"
                if not any(path.endswith(f"_{name}{suffix}") for path in hashes):
                    raise RuntimeError(f"Missing requested final export: {name}")
            if reference_hashes is None:
                reference_hashes = hashes
            results.append(
                {
                    "repeat": repeat + 1,
                    "mode": mode,
                    **metrics,
                    "exports": hashes,
                    "byte_identical": hashes == reference_hashes,
                }
            )
            report["runs"] = results
            report["all_byte_identical"] = all(r["byte_identical"] for r in results)
            report_path.write_text(
                json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n"
            )
            print(f"{repeat + 1} {mode}: {json.dumps(metrics)}", flush=True)
    if not report["all_byte_identical"]:
        sys.exit("Serial/parallel final exports differ; do not change defaults.")


if __name__ == "__main__":
    main()
