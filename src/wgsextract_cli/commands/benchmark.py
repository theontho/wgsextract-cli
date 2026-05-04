import argparse
import json
import os
import platform
import shlex
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from wgsextract_cli.core.messages import CLI_HELP
from wgsextract_cli.core.utils import WGSExtractError, run_command

PROFILE_DEFAULTS: dict[str, dict[str, Any]] = {
    "smoke": {
        "coverage": 0.1,
        "full_size": False,
        "region": "chrM",
        "target_count": 250,
    },
    "standard": {
        "coverage": 1.0,
        "full_size": False,
        "region": None,
        "target_count": 10_000,
    },
    "full": {
        "coverage": 1.0,
        "full_size": True,
        "region": None,
        "target_count": 250_000,
    },
}

EXCLUDED_OPERATIONS = "VEP, DeepVariant, Yleaf, Haplogrep"


@dataclass
class BenchmarkResult:
    name: str
    slug: str
    status: str
    seconds: float
    command: list[str]
    output_dir: str
    stdout_log: str | None = None
    stderr_log: str | None = None
    returncode: int | None = None
    expected_outputs: list[str] | None = None
    error: str | None = None

    @property
    def success(self) -> bool:
        return self.status == "PASS"


def register(
    subparsers: argparse._SubParsersAction, base_parser: argparse.ArgumentParser
):
    parser = subparsers.add_parser(
        "benchmark",
        parents=[base_parser],
        help=CLI_HELP["cmd_benchmark"],
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--profile",
        choices=sorted(PROFILE_DEFAULTS),
        default="standard",
        help=(
            "Benchmark size preset. smoke is chrM-only, standard is a scaled "
            "human-like genome, full uses real chromosome lengths."
        ),
    )
    parser.add_argument(
        "--coverage",
        type=float,
        help="Override generated BAM coverage for the selected profile.",
    )
    parser.add_argument(
        "--full-size",
        action="store_true",
        help="Use real chromosome lengths for the generated benchmark reference.",
    )
    parser.add_argument(
        "--build",
        choices=["hg38", "hg19", "hg37", "t2t"],
        default="hg38",
        help="Generated reference naming/build convention.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=20260504,
        help="Random seed for deterministic benchmark data generation.",
    )
    parser.add_argument(
        "--target-count",
        type=int,
        help="Approximate number of target SNPs for the microarray benchmark.",
    )
    parser.add_argument(
        "-r",
        "--region",
        help="Optional region to benchmark for region-aware operations.",
    )
    parser.add_argument(
        "--keep-going",
        action="store_true",
        help="Continue independent benchmark steps after a failed step when possible.",
    )
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    profile = PROFILE_DEFAULTS[args.profile]
    coverage = args.coverage if args.coverage is not None else profile["coverage"]
    full_size = bool(args.full_size or profile["full_size"])
    region = args.region if args.region else profile["region"]
    target_count = (
        args.target_count if args.target_count is not None else profile["target_count"]
    )

    if coverage <= 0:
        raise WGSExtractError("--coverage must be greater than zero.")
    if target_count <= 0:
        raise WGSExtractError("--target-count must be greater than zero.")

    outdir = _benchmark_root(args)
    run_dir = outdir / "runs" / datetime.now().strftime("%Y%m%d-%H%M%S")
    dataset_dir = run_dir / "dataset"
    steps_dir = run_dir / "steps"
    logs_dir = run_dir / "logs"
    for path in (dataset_dir, steps_dir, logs_dir):
        path.mkdir(parents=True, exist_ok=True)

    ref_path = dataset_dir / "benchmark_ref.fa"
    generated_bam = dataset_dir / "fake.bam"
    target_tab_gz = dataset_dir / "benchmark_targets.tab.gz"

    metadata = {
        "profile": args.profile,
        "coverage": coverage,
        "full_size": full_size,
        "build": args.build,
        "seed": args.seed,
        "region": region,
        "target_count": target_count,
        "excluded_operations": EXCLUDED_OPERATIONS,
        "run_dir": str(run_dir),
    }

    results: list[BenchmarkResult] = []

    def finish_report() -> None:
        _write_report(run_dir, metadata, results)

    def record(result: BenchmarkResult) -> BenchmarkResult:
        results.append(result)
        if not result.success and not args.keep_going:
            finish_report()
            raise WGSExtractError(
                f"Benchmark step failed: {result.name}. See {result.stderr_log}."
            )
        return result

    record(
        _run_cli_step(
            args,
            name="Generate deterministic BAM foundation",
            slug="00-generate-bam",
            command_args=[
                "qc",
                "fake-data",
                "--outdir",
                str(dataset_dir),
                "--ref",
                str(ref_path),
                "--build",
                args.build,
                "--coverage",
                str(coverage),
                "--type",
                "bam",
                "--seed",
                str(args.seed),
            ]
            + (["--full-size"] if full_size else []),
            output_dir=dataset_dir,
            logs_dir=logs_dir,
            expected_outputs=[
                generated_bam,
                Path(str(generated_bam) + ".bai"),
                ref_path,
            ],
        )
    )

    record(
        _run_cli_step(
            args,
            name="BAM metadata and sequencing metrics",
            slug="00a-info-detailed",
            command_args=[
                "info",
                "--detailed",
                "--input",
                str(generated_bam),
                "--ref",
                str(ref_path),
                "--outdir",
                str(steps_dir / "info"),
            ],
            output_dir=steps_dir / "info",
            logs_dir=logs_dir,
            expected_outputs=[],
        )
    )

    record(
        _run_cli_step(
            args,
            name="Index generated reference for alignment",
            slug="01-reference-index",
            command_args=["ref", "index", "--ref", str(ref_path)],
            output_dir=dataset_dir,
            logs_dir=logs_dir,
            expected_outputs=[
                Path(str(ref_path) + ".bwt"),
                Path(str(ref_path) + ".fai"),
            ],
        )
    )

    record(
        _run_internal_step(
            name="Generate deterministic microarray targets",
            slug="02-microarray-targets",
            output_dir=dataset_dir,
            func=lambda: _create_target_snp_tab(
                ref_path, target_tab_gz, target_count, region
            ),
            expected_outputs=[target_tab_gz, Path(str(target_tab_gz) + ".tbi")],
        )
    )

    unalign_dir = steps_dir / "bam-unalign"
    unalign_r1 = unalign_dir / "benchmark_R1.fastq.gz"
    unalign_r2 = unalign_dir / "benchmark_R2.fastq.gz"
    unalign_cmd = [
        "bam",
        "unalign",
        "--input",
        str(generated_bam),
        "--ref",
        str(ref_path),
        "--outdir",
        str(unalign_dir),
        "--r1",
        unalign_r1.name,
        "--r2",
        unalign_r2.name,
    ]
    if region:
        unalign_cmd += ["--region", str(region)]
    record(
        _run_cli_step(
            args,
            name="BAM unalignment to paired FASTQ",
            slug="03-bam-unalign",
            command_args=unalign_cmd,
            output_dir=unalign_dir,
            logs_dir=logs_dir,
            expected_outputs=[unalign_r1, unalign_r2],
        )
    )

    align_dir = steps_dir / "fastq-align"
    aligned_bam = align_dir / "benchmark_R1_aligned.bam"
    record(
        _run_cli_step(
            args,
            name="FASTQ alignment to BAM",
            slug="04-fastq-align",
            command_args=[
                "align",
                "--r1",
                str(unalign_r1),
                "--r2",
                str(unalign_r2),
                "--ref",
                str(ref_path),
                "--outdir",
                str(align_dir),
                "--format",
                "BAM",
            ],
            output_dir=align_dir,
            logs_dir=logs_dir,
            expected_outputs=[aligned_bam, Path(str(aligned_bam) + ".bai")],
        )
    )

    analysis_bam = aligned_bam if aligned_bam.exists() else generated_bam

    subset_dir = steps_dir / "bam-subset"
    subset_bam = subset_dir / f"{analysis_bam.stem}_subset.bam"
    subset_cmd = [
        "extract",
        "bam-subset",
        "--input",
        str(analysis_bam),
        "--ref",
        str(ref_path),
        "--outdir",
        str(subset_dir),
        "--fraction",
        "0.1",
    ]
    if region:
        subset_cmd += ["--region", str(region)]
    record(
        _run_cli_step(
            args,
            name="BAM subset extraction",
            slug="04a-bam-subset",
            command_args=subset_cmd,
            output_dir=subset_dir,
            logs_dir=logs_dir,
            expected_outputs=[subset_bam],
        )
    )

    mt_bam_dir = steps_dir / "mt-bam"
    mt_bam = mt_bam_dir / f"{analysis_bam.stem}_mtDNA.bam"
    record(
        _run_cli_step(
            args,
            name="Mitochondrial BAM extraction",
            slug="04b-mt-bam",
            command_args=[
                "extract",
                "mt-bam",
                "--input",
                str(analysis_bam),
                "--ref",
                str(ref_path),
                "--outdir",
                str(mt_bam_dir),
            ],
            output_dir=mt_bam_dir,
            logs_dir=logs_dir,
            expected_outputs=[mt_bam, Path(str(mt_bam) + ".bai")],
        )
    )

    ydna_bam_dir = steps_dir / "ydna-bam"
    ydna_bam = ydna_bam_dir / f"{analysis_bam.stem}_Y.bam"
    record(
        _run_cli_step(
            args,
            name="Y-DNA BAM extraction",
            slug="04c-ydna-bam",
            command_args=[
                "extract",
                "ydna-bam",
                "--input",
                str(analysis_bam),
                "--ref",
                str(ref_path),
                "--outdir",
                str(ydna_bam_dir),
            ],
            output_dir=ydna_bam_dir,
            logs_dir=logs_dir,
            expected_outputs=[ydna_bam, Path(str(ydna_bam) + ".bai")],
        )
    )

    sort_dir = steps_dir / "bam-sort"
    sorted_bam = sort_dir / f"{analysis_bam.stem}_sorted.bam"
    record(
        _run_cli_step(
            args,
            name="BAM coordinate sort",
            slug="05-bam-sort",
            command_args=[
                "bam",
                "sort",
                "--input",
                str(analysis_bam),
                "--ref",
                str(ref_path),
                "--outdir",
                str(sort_dir),
            ],
            output_dir=sort_dir,
            logs_dir=logs_dir,
            expected_outputs=[sorted_bam],
        )
    )

    cram_dir = steps_dir / "bam-to-cram"
    cram_path = cram_dir / f"{analysis_bam.stem}.cram"
    to_cram = record(
        _run_cli_step(
            args,
            name="BAM to CRAM conversion",
            slug="06-bam-to-cram",
            command_args=[
                "bam",
                "to-cram",
                "--input",
                str(analysis_bam),
                "--ref",
                str(ref_path),
                "--outdir",
                str(cram_dir),
            ],
            output_dir=cram_dir,
            logs_dir=logs_dir,
            expected_outputs=[cram_path, Path(str(cram_path) + ".crai")],
        )
    )

    bam_roundtrip_dir = steps_dir / "cram-to-bam"
    if to_cram.success:
        record(
            _run_cli_step(
                args,
                name="CRAM to BAM conversion",
                slug="07-cram-to-bam",
                command_args=[
                    "bam",
                    "to-bam",
                    "--input",
                    str(cram_path),
                    "--ref",
                    str(ref_path),
                    "--outdir",
                    str(bam_roundtrip_dir),
                ],
                output_dir=bam_roundtrip_dir,
                logs_dir=logs_dir,
                expected_outputs=[bam_roundtrip_dir / f"{cram_path.stem}.bam"],
            )
        )
    else:
        results.append(
            _skipped_result(
                "CRAM to BAM conversion",
                "07-cram-to-bam",
                bam_roundtrip_dir,
                "BAM to CRAM conversion failed.",
            )
        )

    snp_dir = steps_dir / "vcf-snp"
    snp_cmd = [
        "vcf",
        "snp",
        "--input",
        str(analysis_bam),
        "--ref",
        str(ref_path),
        "--outdir",
        str(snp_dir),
        "--ploidy",
        _ploidy_for_build(args.build),
    ]
    if region:
        snp_cmd += ["--region", str(region)]
    record(
        _run_cli_step(
            args,
            name="VCF SNP generation",
            slug="08-vcf-snp",
            command_args=snp_cmd,
            output_dir=snp_dir,
            logs_dir=logs_dir,
            expected_outputs=[snp_dir / "snps.vcf.gz", snp_dir / "snps.vcf.gz.tbi"],
        )
    )

    indel_dir = steps_dir / "vcf-indel"
    indel_cmd = [
        "vcf",
        "indel",
        "--input",
        str(analysis_bam),
        "--ref",
        str(ref_path),
        "--outdir",
        str(indel_dir),
        "--ploidy",
        _ploidy_for_build(args.build),
    ]
    if region:
        indel_cmd += ["--region", str(region)]
    record(
        _run_cli_step(
            args,
            name="VCF indel generation",
            slug="09-vcf-indel",
            command_args=indel_cmd,
            output_dir=indel_dir,
            logs_dir=logs_dir,
            expected_outputs=[
                indel_dir / "indels.vcf.gz",
                indel_dir / "indels.vcf.gz.tbi",
            ],
        )
    )

    microarray_dir = steps_dir / "microarray"
    combined_kit = microarray_dir / f"{analysis_bam.stem}_CombinedKit.txt"
    microarray_cmd = [
        "microarray",
        "--input",
        str(analysis_bam),
        "--ref",
        str(ref_path),
        "--outdir",
        str(microarray_dir),
        "--ref-vcf-tab",
        str(target_tab_gz),
        "--formats",
        "all",
    ]
    if region:
        microarray_cmd += ["--region", str(region)]
    record(
        _run_cli_step(
            args,
            name="Microarray CombinedKit generation",
            slug="10-microarray",
            command_args=microarray_cmd,
            output_dir=microarray_dir,
            logs_dir=logs_dir,
            expected_outputs=[combined_kit],
        )
    )

    qc_dir = steps_dir / "vcf-qc"
    record(
        _run_cli_step(
            args,
            name="VCF quality-control statistics",
            slug="11-vcf-qc",
            command_args=[
                "qc",
                "vcf",
                "--input",
                str(snp_dir / "snps.vcf.gz"),
                "--outdir",
                str(qc_dir),
            ],
            output_dir=qc_dir,
            logs_dir=logs_dir,
            expected_outputs=[qc_dir / "snps.vcf.gz.vcfstats.txt"],
        )
    )

    finish_report()


