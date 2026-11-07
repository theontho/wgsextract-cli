"""Command-line entrypoint for the localization linter."""

from __future__ import annotations

import argparse
import sys

from .discovery import discover_bundles
from .models import BundleTarget, Finding, LocaleReport
from .parser import parse_toml_file
from .reporting import emit_json, print_text_report
from .rules import lint_locale, lint_source, update_source_hashes


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="lint_locales.py",
        description="Lints localization TOML files for GUI-for-CLI bundles.",
        epilog=(
            "PATH may be a bundle directory containing strings/, a strings folder, a "
            "strings.<code>.toml file, or omitted to auto-scan "
            "resources/BuiltinStrings/ plus examples/*/strings/ "
            "relative to the current working directory.\n\n"
            "Annotate intentional verbatim reuse with a trailing comment:\n"
            '  "bundle.displayName" = "WGS Extract"  # i18n-ignore'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat warnings (untranslated values, extra keys) as errors.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a machine-readable JSON report instead of plain text.",
    )
    parser.add_argument(
        "--update-source-hashes",
        action="store_true",
        help="Rewrite locale files in place, stamping each line with the current source hash.",
    )
    parser.add_argument(
        "paths", nargs="*", help="Bundle directories, strings folders, or strings.<code>.toml files."
    )
    args = parser.parse_args()

    bundles = discover_bundles(args.paths)
    if not bundles:
        sys.stderr.write(
            "No bundles found. Pass a bundle path or run from a directory with examples/.\n"
        )
        return 2

    if args.update_source_hashes:
        return update_hashes(bundles)

    bundle_results = lint_bundles(bundles)
    had_error = emit_json(bundle_results, args.strict) if args.json else print_text_report(
        bundle_results, args.strict
    )
    return 1 if had_error else 0


def update_hashes(bundles: list[BundleTarget]) -> int:
    total_updated = 0
    for bundle in bundles:
        source = parse_toml_file(bundle.source_path)
        for code, path in bundle.locales:
            count = update_source_hashes(source, path)
            if count:
                print(f"{bundle.name}/{code}: updated {count} line(s)")
                total_updated += count
    print(f"Updated {total_updated} line(s) total.")
    return 0


def lint_bundles(
    bundles: list[BundleTarget],
) -> list[tuple[BundleTarget, list[LocaleReport], list[Finding]]]:
    bundle_results: list[tuple[BundleTarget, list[LocaleReport], list[Finding]]] = []
    for bundle in bundles:
        source = parse_toml_file(bundle.source_path)
        source_findings = lint_source(source, requires_builtin=bundle.requires_builtin)
        locale_reports: list[LocaleReport] = []
        for code, path in bundle.locales:
            target = parse_toml_file(path)
            locale_reports.append(
                lint_locale(
                    source,
                    target,
                    bundle.name,
                    code,
                    requires_builtin=bundle.requires_builtin,
                )
            )
        bundle_results.append((bundle, locale_reports, source_findings))
    return bundle_results
