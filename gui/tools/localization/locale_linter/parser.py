"""Line-aware TOML parsing helpers for string tables."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from .models import ParsedEntry, ParsedFile


def parse_toml_file(path: Path) -> ParsedFile:
    parsed = ParsedFile(path=path)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        parsed.parse_errors.append((0, "Could not read file as UTF-8"))
        return parsed

    lines = text.split("\n")
    i = 0
    while i < len(lines):
        raw_line = lines[i]
        line_number = i + 1
        i += 1
        trimmed = raw_line.strip()
        if not trimmed or trimmed.startswith("#"):
            continue

        eq = trimmed.find("=")
        if eq < 0:
            parsed.parse_errors.append((line_number, f"Missing `=` separator: {trimmed}"))
            continue

        raw_key = trimmed[:eq].strip()
        raw_value_and_comment = trimmed[eq + 1 :].strip()
        key = unquote_key(raw_key)
        if not key:
            parsed.parse_errors.append((line_number, "Empty key"))
            continue

        if raw_value_and_comment.startswith('"""'):
            body = raw_value_and_comment[3:]
            collected: list[str] = []
            end_in_body = body.find('"""')
            if end_in_body >= 0:
                collected.append(body[:end_in_body])
            else:
                collected.append(body)
                found_end = False
                while i < len(lines):
                    nxt = lines[i]
                    i += 1
                    end = nxt.find('"""')
                    if end >= 0:
                        collected.append(nxt[:end])
                        found_end = True
                        break
                    collected.append(nxt)
                if not found_end:
                    parsed.parse_errors.append(
                        (line_number, f"Unterminated multiline string for key {key}")
                    )
                    continue
            if collected and collected[0] == "":
                collected.pop(0)
            if collected and collected[-1] == "":
                collected.pop()
            _record(parsed, key, "\n".join(collected), line_number, False, None)
            continue

        value_literal, comment = split_value_and_comment(raw_value_and_comment)
        value_literal = value_literal.strip()
        if value_literal in {"true", "false"}:
            _record(
                parsed,
                key,
                value_literal,
                line_number,
                "i18n-ignore" in comment.lower(),
                extract_source_hash(comment),
            )
            continue
        if not (value_literal.startswith('"') and value_literal.endswith('"') and len(value_literal) >= 2):
            parsed.parse_errors.append(
                (line_number, f"Value must be a double-quoted string: {value_literal}")
            )
            continue
        value = unescape(value_literal[1:-1])
        _record(
            parsed,
            key,
            value,
            line_number,
            "i18n-ignore" in comment.lower(),
            extract_source_hash(comment),
        )

    return parsed


def _record(
    parsed: ParsedFile,
    key: str,
    value: str,
    line: int,
    ignore_untranslated: bool,
    recorded_source_hash: Optional[str],
) -> None:
    if key in parsed.key_index:
        prev = parsed.entries[parsed.key_index[key]].line
        parsed.duplicate_keys.append((key, line, prev))
    parsed.key_index[key] = len(parsed.entries)
    parsed.entries.append(
        ParsedEntry(
            key=key,
            value=value,
            line=line,
            ignore_untranslated=ignore_untranslated,
            recorded_source_hash=recorded_source_hash,
        )
    )


_SOURCE_HASH_RE = re.compile(r"i18n-source-hash:([0-9a-fA-F]+)", re.IGNORECASE)


def extract_source_hash(comment: str) -> Optional[str]:
    match = _SOURCE_HASH_RE.search(comment)
    return match.group(1).lower() if match else None


def split_value_and_comment(raw: str) -> tuple[str, str]:
    in_string = False
    escaped = False
    split_at: Optional[int] = None
    for idx, ch in enumerate(raw):
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if ch == "#" and not in_string:
            split_at = idx
            break
    if split_at is None:
        return raw, ""
    return raw[:split_at].strip(), raw[split_at:].strip()


def unquote_key(key: str) -> str:
    if len(key) >= 2 and key.startswith('"') and key.endswith('"'):
        return key[1:-1]
    return key


_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}


def unescape(value: str) -> str:
    result: list[str] = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch != "\\":
            result.append(ch)
            i += 1
            continue
        if i + 1 >= len(value):
            result.append("\\")
            break
        nxt = value[i + 1]
        replacement = _ESCAPES.get(nxt)
        if replacement is not None:
            result.append(replacement)
        else:
            result.append("\\")
            result.append(nxt)
        i += 2
    return "".join(result)