def _benchmark_root(args: argparse.Namespace) -> Path:
    explicit_dests: set[str] = getattr(args, "_explicit_dests", set())
    if "outdir" in explicit_dests and args.outdir:
        return Path(args.outdir).expanduser().resolve()
    return (Path.cwd() / "out" / "benchmark").resolve()


def _run_cli_step(
    args: argparse.Namespace,
    name: str,
    slug: str,
    command_args: list[str],
    output_dir: Path,
    logs_dir: Path,
    expected_outputs: list[Path],
) -> BenchmarkResult:
    output_dir.mkdir(parents=True, exist_ok=True)
    stdout_log = logs_dir / f"{slug}.stdout.log"
    stderr_log = logs_dir / f"{slug}.stderr.log"
    command = _cli_command(args, command_args)
    start = time.perf_counter()

    with (
        open(stdout_log, "w", encoding="utf-8") as out,
        open(stderr_log, "w", encoding="utf-8") as err,
    ):
        completed = subprocess.run(
            command,
            stdout=out,
            stderr=err,
            check=False,
            text=True,
            env=_subprocess_env(),
        )

    seconds = time.perf_counter() - start
    missing = [str(path) for path in expected_outputs if not path.exists()]
    status = "PASS" if completed.returncode == 0 and not missing else "FAIL"
    error = None
    if completed.returncode != 0:
        error = f"Command exited with status {completed.returncode}."
    elif missing:
        error = "Missing expected output(s): " + ", ".join(missing)

    return BenchmarkResult(
        name=name,
        slug=slug,
        status=status,
        seconds=seconds,
        command=command,
        output_dir=str(output_dir),
        stdout_log=str(stdout_log),
        stderr_log=str(stderr_log),
        returncode=completed.returncode,
        expected_outputs=[str(path) for path in expected_outputs],
        error=error,
    )


