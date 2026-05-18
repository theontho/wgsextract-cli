import json
import shlex
from dataclasses import asdict
from pathlib import Path
from typing import Any

from wgsextract_cli.core.utils import (
    WGSExtractError,
    run_command,
)

from ._benchmark_machine import (
    _format_stdout_report,
    _machine_stat_display_value,
    _system_metadata,
    _write_github_step_summary,
)
from ._benchmark_models import (
    BenchmarkResult,
    _read_fai,
)


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
        f"- Suite: `{metadata['suite']}`",
        f"- Coverage: `{metadata['coverage']}x`",
        f"- Tool runtime: `{metadata['tool_runtime']}`",
        f"- Full size reference: `{metadata['full_size']}`",
        f"- Data source: `{metadata['data_source']}` ({metadata['data_source_description']})",
        f"- Fake BAM generator: `{metadata['fake_bam_generator']}`",
        f"- Build: `{metadata['build']}`",
        f"- Region: `{metadata['region'] or 'whole generated genome'}`",
        f"- Seed: `{metadata['seed']}`",
        f"- Target SNP count: `{metadata['target_count']}`",
        f"- Threads: `{metadata['threads']}`",
        f"- Thread policy: `{metadata['thread_policy']}`",
        f"- Base file: `{metadata['base_file']}` ({metadata['base_file_size'] or 'not available'})",
        f"- Excluded operations: {metadata['excluded_operations']}",
        f"- JSON results: `{report_json}`",
        "",
        "## Machine Stats",
        "",
        "| Metric | Value |",
        "| --- | --- |",
    ]
    for label, key in (
        ("OS", "os"),
        ("Architecture", "architecture"),
        ("CPU", "cpu_model"),
        ("Cores", "cores"),
        ("CPU frequency", "cpu_frequency"),
        ("RAM", "ram"),
        ("RAM speed", "ram_speed"),
        ("Benchmark filesystem", "benchmark_filesystem"),
        ("Disk", "disk"),
        ("Drive", "drive"),
        ("Drive speed/type", "drive_speed"),
        ("Python", "python"),
    ):
        value = _machine_stat_display_value(metadata["machine_stats"], key)
        if value:
            lines.append(f"| {label} | {value} |")

    lines += [
        "",
        "## External Tools",
        "",
        "| Tool | Required | Status | Runtime | Path | Version | Purpose |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for tool in metadata["external_tools"]:
        lines.append(
            "| "
            f"{tool['name']} | "
            f"{tool['required']} | "
            f"{tool['status']} | "
            f"{tool['runtime'] or 'missing'} | "
            f"`{tool['path'] or 'missing'}` | "
            f"{tool['version'] or 'version unavailable'} | "
            f"{tool['purpose']} |"
        )

    lines += [
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

    _write_github_step_summary(metadata, results, report_md)
    print(stdout_report)


def _align_output_stem(fastq_path: Path) -> str:
    return fastq_path.name.split(".")[0]


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
