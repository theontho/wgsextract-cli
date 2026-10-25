import json
import os
import platform
import shlex
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .machine import (
    MACHINE_STAT_FIELDS,
    _machine_stat_display_value,
)
from .models import BenchmarkResult


def _system_metadata() -> dict[str, str]:
    return {
        "platform": platform.platform(),
        "python": sys.version.replace("\n", " "),
        "executable": sys.executable,
    }


def _format_tool_names(tools: list[dict[str, str | bool | None]]) -> str:
    active_tools = [str(tool["name"]) for tool in tools if tool["active"]]
    return ", ".join(active_tools) if active_tools else "none"


def _format_machine_summary(stats: dict[str, str | int | None]) -> str:
    cores = _machine_stat_display_value(stats, "cores") or "unknown cores"
    ram = stats.get("ram_total") or "unknown RAM"
    disk_free = stats.get("disk_free") or "unknown disk free"
    os_name = stats.get("os") or "unknown OS"
    cpu = stats.get("cpu_model") or "unknown CPU"
    return f"{os_name} | {cpu} | {cores} | RAM {ram} | disk free {disk_free}"


def _format_stdout_report(
    metadata: dict[str, Any], results: list[BenchmarkResult], report_md: Path
) -> str:
    total = sum(result.seconds for result in results if result.status != "SKIP")
    passed = sum(1 for result in results if result.status == "PASS")
    failed = sum(1 for result in results if result.status == "FAIL")
    skipped = sum(1 for result in results if result.status == "SKIP")

    lines = [
        "",
        "WGSExtract CLI Benchmark Summary",
        f"Profile: {metadata['profile']} | Suite: {metadata['suite']} | "
        f"Coverage: {metadata['coverage']}x | Full size: {metadata['full_size']}",
        f"Tool runtime: {metadata['tool_runtime']}",
        f"Data source: {metadata['data_source']} ({metadata['data_source_description']})",
        f"Fake BAM generator: {metadata['fake_bam_generator']}",
        f"Threads: {metadata['threads']}",
        f"Thread policy: {metadata['thread_policy']}",
        f"Region: {metadata['region'] or 'whole generated genome'} | Seed: {metadata['seed']}",
        f"Base file: {metadata['base_file']} ({metadata['base_file_size'] or 'not available'})",
        "Machine: " + _format_machine_summary(metadata["machine_stats"]),
        "External tools: " + _format_tool_names(metadata["external_tools"]),
        f"Excluded operations: {metadata['excluded_operations']}",
    ]
    lines += [
        f"Total measured time: {total:.2f}s",
        f"Passed: {passed} | Failed: {failed} | Skipped: {skipped}",
        f"Markdown report: {report_md}",
    ]
    return "\n".join(lines)


def _write_github_step_summary(
    metadata: dict[str, Any], results: list[BenchmarkResult], report_md: Path
) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return

    total = sum(result.seconds for result in results if result.status != "SKIP")
    passed = sum(1 for result in results if result.status == "PASS")
    failed = sum(1 for result in results if result.status == "FAIL")
    skipped = sum(1 for result in results if result.status == "SKIP")
    lines = [
        "## WGSExtract Benchmark",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Total measured time | {total:.2f}s |",
        f"| Passed | {passed} |",
        f"| Failed | {failed} |",
        f"| Skipped | {skipped} |",
        f"| Suite | {metadata['suite']} |",
        f"| Profile | {metadata['profile']} |",
        f"| Data source | {metadata['data_source']} |",
        f"| Base file size | {metadata['base_file_size'] or 'not available'} |",
        f"| Report | `{report_md}` |",
        "",
    ]
    with open(summary_path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")


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
    machine_report_fields = (
        *MACHINE_STAT_FIELDS[:-1],
        ("Drive speed/type", "drive_speed"),
        ("Python", "python"),
    )
    for label, key in machine_report_fields:
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
    with open(report_json, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")

    stdout_report = _format_stdout_report(metadata, results, report_md)
    markdown_report = _format_markdown_report(metadata, results, report_json)
    with open(report_md, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(markdown_report)

    _write_github_step_summary(metadata, results, report_md)
    print(stdout_report)