def _run_internal_step(
    name: str,
    slug: str,
    output_dir: Path,
    func: Any,
    expected_outputs: list[Path],
) -> BenchmarkResult:
    output_dir.mkdir(parents=True, exist_ok=True)
    start = time.perf_counter()
    error = None
    try:
        func()
    except Exception as exc:
        error = str(exc)
    seconds = time.perf_counter() - start
    missing = [str(path) for path in expected_outputs if not path.exists()]
    if missing:
        missing_text = "Missing expected output(s): " + ", ".join(missing)
        error = f"{error}; {missing_text}" if error else missing_text
    status = "PASS" if error is None else "FAIL"
    return BenchmarkResult(
        name=name,
        slug=slug,
        status=status,
        seconds=seconds,
        command=["internal", slug],
        output_dir=str(output_dir),
        expected_outputs=[str(path) for path in expected_outputs],
        error=error,
    )


def _skipped_result(
    name: str, slug: str, output_dir: Path, reason: str
) -> BenchmarkResult:
    return BenchmarkResult(
        name=name,
        slug=slug,
        status="SKIP",
        seconds=0.0,
        command=[],
        output_dir=str(output_dir),
        error=reason,
    )


def _cli_command(args: argparse.Namespace, command_args: list[str]) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "wgsextract_cli.main",
        "--parent-pid",
        str(os.getpid()),
    ]
    if getattr(args, "debug", False):
        command.append("--debug")
    elif getattr(args, "quiet", False):
        command.append("--quiet")
    command += command_args
    if getattr(args, "threads", None) is not None:
        command += ["--threads", str(args.threads)]
    if getattr(args, "memory", None) is not None:
        command += ["--memory", str(args.memory)]
    return command


