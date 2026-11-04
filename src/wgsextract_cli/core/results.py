"""Result manifests: the typed record of what one command read, wrote and found.

A command reaches the active ``CommandResult`` through ``current_result()`` and
records inputs, outputs, metrics, warnings and a ``details`` payload on it.
``cli.dispatch`` creates the result, maps exceptions to exit codes, writes
``<output-stem>.manifest.json`` next to the outputs and, with ``--json``,
prints the same document to stdout. The model is plain dataclasses so library
callers (the typed API, server mode) can return it directly.

The document follows ``assets/schemas/wgsextract.result.1.schema.json``;
docs/result-manifest.md describes it.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import json
import logging
import os
import platform as _platform
import re
import sys
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any, Literal, TextIO

from wgsextract_cli import __version__

SCHEMA_ID = "wgsextract.result/1"
SCHEMA_RESOURCE = "assets/schemas/wgsextract.result.1.schema.json"
MANIFEST_SUFFIX = ".manifest.json"

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_MISSING_DEPENDENCY = 3

Scalar = str | int | float | bool | None
Status = Literal["ok", "failed", "cancelled"]
ErrorKind = Literal["failure", "usage", "missing_dependency", "cancelled"]

FINGERPRINT_PREFIX = "wgse-fp1:"
_FINGERPRINT_BLOCK = 1 << 20  # 1 MiB

# Longest suffixes first so ".vcf.gz" wins over ".gz".
_KINDS: tuple[tuple[str, str], ...] = (
    (".vcf.gz", "vcf"),
    (".vcf.bgz", "vcf"),
    (".fastq.gz", "fastq"),
    (".fq.gz", "fastq"),
    (".fasta.gz", "fasta"),
    (".fa.gz", "fasta"),
    (".fna.gz", "fasta"),
    (".bed.gz", "bed"),
    (".tsv.gz", "tsv"),
    (".csv.gz", "csv"),
    (".txt.gz", "txt"),
    (".bam", "bam"),
    (".cram", "cram"),
    (".sam", "sam"),
    (".vcf", "vcf"),
    (".bcf", "bcf"),
    (".gvcf", "vcf"),
    (".fasta", "fasta"),
    (".fa", "fasta"),
    (".fna", "fasta"),
    (".fastq", "fastq"),
    (".fq", "fastq"),
    (".bed", "bed"),
    (".tsv", "tsv"),
    (".csv", "csv"),
    (".txt", "txt"),
    (".json", "json"),
    (".html", "html"),
    (".pdf", "pdf"),
    (".zip", "zip"),
    (".gz", "gzip"),
)
_INDEX_SUFFIXES = {
    "bam": (".bai", ".csi"),
    "cram": (".crai",),
    "vcf": (".tbi", ".csi"),
    "bcf": (".csi",),
    "fasta": (".fai",),
}
_COMPRESSION_SUFFIXES = (".gz", ".bgz", ".zst", ".bz2", ".xz")
_SECRET_NAME = re.compile(r"token|secret|password|passwd|credential|api_?key", re.I)
_VERSION = re.compile(r"\d+(?:\.\d+)+[\w.+-]*")
# Launchers whose own version says nothing about the tool they run.
_LAUNCHERS = {"pixi", "env", "wsl", "conda", "mamba", "micromamba", "nice", "ionice"}


def _utc(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def file_kind(path: str | os.PathLike[str]) -> str:
    """The file type a manifest reports, from the name (or ``directory``)."""
    name = os.fspath(path).lower()
    if os.path.isdir(name) or os.path.isdir(os.fspath(path)):
        return "directory"
    for suffix, kind in _KINDS:
        if name.endswith(suffix):
            return kind
    return "other"


def fingerprint(path: str | os.PathLike[str]) -> str | None:
    """A cheap content fingerprint, ``wgse-fp1:<sha256 hex>``.

    Hashes the size and the first, middle and last MiB (the whole file when it
    is at most 3 MiB), so a 100 GB BAM costs three reads, not a full pass. It
    tells files apart and survives copies (mtime isn't hashed), but it isn't an
    integrity check: an edit confined to unread bytes keeps the fingerprint.
    Use ``wgsextract sign`` for full-content digests. None for non-files or
    unreadable files.
    """
    try:
        with open(path, "rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            digest = hashlib.sha256(b"wgse-fp1\0" + str(size).encode() + b"\0")
            if size <= 3 * _FINGERPRINT_BLOCK:
                digest.update(stream.read())
            else:
                for offset in (
                    0,
                    (size - _FINGERPRINT_BLOCK) // 2,
                    size - _FINGERPRINT_BLOCK,
                ):
                    stream.seek(offset)
                    digest.update(stream.read(_FINGERPRINT_BLOCK))
    except (OSError, ValueError):
        return None
    return FINGERPRINT_PREFIX + digest.hexdigest()


def _index_of(path: Path, kind: str) -> str | None:
    for suffix in _INDEX_SUFFIXES.get(kind, ()):
        for candidate in (Path(str(path) + suffix), path.with_suffix(suffix)):
            if candidate != path and candidate.is_file():
                return str(candidate)
    return None


def manifest_path_for(output: str | os.PathLike[str]) -> Path:
    """``<output-stem>.manifest.json``: ``snps.vcf.gz`` -> ``snps.manifest.json``."""
    path = Path(output)
    name = path.name
    if path.suffix.lower() in _COMPRESSION_SUFFIXES:
        name = name[: -len(path.suffix)]
    stem = Path(name).stem if Path(name).suffix else name
    return path.with_name(stem + MANIFEST_SUFFIX)


@dataclass
class FileRecord:
    """A file (or directory) a command read or wrote."""

    path: str
    kind: str
    role: str
    exists: bool = False
    size_bytes: int | None = None
    mtime: str | None = None
    fingerprint: str | None = None
    index: str | None = None
    metrics: dict[str, Scalar] = field(default_factory=dict)

    @classmethod
    def describe(
        cls,
        path: str | os.PathLike[str],
        role: str,
        *,
        kind: str | None = None,
        metrics: Mapping[str, Scalar] | None = None,
    ) -> FileRecord:
        absolute = Path(path).expanduser().absolute()
        record = cls(
            path=str(absolute),
            kind=kind or file_kind(absolute),
            role=role,
            metrics=dict(metrics or {}),
        )
        record.refresh()
        return record

    def refresh(self) -> None:
        """Re-read size, mtime, fingerprint and index from disk."""
        path = Path(self.path)
        try:
            stat = path.stat()
        except OSError:
            self.exists, self.size_bytes, self.mtime = False, None, None
            self.fingerprint = self.index = None
            return
        self.exists = True
        self.mtime = _utc(stat.st_mtime)
        if path.is_dir():
            self.kind = "directory"
            self.size_bytes = self.fingerprint = self.index = None
            return
        self.size_bytes = stat.st_size
        self.fingerprint = fingerprint(path)
        self.index = _index_of(path, self.kind)


@dataclass
class ResultWarning:
    """A condition the user should know about; ``code`` is stable for machines."""

    code: str
    message: str
    details: dict[str, Scalar] = field(default_factory=dict)


@dataclass
class ResultError:
    kind: ErrorKind
    message: str


@dataclass
class ReferenceRecord:
    path: str
    build: str | None = None
    fingerprint: str | None = None


@dataclass
class Provenance:
    version: str = __version__
    argv: list[str] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    tools: dict[str, str] = field(default_factory=dict)
    reference: ReferenceRecord | None = None
    platform: str = field(default_factory=_platform.platform)
    python: str = field(default_factory=_platform.python_version)
    started: str = field(default_factory=lambda: _utc(time.time()))
    finished: str = ""
    elapsed_s: float = 0.0


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, os.PathLike):
        return os.fspath(value)
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [_json_safe(v) for v in value]
        return sorted(items, key=str) if isinstance(value, (set, frozenset)) else items
    return str(value)


def params_from_args(args: argparse.Namespace) -> dict[str, Any]:
    """The effective options of a run, minus internals and secrets."""
    params: dict[str, Any] = {}
    for key, value in sorted(vars(args).items()):
        if key.startswith("_") or key in {"func", "parent_pid"} or callable(value):
            continue
        params[key] = "<redacted>" if _SECRET_NAME.search(key) else _json_safe(value)
    return params


def tools_in_command(command: Sequence[str]) -> set[str]:
    """Names of the external tools a command line runs (best effort)."""
    tools: set[str] = set()
    parts = list(command)
    # Skip launchers such as `pixi run -e env --manifest-path X tool ...`.
    while parts and Path(parts[0]).stem.lower() in _LAUNCHERS:
        parts.pop(0)
        if parts and parts[0] == "run":
            parts.pop(0)
        while parts and parts[0].startswith("-"):
            option = parts.pop(0)
            if "=" not in option and option in {
                "-e",
                "--environment",
                "--manifest-path",
                "-n",
            }:
                parts = parts[1:]
    if parts:
        tools.add(Path(parts[0]).stem)
    tools.update(
        tool
        for tool in (
            "bwa",
            "bwa-mem2",
            "samtools",
            "bcftools",
            "gatk",
            "minimap2",
            "samblaster",
        )
        if tool in command
    )
    if "org.broadinstitute.hellbender.Main" in command:
        tools.add("gatk")
    return {tool for tool in tools if tool}


def tool_version(tool: str) -> str | None:
    """The version number of an installed tool, or None when unknown."""
    if tool.lower().startswith("python"):
        return _platform.python_version()
    from wgsextract_cli.core.dependency_checks import get_tool_version

    try:
        text = get_tool_version(tool)
    except Exception:  # a version probe must never fail a run
        return None
    if not text or text.startswith("Error"):
        return None
    match = _VERSION.search(text)
    return match.group(0) if match else text.strip()[:80]


@dataclass
class CommandResult:
    """What one command read, wrote and found; serialised as wgsextract.result/1."""

    command: str
    status: Status = "ok"
    exit_code: int = EXIT_OK
    error: ResultError | None = None
    inputs: list[FileRecord] = field(default_factory=list)
    outputs: list[FileRecord] = field(default_factory=list)
    metrics: dict[str, Scalar] = field(default_factory=dict)
    warnings: list[ResultWarning] = field(default_factory=list)
    details: dict[str, Any] | None = None
    manifest: str | None = None
    provenance: Provenance = field(default_factory=Provenance)

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._tools: set[str] = set()
        self._manifest_stem: Path | None = None
        self._clock = time.monotonic()

    # --- recording -------------------------------------------------------

    def add_input(
        self, path: str | os.PathLike[str] | None, role: str = "input"
    ) -> None:
        if not path:
            return
        record = FileRecord.describe(path, role)
        with self._lock:
            if not any(r.path == record.path and r.role == role for r in self.inputs):
                self.inputs.append(record)

    def add_output(
        self,
        path: str | os.PathLike[str] | None,
        role: str,
        *,
        kind: str | None = None,
        metrics: Mapping[str, Scalar] | None = None,
    ) -> None:
        """Register an output; it is stat'ed and fingerprinted when finalised."""
        if not path:
            return
        absolute = str(Path(path).expanduser().absolute())
        with self._lock:
            for record in self.outputs:
                if record.path == absolute:
                    record.role = role
                    record.metrics.update(metrics or {})
                    return
            self.outputs.append(
                FileRecord(
                    path=absolute,
                    kind=kind or file_kind(absolute),
                    role=role,
                    metrics=dict(metrics or {}),
                )
            )

    def add_metrics(self, **metrics: Scalar) -> None:
        with self._lock:
            self.metrics.update(metrics)

    def warn(self, code: str, message: str, **details: Scalar) -> None:
        """Record a structured warning (without logging it; see ``warn``)."""
        with self._lock:
            self.warnings.append(ResultWarning(code, message, dict(details)))

    def set_details(self, details: Mapping[str, Any] | None) -> None:
        self.details = None if details is None else dict(details)

    def set_reference(
        self, path: str | os.PathLike[str] | None, build: str | None = None
    ) -> None:
        if not path:
            return
        absolute = Path(path).expanduser().absolute()
        self.provenance.reference = ReferenceRecord(
            path=str(absolute),
            build=build,
            fingerprint=fingerprint(absolute) if absolute.is_file() else None,
        )

    def set_manifest_stem(self, stem: str | os.PathLike[str]) -> None:
        """Name the manifest ``<stem>.manifest.json`` instead of after an output."""
        self._manifest_stem = Path(stem).expanduser().absolute()

    def record_tool_command(self, command: Sequence[str]) -> None:
        tools = tools_in_command(command)
        with self._lock:
            self._tools.update(tools)

    # --- finishing -------------------------------------------------------

    def fail(self, kind: ErrorKind, message: str, exit_code: int) -> None:
        self.status = "cancelled" if kind == "cancelled" else "failed"
        self.exit_code = exit_code
        self.error = ResultError(kind, message)

    def manifest_path(self) -> Path | None:
        if self._manifest_stem is not None:
            stem = self._manifest_stem
            return stem.with_name(stem.name + MANIFEST_SUFFIX)
        for record in self.outputs:
            if record.kind != "directory":
                return manifest_path_for(record.path)
        for record in self.outputs:
            return Path(record.path, Path(record.path).name + MANIFEST_SUFFIX)
        return None

    def finalize(self, *, probe_tools: bool = True) -> None:
        """Stat the outputs and fill in timing and tool versions."""
        for record in self.outputs:
            record.refresh()
        if self.provenance.reference is None:
            ref = self.provenance.params.get("ref")
            if isinstance(ref, str) and Path(ref).expanduser().is_file():
                self.set_reference(ref)
        if probe_tools:
            self.provenance.tools = {
                tool: version
                for tool in sorted(self._tools)
                if (version := tool_version(tool))
            }
        self.provenance.finished = _utc(time.time())
        self.provenance.elapsed_s = round(time.monotonic() - self._clock, 3)

    def to_dict(self) -> dict[str, Any]:
        document: dict[str, Any] = {"schema": SCHEMA_ID}
        for item in dataclasses.fields(self):
            document[item.name] = _json_safe_dataclass(getattr(self, item.name))
        return document

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2) + "\n"

    def write_manifest(self) -> Path | None:
        """Write the manifest atomically next to the outputs; None if it has none."""
        from wgsextract_cli.core.atomic_output import atomic_output

        path = self.manifest_path()
        if path is None or not path.parent.is_dir():
            return None
        self.manifest = str(path)
        with atomic_output(path, retire_indexes=False) as staged:
            Path(staged).write_text(self.to_json(), encoding="utf-8", newline="\n")
        return path


