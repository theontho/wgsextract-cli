"""Text and JSON reporting for localization lint results."""

from __future__ import annotations

import json
import sys

from .models import BundleTarget, Finding, LocaleReport

BundleResults = list[tuple[BundleTarget, list[LocaleReport], list[Finding]]]

_IS_TTY = sys.stdout.isatty()


def color(text: str, ansi: str) -> str:
    return f"\033[{ansi}m{text}\033[0m" if _IS_TTY else text


def print_text_report(bundles: BundleResults, strict: bool) -> bool:
    had_error = False
    for bundle, locales, source_findings in bundles:
        print(color(f"=== {bundle.name} ({bundle.directory})", "1;36"))
        if source_findings:
            print(color(f"  source {bundle.source_path.name}:", "1"))
            for finding in source_findings:
                print_finding(finding, indent="    ")
                had_error = had_error or finding.severity == "error" or strict
        if not locales:
            print("  (no locale files found)")
            continue
        for report in locales:
            summary = (
                f"  [{report.locale_code}] {report.total_keys} keys, "
                f"{report.error_count} errors, {report.warning_count} warnings"
            )
            if report.error_count:
                colored_summary = color(summary, "31")
            elif report.warning_count:
                colored_summary = color(summary, "33")
            else:
                colored_summary = color(summary, "32")
            print(colored_summary)
            for finding in report.findings:
                print_finding(finding, indent="    ")
            had_error = had_error or report.error_count > 0 or (strict and report.warning_count > 0)
    return had_error


def print_finding(finding: Finding, indent: str) -> None:
    tag = color("error", "31") if finding.severity == "error" else color("warn ", "33")
    location = f":{finding.line}" if finding.line is not None else ""
    key_part = f" [{finding.key}]" if finding.key else ""
    print(f"{indent}{tag} {finding.code}{location}{key_part} — {finding.message}")


def emit_json(bundles: BundleResults, strict: bool) -> bool:
    had_error = False
    bundles_payload: list[dict] = []
    for bundle, locales, source_findings in bundles:
        for finding in source_findings:
            had_error = had_error or finding.severity == "error" or strict
        locales_payload: list[dict] = []
        for report in locales:
            had_error = had_error or report.error_count > 0 or (strict and report.warning_count > 0)
            locales_payload.append(
                {
                    "code": report.locale_code,
                    "path": str(report.path),
                    "totalKeys": report.total_keys,
                    "errors": report.error_count,
                    "warnings": report.warning_count,
                    "findings": [_finding_dict(finding) for finding in report.findings],
                }
            )
        bundles_payload.append(
            {
                "name": bundle.name,
                "path": str(bundle.directory),
                "source": str(bundle.source_path),
                "sourceFindings": [_finding_dict(finding) for finding in source_findings],
                "locales": locales_payload,
            }
        )
    print(json.dumps({"bundles": bundles_payload, "ok": not had_error}, indent=2, sort_keys=True))
    return had_error


def _finding_dict(finding: Finding) -> dict:
    out: dict = {"severity": finding.severity, "code": finding.code, "message": finding.message}
    if finding.line is not None:
        out["line"] = finding.line
    if finding.key is not None:
        out["key"] = finding.key
    return out
