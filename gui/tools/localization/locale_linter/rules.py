"""Localization lint rules and source-hash rewriting."""

from __future__ import annotations

import hashlib
from pathlib import Path

from .constants import BUILTIN_REQUIRED_KEYS, VALID_LAYOUT_DIRECTIONS
from .models import Finding, LocaleReport, ParsedFile
from .parser import split_value_and_comment, unquote_key


def lint_locale(
    source: ParsedFile,
    target: ParsedFile,
    bundle_name: str,
    locale_code: str,
    requires_builtin: bool,
) -> LocaleReport:
    findings = common_file_findings(target)
    source_keys = {entry.key for entry in source.entries}
    target_keys = {entry.key for entry in target.entries}
    builtin_set = set(BUILTIN_REQUIRED_KEYS) if requires_builtin else set()
    required_keys = source_keys | builtin_set

    for missing in sorted(required_keys - target_keys):
        is_builtin = missing in builtin_set and missing not in source_keys
        findings.append(
            Finding(
                "error",
                "missing-builtin-key" if is_builtin else "missing-key",
                None,
                missing,
                "Required built-in key not provided"
                if is_builtin
                else "Key present in source is missing",
            )
        )

    for extra in sorted(target_keys - required_keys):
        findings.append(
            Finding(
                "warning",
                "extra-key",
                target.line_for(extra),
                extra,
                "Key not present in source strings file"
                + (" or built-in list" if requires_builtin else ""),
            )
        )

    for entry in target.entries:
        if not entry.value.strip():
            findings.append(
                Finding("error", "empty-value", entry.line, entry.key, "Empty translation value")
            )

        if entry.key == "language.layoutDirection":
            normalized = entry.value.strip().lower()
            if normalized not in VALID_LAYOUT_DIRECTIONS:
                findings.append(
                    Finding(
                        "error",
                        "invalid-layout-direction",
                        entry.line,
                        entry.key,
                        f'language.layoutDirection must be "ltr" or "rtl" (got "{entry.value}")',
                    )
                )
            continue

        if entry.key == "language.code":
            trimmed = entry.value.strip()
            if trimmed != locale_code:
                findings.append(
                    Finding(
                        "error",
                        "language-code-mismatch",
                        entry.line,
                        entry.key,
                        f'language.code is "{trimmed}" but file is "strings.{locale_code}.toml"',
                    )
                )
            continue

        if entry.key == "language.name":
            continue

        source_value = source.value_for(entry.key)
        if (
            source_value is not None
            and source_value == entry.value
            and not entry.ignore_untranslated
            and entry.value.strip()
        ):
            findings.append(
                Finding(
                    "warning",
                    "untranslated",
                    entry.line,
                    entry.key,
                    "Value matches English source verbatim (add `# i18n-ignore` if intentional)",
                )
            )

        if (
            source_value is not None
            and entry.recorded_source_hash is not None
            and entry.recorded_source_hash != short_source_hash(source_value)
        ):
            findings.append(
                Finding(
                    "warning",
                    "source-changed",
                    entry.line,
                    entry.key,
                    "Source string has changed since translation; "
                    "retranslate then run --update-source-hashes",
                )
            )

    return LocaleReport(bundle_name, locale_code, target.path, len(target.entries), findings)


def lint_source(source: ParsedFile, requires_builtin: bool) -> list[Finding]:
    findings = common_file_findings(source)
    keys = {entry.key for entry in source.entries}
    if requires_builtin:
        for required in BUILTIN_REQUIRED_KEYS:
            if required not in keys:
                findings.append(
                    Finding(
                        "error",
                        "missing-builtin-key",
                        None,
                        required,
                        "Source file is missing required built-in key",
                    )
                )
    for entry in source.entries:
        if not entry.value.strip():
            findings.append(
                Finding("error", "empty-value", entry.line, entry.key, "Empty value in source file")
            )
        if entry.key == "language.layoutDirection":
            normalized = entry.value.strip().lower()
            if normalized not in VALID_LAYOUT_DIRECTIONS:
                findings.append(
                    Finding(
                        "error",
                        "invalid-layout-direction",
                        entry.line,
                        entry.key,
                        'language.layoutDirection must be "ltr" or "rtl"',
                    )
                )
    return findings


def common_file_findings(parsed: ParsedFile) -> list[Finding]:
    findings: list[Finding] = []
    for line, message in parsed.parse_errors:
        findings.append(Finding("error", "parse-error", line, None, message))
    for key, line, prev_line in parsed.duplicate_keys:
        findings.append(
            Finding(
                "error",
                "duplicate-key",
                line,
                key,
                f"Duplicate key (previously defined on line {prev_line})",
            )
        )
    return findings


def short_source_hash(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8"), usedforsecurity=False).hexdigest()[:8]


def update_source_hashes(source: ParsedFile, target_path: Path) -> int:
    try:
        raw = target_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return 0
    original_ends_with_newline = raw.endswith("\n")
    lines = raw.split("\n")
    if original_ends_with_newline and lines and lines[-1] == "":
        lines.pop()

    updated = 0
    multiline_delimiter = ""
    for idx, line in enumerate(lines):
        trimmed = line.strip()
        if multiline_delimiter:
            if multiline_delimiter in trimmed:
                multiline_delimiter = ""
            continue
        if not trimmed or trimmed.startswith("#"):
            continue
        eq = trimmed.find("=")
        if eq < 0:
            continue
        key = unquote_key(trimmed[:eq].strip())
        source_value = source.value_for(key)
        if source_value is None:
            continue
        rest = trimmed[eq + 1 :].strip()
        delimiter = next((token for token in ('"""', "'''") if rest.startswith(token)), None)
        if delimiter is not None:
            if rest.count(delimiter) < 2:
                multiline_delimiter = delimiter
            continue
        value_part, comment_part = split_value_and_comment(line)
        if not value_part.strip().endswith('"'):
            continue
        new_comment = merge_source_hash(comment_part, short_source_hash(source_value))
        stripped_value = value_part.rstrip(" \t")
        rebuilt = stripped_value if not new_comment else f"{stripped_value}  {new_comment}"
        if rebuilt != line:
            lines[idx] = rebuilt
            updated += 1

    joined = "\n".join(lines)
    if original_ends_with_newline:
        joined += "\n"
    target_path.write_text(joined, encoding="utf-8")
    return updated


def merge_source_hash(comment: str, hash_hex: str) -> str:
    trimmed = comment.strip()
    if not trimmed:
        return f"# i18n-source-hash:{hash_hex}"
    body = trimmed[1:].strip() if trimmed.startswith("#") else trimmed
    pieces = [piece for piece in body.split(" ") if piece]
    kept = [piece for piece in pieces if not piece.lower().startswith("i18n-source-hash:")]
    kept.append(f"i18n-source-hash:{hash_hex}")
    return "# " + " ".join(kept)