def _subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    src_dir = Path(__file__).resolve().parents[2]
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{src_dir}{os.pathsep}{existing}" if existing else str(src_dir)
    return env


def _create_target_snp_tab(
    ref_path: Path, target_tab_gz: Path, target_count: int, region: str | None
) -> None:
    fai_path = Path(str(ref_path) + ".fai")
    if not fai_path.exists():
        run_command(["samtools", "faidx", str(ref_path)])

    contigs = _read_fai(fai_path)
    ranges = _target_ranges(contigs, region)
    if not ranges:
        raise WGSExtractError(f"No reference contigs match benchmark region: {region}")

    total_bases = sum(end - start + 1 for _name, start, end in ranges)
    tab_path = target_tab_gz.with_suffix("")
    bases = ["A", "C", "G", "T"]

    with open(tab_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("#CHROM\tPOS\tID\tREF\tALT\n")
        snp_index = 1
        for chrom, start, end in ranges:
            length = end - start + 1
            contig_targets = max(1, round(target_count * (length / total_bases)))
            step = max(1, length // (contig_targets + 1))
            for offset in range(step, length, step):
                if snp_index > target_count and len(ranges) == 1:
                    break
                pos = start + offset
                ref = bases[(snp_index + len(chrom)) % len(bases)]
                alt = bases[(snp_index + len(chrom) + 1) % len(bases)]
                if ref == alt:
                    alt = bases[(bases.index(ref) + 1) % len(bases)]
                handle.write(f"{chrom}\t{pos}\tbench_rs{snp_index}\t{ref}\t{alt}\n")
                snp_index += 1

    run_command(["bgzip", "-f", str(tab_path)])
    run_command(["tabix", "-f", "-p", "vcf", str(target_tab_gz)])


def _read_fai(fai_path: Path) -> list[tuple[str, int]]:
    contigs: list[tuple[str, int]] = []
    with open(fai_path, encoding="utf-8") as handle:
        for line in handle:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 2:
                contigs.append((parts[0], int(parts[1])))
    return contigs


def _target_ranges(
    contigs: list[tuple[str, int]], region: str | None
) -> list[tuple[str, int, int]]:
    if not region:
        return [(chrom, 1, length) for chrom, length in contigs]

    chrom_part, has_range, range_part = region.partition(":")
    matching = [(chrom, length) for chrom, length in contigs if chrom == chrom_part]
    if not matching and chrom_part.startswith("chr"):
        matching = [
            (chrom, length) for chrom, length in contigs if chrom == chrom_part[3:]
        ]
    if not matching and not chrom_part.startswith("chr"):
        matching = [
            (chrom, length) for chrom, length in contigs if chrom == f"chr{chrom_part}"
        ]

    ranges = []
    for chrom, length in matching:
        start = 1
        end = length
        if has_range:
            raw_start, _sep, raw_end = range_part.replace(",", "").partition("-")
            start = max(1, int(raw_start))
            end = min(length, int(raw_end) if raw_end else length)
        if start <= end:
            ranges.append((chrom, start, end))
    return ranges


def _ploidy_for_build(build: str) -> str:
    if build in {"hg19", "hg37"}:
        return "GRCh37"
    return "GRCh38"


def _write_report(
    run_dir: Path, metadata: dict[str, Any], results: list[BenchmarkResult]
) -> None:
    report_md = run_dir / "benchmark_report.md"
    report_json = run_dir / "benchmark_results.json"
    payload = {
        "metadata": metadata,
        "system": _system_metadata(),
        "results": [asdict(result) for result in results],
    }
    with open(report_json, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")

    stdout_report = _format_stdout_report(metadata, results, report_md)
    markdown_report = _format_markdown_report(metadata, results, report_json)
    with open(report_md, "w", encoding="utf-8") as handle:
        handle.write(markdown_report)

    print(stdout_report)


def _system_metadata() -> dict[str, str]:
    return {
        "platform": platform.platform(),
        "python": sys.version.replace("\n", " "),
        "executable": sys.executable,
    }


def _format_stdout_report(
    metadata: dict[str, Any], results: list[BenchmarkResult], report_md: Path
) -> str:
    total = sum(result.seconds for result in results if result.status != "SKIP")
    passed = sum(1 for result in results if result.status == "PASS")
    failed = sum(1 for result in results if result.status == "FAIL")
    skipped = sum(1 for result in results if result.status == "SKIP")

    lines = [
        "WGSExtract CLI Benchmark Report",
        f"Profile: {metadata['profile']} | Coverage: {metadata['coverage']}x | Full size: {metadata['full_size']}",
        f"Region: {metadata['region'] or 'whole generated genome'} | Seed: {metadata['seed']}",
        f"Excluded operations: {metadata['excluded_operations']}",
        "",
        f"{'Step':<42} {'Status':<6} {'Seconds':>10}",
        "-" * 62,
    ]
    for result in results:
        lines.append(f"{result.name:<42} {result.status:<6} {result.seconds:>10.2f}")
    lines += [
        "-" * 62,
        f"Total measured time: {total:.2f}s",
        f"Passed: {passed} | Failed: {failed} | Skipped: {skipped}",
        f"Markdown report: {report_md}",
    ]
    return "\n".join(lines)


def _format_markdown_report(
    metadata: dict[str, Any], results: list[BenchmarkResult], report_json: Path
) -> str:
    total = sum(result.seconds for result in results if result.status != "SKIP")
    lines = [
        "# WGSExtract CLI Benchmark Report",
        "",
        "## Configuration",
        "",
        f"- Profile: `{metadata['profile']}`",
        f"- Coverage: `{metadata['coverage']}x`",
        f"- Full size reference: `{metadata['full_size']}`",
        f"- Build: `{metadata['build']}`",
        f"- Region: `{metadata['region'] or 'whole generated genome'}`",
        f"- Seed: `{metadata['seed']}`",
        f"- Target SNP count: `{metadata['target_count']}`",
        f"- Excluded operations: {metadata['excluded_operations']}",
        f"- JSON results: `{report_json}`",
        "",
        "## Summary",
        "",
        "| Step | Status | Seconds | Output Directory |",
        "| --- | --- | ---: | --- |",
    ]
    for result in results:
        lines.append(
            f"| {result.name} | {result.status} | {result.seconds:.2f} | `{result.output_dir}` |"
        )
    lines += ["", f"Total measured time: **{total:.2f}s**", "", "## Details", ""]

    for result in results:
        lines += [
            f"### {result.name}",
            "",
            f"- Status: `{result.status}`",
            f"- Duration: `{result.seconds:.2f}s`",
            f"- Output directory: `{result.output_dir}`",
        ]
        if result.command:
            lines.append(f"- Command: `{shlex.join(result.command)}`")
        if result.returncode is not None:
            lines.append(f"- Return code: `{result.returncode}`")
        if result.stdout_log:
            lines.append(f"- Stdout log: `{result.stdout_log}`")
        if result.stderr_log:
            lines.append(f"- Stderr log: `{result.stderr_log}`")
        if result.expected_outputs:
            outputs = ", ".join(f"`{path}`" for path in result.expected_outputs)
            lines.append(f"- Expected outputs: {outputs}")
        if result.error:
            lines.append(f"- Error: {result.error}")
        lines.append("")

    return "\n".join(lines)
