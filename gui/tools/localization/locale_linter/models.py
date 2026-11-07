"""Shared data models for localization linting."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class ParsedEntry:
    key: str
    value: str
    line: int
    ignore_untranslated: bool
    recorded_source_hash: Optional[str]


@dataclass
class ParsedFile:
    path: Path
    entries: list[ParsedEntry] = field(default_factory=list)
    duplicate_keys: list[tuple[str, int, int]] = field(default_factory=list)
    parse_errors: list[tuple[int, str]] = field(default_factory=list)
    key_index: dict[str, int] = field(default_factory=dict)

    def value_for(self, key: str) -> Optional[str]:
        idx = self.key_index.get(key)
        return self.entries[idx].value if idx is not None else None

    def line_for(self, key: str) -> Optional[int]:
        idx = self.key_index.get(key)
        return self.entries[idx].line if idx is not None else None


@dataclass
class Finding:
    severity: str
    code: str
    line: Optional[int]
    key: Optional[str]
    message: str


@dataclass
class LocaleReport:
    bundle_name: str
    locale_code: str
    path: Path
    total_keys: int
    findings: list[Finding]

    @property
    def error_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == "error")

    @property
    def warning_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == "warning")


@dataclass
class BundleTarget:
    name: str
    directory: Path
    source_path: Path
    source_code: str
    locales: list[tuple[str, Path]]
    requires_builtin: bool