def _json_safe_dataclass(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _json_safe_dataclass(getattr(value, item.name))
            for item in dataclasses.fields(value)
        }
    if isinstance(value, list):
        return [_json_safe_dataclass(v) for v in value]
    return _json_safe(value)


def load_schema() -> dict[str, Any]:
    """The wgsextract.result/1 JSON Schema shipped with the package."""
    text = resources.files("wgsextract_cli").joinpath(SCHEMA_RESOURCE).read_text()
    schema: dict[str, Any] = json.loads(text)
    return schema


# --- the active result -----------------------------------------------------

# One command runs per process, and its worker threads must reach the same
# result, so this is a process global rather than a ContextVar.
_active: CommandResult | None = None
_json_mode = False


def current_result() -> CommandResult:
    """The result of the running command; a throwaway one outside dispatch."""
    return _active if _active is not None else CommandResult(command="")


def json_mode() -> bool:
    return _json_mode


@contextlib.contextmanager
def collecting(result: CommandResult, *, as_json: bool = False) -> Iterator[None]:
    global _active, _json_mode
    previous = (_active, _json_mode)
    _active, _json_mode = result, as_json
    try:
        yield
    finally:
        _active, _json_mode = previous


def record_tool_command(command: Sequence[str]) -> None:
    if _active is not None:
        _active.record_tool_command(command)


def warn(code: str, message: str, **details: Scalar) -> None:
    """Log a warning and record it on the running command's result."""
    logging.warning(message)
    current_result().warn(code, message, **details)


def human_output(*values: object, sep: str = " ", end: str = "\n") -> None:
    """print() for human-readable results; silent under --json."""
    if not _json_mode:
        print(*values, sep=sep, end=end)


@contextlib.contextmanager
def stdout_reserved() -> Iterator[TextIO]:
    """Send everything written to stdout, by Python or by child processes, to
    stderr; yield a stream that still reaches the real stdout."""
    sys.stdout.flush()
    try:
        fd = sys.stdout.fileno()
    except (AttributeError, OSError, ValueError):
        fd = None
    if fd is None:
        real = sys.stdout
        with contextlib.redirect_stdout(sys.stderr):
            yield real
        return
    sys.stderr.flush()
    saved = os.dup(fd)
    try:
        os.dup2(sys.stderr.fileno(), fd)
        real = open(saved, "w", encoding="utf-8", newline="\n", closefd=False)  # noqa: SIM115
        try:
            yield real
        finally:
            real.flush()
            sys.stdout.flush()
            os.dup2(saved, fd)
    finally:
        os.close(saved)
