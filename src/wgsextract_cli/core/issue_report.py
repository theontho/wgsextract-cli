"""Pre-filled GitHub bug reports, redacted on this computer before anyone sees them.

A report describes this installation (versions, OS, CPU, RAM, tools, settings)
and, when there is one, a run: its command line, exit status, error, warnings
and log tail. It never contains genotypes, reads, file contents, sample names,
user or host names, full paths or secrets: every string taken from the run or
the settings goes through ``Redactor`` first, and the result's ``details`` are
left out entirely. Nothing is sent anywhere; the report becomes a GitHub
new-issue link that the user reviews and submits in their own browser.
"""

from __future__ import annotations

import getpass
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import urllib.parse
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from wgsextract_cli import __version__
from wgsextract_cli.core.tool_catalog import MANDATORY_TOOLS, OPTIONAL_TOOLS
from wgsextract_cli.core.utils import UsageError

ISSUES_URL = "https://github.com/theontho/wgsextract-cli/issues/new"
# .github/ISSUE_TEMPLATE/bug_report.yml; its field ids are the query keys below.
TEMPLATE = "bug_report.yml"
DESCRIPTION_FIELD = "what-happened"
DIAGNOSTICS_FIELD = "diagnostics"
LABELS = ("bug",)
# Browsers and GitHub accept longer links, but ~8 KB is safe everywhere.
MAX_URL_LENGTH = 8000
DEFAULT_LOG_LINES = 40

# Metrics safe to share: what kind of data it is, never what it says about
# the person (sex, haplogroups and calls are left out).
_SHAREABLE_METRICS = re.compile(
    r"^(reference_model|sequencer|avg_read_len|is_paired|avg_insert_size|size_gb"
    r"|sorted|indexed|duplicates_marked|duplicate_pct|alt_aware)$"
    r"|coverage|depth|read_len"
)

# --- redaction ---------------------------------------------------------------

# Extensions of files whose names are usually a sample or person's name.
_DATA_EXTENSIONS = tuple(
    sorted(
        {
            base + gz
            for base in (
                ".bam .cram .sam .ubam .bai .crai .csi .vcf .gvcf .g.vcf .bcf .fastq "
                ".fq .fa .fasta .fna .bed .txt .csv .tsv .json .zip .log .tbi .bw "
                ".bigwig .html .pdf .md"
            ).split()
            for gz in ("", ".gz", ".bgz", ".zst", ".bz2", ".xz")
        },
        key=len,
        reverse=True,
    )
)
# Other file and folder names are shown only when they are programs.
_PROGRAM_EXTENSIONS = (".py", ".sh", ".exe", ".bat", ".dll", ".so", ".dylib", ".jar")
_SAFE_PROGRAMS = frozenset(
    name.lower()
    for name in (
        *MANDATORY_TOOLS,
        *OPTIONAL_TOOLS,
        "python",
        "pixi",
        "docker",
        "podman",
        "wsl",
        "conda",
        "perl",
        "bash",
        "wgsextract",
        "bin",
    )
)
_PATH_CHARS = r"[^\s'\"`<>|;,()\[\]{}]"
_PATH = re.compile(
    rf"(?<![\w.~:/\\>-])(?:[A-Za-z]:[\\/]|\\\\|~[\\/]|/(?=[\w.~-])){_PATH_CHARS}*"
)
_QUOTED_PATH = re.compile(r"""(['"])((?:[A-Za-z]:[\\/]|\\\\|~[\\/]|/)[^'"\r\n]*)\1""")
_BARE_FILE = re.compile(
    r"(?<![\w<>/\\.~-])\w[\w.-]*?(?:"
    + "|".join(re.escape(e) for e in _DATA_EXTENSIONS)
    + r")(?![\w.-])",
    re.IGNORECASE,
)
# Relative paths: reduced when they have 2+ separators or end in a data file.
_RELATIVE_PATH = re.compile(r"(?<![\w<>/\\.~:@-])[\w.-]+(?:[\\/][\w.-]+)+[\\/]?")
_URL = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s<>'\"`]+")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_SECRET_WORD = r"[\w-]*(?:token|secret|password|passwd|credential|api[_-]?key)[\w-]*"
_SECRET_ASSIGNMENT = re.compile(
    rf"(?i)(--?{_SECRET_WORD}(?:=|\s+)|\b{_SECRET_WORD}\s*[=:]\s*)(['\"]?)[^\s'\"]+\2"
)
_KNOWN_TOKENS = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w{20,}|hf_[A-Za-z0-9]{20,}"
    r"|AKIA[0-9A-Z]{16}|xox[abprs]-[\w-]{10,})\b|(?i:\bbearer\s+)[\w.~+/-]{8,}=*"
)
_READ_NAMES = re.compile(
    # Illumina, SRA/ENA reads, MGI/BGI, PacBio and Nanopore (UUID) read names.
    r"\b[\w-]+:\d+:[\w-]+:\d+:\d+:\d+:\d+\b"
    r"|\b[SDE]RR\d{5,}\.\d+(?:\.\d+)?\b"
    r"|\b[A-Z]\d{8,10}L\dC\d{3}R\d{3}\d+\b"
    r"|\bm\d{5,}[eUs]?_\d{6}_\d{6}/\d+(?:/\w+)?"
    r"|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
)
_VARIANT_LINE = re.compile(
    r"^\s*(?:chr)?(?:\d{1,2}|[XYM]|MT)[\s:]+\d+\s+\S+\s+[ACGTN]+\s+[ACGTN.,*<>]"
    r"|^\s*(?:rs|i)\d+\s+\S+\s+\d+\s+[ACGTDI-]{1,2}\s*$"
    r"|^[ACGTN]{30,}$",
    re.IGNORECASE,
)
_PLACEHOLDER = re.compile(r"<[a-z][a-z -]*>")


def _word(token: str) -> re.Pattern[str]:
    """``token`` as a whole word, case-insensitively, outside ``<placeholders>``."""
    return re.compile(rf"(?<![A-Za-z0-9<]){re.escape(token)}(?![A-Za-z0-9>])", re.I)


def file_stems(path: str) -> set[str]:
    """Name parts of a file that may identify a person: ``John_Doe.hg38.cram``
    gives ``John_Doe.hg38`` and ``John_Doe``."""
    name = re.split(r"[\\/]", path.rstrip("\\/"))[-1]
    stems = {name.split(".", 1)[0]}
    for extension in _DATA_EXTENSIONS:
        if name.lower().endswith(extension):
            stems.add(name[: -len(extension)])
            break
    return {s for s in stems if len(s) >= 3 and not s.isdigit()}


@dataclass
class Redactor:
    """Removes what could identify a person or leak a secret from text.

    ``sensitive`` strings (sample and file stems) and ``users``/``hosts``
    names are replaced wherever they appear; paths keep only a non-identifying
    file name (``<path>/samtools``, ``<path>/<file>.cram``); ``safe_names``
    (reference genome stems) may stay in file names. ``counts`` says what was
    removed, by kind."""

    home: str | None = None
    users: Sequence[str] = ()
    hosts: Sequence[str] = ()
    sensitive: Iterable[str] = ()
    safe_names: Iterable[str] = ()
    counts: Counter[str] = field(default_factory=Counter)

    def __post_init__(self) -> None:
        # Longest first, so "John_Doe.hg38" goes before "John_Doe".
        def words(values: Iterable[str], minimum: int) -> list[re.Pattern[str]]:
            unique = {v for v in values if v and len(v) >= minimum}
            return [_word(v) for v in sorted(unique, key=len, reverse=True)]

        self._sensitive = words(self.sensitive, 3)
        self._users = words(self.users, 3)
        self._hosts = words(self.hosts, 3)
        self._all_users = {u.lower() for u in self.users if u}
        self._safe = {s.lower() for s in self.safe_names if s}

    @classmethod
    def for_this_computer(
        cls, *, sensitive: Iterable[str] = (), safe_names: Iterable[str] = ()
    ) -> Redactor:
        home = str(Path.home())
        users = {Path(home).name}
        try:
            users.add(getpass.getuser())
        except (KeyError, OSError):  # no user database entry (some containers)
            pass
        node = platform.node()
        hosts = {node, node.split(".", 1)[0]} if node else set()
        return cls(
            home=home,
            users=sorted(users),
            hosts=sorted(hosts),
            sensitive=sensitive,
            safe_names=safe_names,
        )

    def _sub(self, pattern: re.Pattern[str], repl: str, text: str, kind: str) -> str:
        text, n = pattern.subn(repl, text)
        self.counts[kind] += n
        return text

    def _names(self, text: str) -> str:
        for pattern in self._sensitive:
            text = self._sub(pattern, "<sample>", text, "sample or file name")
        for pattern in self._users:
            text = self._sub(pattern, "<user>", text, "user name")
        for pattern in self._hosts:
            text = self._sub(pattern, "<host>", text, "computer name")
        return text

    def _file_name(self, name: str) -> str:
        if not name or _PLACEHOLDER.fullmatch(name):
            return name
        if name.lower() in self._all_users:
            self.counts["user name"] += 1
            return "<user>"
        lower = name.lower()
        for extension in _DATA_EXTENSIONS:
            if lower.endswith(extension) and len(name) > len(extension):
                if name[: -len(extension)].lower() in self._safe:
                    return name
                self.counts["file name"] += 1
                return "<file>" + name[-len(extension) :]
        if lower in _SAFE_PROGRAMS or lower.endswith(_PROGRAM_EXTENSIONS):
            return self._names(name)
        self.counts["file name"] += 1
        return "<name>"

    def path(self, value: str) -> str:
        """One path, reduced to ``<path>/`` and a non-identifying file name.

        Paths inside this package or a Python environment keep their place
        there (``wgsextract_cli/core/x.py``), which is what a traceback needs."""
        trimmed = value.rstrip(".:")
        tail = value[len(trimmed) :]
        normal = trimmed.replace("\\", "/")
        for anchor in ("site-packages/", "wgsextract_cli/"):
            index = normal.find(anchor)
            if index >= 0:
                self.counts["folder"] += 1
                return f"<{anchor.rstrip('/')}>/{normal[index + len(anchor) :]}{tail}"
        name = normal.rstrip("/").rsplit("/", 1)[-1]
        if name in ("", "~") or re.fullmatch(r"[A-Za-z]:", name):
            name = ""
        self.counts["folder"] += 1
        reduced = "<path>/" + self._file_name(name) if name else "<path>"
        return reduced + tail

    def _paths(self, text: str) -> str:
        text = _QUOTED_PATH.sub(lambda m: m[1] + self.path(m[2]) + m[1], text)
        text = _PATH.sub(lambda m: self.path(m[0]), text)
        text = _RELATIVE_PATH.sub(self._relative, text)
        return _BARE_FILE.sub(lambda m: self._file_name(m[0]), text)

    def _relative(self, match: re.Match[str]) -> str:
        value = match[0]
        separators = len(re.findall(r"[\\/]", value.rstrip("\\/")))
        if separators >= 2 or value.lower().endswith(_DATA_EXTENSIONS):
            return self.path(value)
        return value

    def _line(self, line: str) -> str:
        if line.count("\t") >= 3 or _VARIANT_LINE.search(line):
            self.counts["data line"] += 1
            return "<data line removed>"
        line = self._sub(_KNOWN_TOKENS, "<secret>", line, "secret")
        line = _SECRET_ASSIGNMENT.sub(self._secret, line)
        pieces: list[str] = []
        last = 0
        for match in _URL.finditer(line):
            pieces += [self._local(line[last : match.start()]), self._url(match[0])]
            last = match.end()
        line = "".join(pieces) + self._local(line[last:])
        line = self._sub(_READ_NAMES, "<read>", line, "read name")
        return self._names(line)

    def _local(self, text: str) -> str:
        """Text outside URLs: emails, the home folder and paths."""
        text = self._sub(_EMAIL, "<email>", text, "email address")
        if self.home:
            home = re.compile(re.escape(self.home), re.I if os.name == "nt" else 0)
            text = self._sub(home, "~", text, "home folder")
        return self._paths(text)

    def _secret(self, match: re.Match[str]) -> str:
        self.counts["secret"] += 1
        return f"{match[1]}{match[2]}<redacted>{match[2]}"

    def _url(self, url: str) -> str:
        from wgsextract_cli.core.download_progress import redact_urls

        clean = redact_urls(url)
        if clean != url:
            self.counts["URL credentials or query"] += 1
        return clean

    def text(self, value: object) -> str:
        """Any text: a message, a log line, a setting."""
        return "\n".join(self._line(line) for line in str(value).splitlines())

    def argv(self, argv: Sequence[str]) -> list[str]:
        from wgsextract_cli.core.results import redact_argv

        cleaned = redact_argv(list(argv))
        self.counts["secret"] += sum(arg.endswith("<redacted>") for arg in cleaned)
        return [self.text(arg) for arg in cleaned]

    def summary(self) -> dict[str, int]:
        return {kind: n for kind, n in sorted(self.counts.items()) if n}


# --- the run being reported --------------------------------------------------


@dataclass
class RunRecord:
    """The run a report is about, as read from the local files."""

    source: str  # "last run", "last failed run", or the kind of --run file
    manifest: dict[str, Any]
    log_tail: list[str] = field(default_factory=list)

    @property
    def command(self) -> str:
        return str(self.manifest.get("command") or "wgsextract")

    @property
    def failed(self) -> bool:
        return bool(self.manifest.get("exit_code"))


def load_run(path: Path | None, *, last_failed: bool = False) -> RunRecord | None:
    """The run to report on: ``path`` (a result manifest, a run log or its
    ``.provenance.json``) or else the last (failed) run this computer recorded."""
    from wgsextract_cli.core.last_run import load_last_run

    if path is None:
        document = load_last_run(failed=last_failed)
        if document is None:
            return None
        source = "last failed run" if last_failed else "last run"
        return RunRecord(source, document["result"], _log_tail_of(document))
    path = Path(path).expanduser()
    if not path.is_file():
        raise UsageError(f"--run: no such file: {path}")
    name = path.name
    if name.endswith(".log"):
        provenance = Path(str(path) + ".provenance.json")
        manifest = (
            _from_provenance(_read_json(provenance)) if provenance.is_file() else {}
        )
        return RunRecord("run log (--run)", manifest, _tail_lines(path))
    document = _read_json(path)
    if name.endswith(".provenance.json"):
        log = Path(str(path)[: -len(".provenance.json")])
        tail = _tail_lines(log) if log.is_file() else []
        return RunRecord("run log provenance (--run)", _from_provenance(document), tail)
    schema = document.get("schema")
    if schema == "wgsextract.last-run/1":
        return RunRecord(
            "recorded run (--run)", document["result"], _log_tail_of(document)
        )
    if schema == "wgsextract.result/1":
        return RunRecord("result manifest (--run)", document)
    raise UsageError(
        f"--run: {name} is not a result manifest (.manifest.json), run log (.log) "
        "or run provenance file (.provenance.json)."
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise UsageError(f"--run: cannot read {path.name}: {e}") from e
    if not isinstance(document, dict):
        raise UsageError(f"--run: {path.name} is not a JSON object.")
    return document


def _log_tail_of(document: Mapping[str, Any]) -> list[str]:
    log_file = document.get("log_file")
    if log_file and Path(log_file).is_file():
        return _tail_lines(Path(log_file))
    return [str(line) for line in document.get("log_tail") or []]


def _tail_lines(path: Path, limit: int = 400) -> list[str]:
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 256 * 1024))
            data = stream.read()
    except OSError:
        return []
    return data.decode("utf-8", "replace").splitlines()[-limit:]


# Options naming files or samples, in run-log argv, by params key.
_INPUT_OPTIONS = {
    "--input": "input",
    "-i": "input",
    "--vcf-input": "vcf_input",
    "--r1": "r1",
    "--r2": "r2",
    "--genome": "genome",
    "--mother-vcf": "mother_vcf",
    "--father-vcf": "father_vcf",
}


def _from_provenance(document: Mapping[str, Any]) -> dict[str, Any]:
    """A run log's provenance as a (partial) result manifest."""
    from wgsextract_cli.commands.registry import COMMAND_TIERS

    argv = [str(a) for a in document.get("argv") or []]
    commands = {tier.name for tier in COMMAND_TIERS}
    words: list[str] = []
    params: dict[str, str] = {}
    for i, arg in enumerate(argv):
        option = arg.split("=", 1)[0]
        value = arg.split("=", 1)[1] if "=" in arg else (argv[i + 1 : i + 2] or [""])[0]
        if option in _INPUT_OPTIONS:
            params[_INPUT_OPTIONS[option]] = value
        if not words and arg in commands:
            words.append(arg)
        elif len(words) == 1 and re.fullmatch(r"[a-z][a-z0-9-]*", arg):
            words.append(arg)
        elif words:
            words.append("")
    status = document.get("status")
    return {
        "command": " ".join(w for w in words[:2] if w) or "wgsextract",
        "status": "failed" if status == "failed" else status or "unknown",
        "exit_code": 1 if status == "failed" else 0,
        "error": None,
        "inputs": [],
        "outputs": [],
        "metrics": {},
        "warnings": [],
        "provenance": {
            "version": document.get("wgsextract_version"),
            "argv": argv,
            "params": params,
            "tools": document.get("tool_versions") or {},
            "reference": document.get("reference"),
            "started": document.get("started_at"),
            "finished": document.get("finished_at"),
        },
    }


def catalog_reference_names() -> set[str]:
    """Stems of the reference genomes in the catalog (``hs38DH``, ``chm13v2.0``):
    file names that are safe to show. A user's own reference names are not."""
    import csv
    from importlib import resources

    names: set[str] = set()
    try:
        text = (
            resources.files("wgsextract_cli")
            .joinpath("assets/reference/seed_genomes.csv")
            .read_text(encoding="utf-8")
        )
    except OSError:
        return names
    for row in csv.DictReader(text.splitlines()):
        for column in ("Final File Name", "Initial File Name"):
            names |= file_stems(row.get(column) or "")
    return names


def _sensitive_names(manifest: Mapping[str, Any]) -> tuple[set[str], set[str]]:
    """Stems that may name a person (inputs, outputs, --genome), and catalog
    reference stems that are safe to show."""
    sensitive: set[str] = set()
    for record in [*(manifest.get("inputs") or []), *(manifest.get("outputs") or [])]:
        if isinstance(record, Mapping) and record.get("kind") != "directory":
            sensitive |= file_stems(str(record.get("path") or ""))
    provenance = manifest.get("provenance") or {}
    params = provenance.get("params") or {}
    for key in ("input", "vcf_input", "r1", "r2", "mother_vcf", "father_vcf"):
        if isinstance(params.get(key), str):
            sensitive |= file_stems(params[key])
    for key in ("genome", "sample", "sample_name"):
        if isinstance(params.get(key), str):
            sensitive.add(params[key])
    safe = catalog_reference_names()
    return sensitive - safe, safe


# --- this computer -----------------------------------------------------------


def _gib(n: float) -> str:
    return f"{n / 1024**3:.1f} GiB"


def _cpu_model() -> str:
    try:
        if sys.platform.startswith("linux"):
            for line in Path("/proc/cpuinfo").read_text(errors="replace").splitlines():
                if line.lower().startswith(("model name", "hardware", "cpu model")):
                    return line.split(":", 1)[1].strip()
        elif sys.platform == "darwin":
            completed = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=5,
                check=False,
            )
            if completed.stdout.strip():
                return completed.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return platform.processor() or "unknown"


def _install_type() -> str:
    from wgsextract_cli.core.runtime import source_checkout_root

    root = source_checkout_root()
    if root is not None:
        try:
            commit = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=5,
                check=False,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            commit = ""
        return f"source checkout, commit {commit}" if commit else "source checkout"
    prefix = Path(sys.prefix).parts
    if ".pixi" in prefix:
        return "Pixi environment (installer)"
    if "conda" in sys.version.lower() or Path(sys.prefix, "conda-meta").is_dir():
        return "conda environment"
    return "Python package"


def _os_text() -> str:
    system = platform.system()
    if system == "Darwin":
        text = f"macOS {platform.mac_ver()[0]}"
    elif system == "Windows":
        text = f"Windows {platform.release()} ({platform.version()})"
    else:
        text = f"{system} {platform.release()}"
        if "microsoft" in platform.release().lower():
            text += " (WSL)"
        try:
            for line in Path("/etc/os-release").read_text().splitlines():
                if line.startswith("PRETTY_NAME="):
                    text += f", {line.split('=', 1)[1].strip(chr(34))}"
        except OSError:
            pass
    return f"{text}, {platform.machine()}"


def environment_lines() -> list[str]:
    """This installation and computer, without names or paths."""
    import psutil

    memory = psutil.virtual_memory()
    logical = psutil.cpu_count() or os.cpu_count() or 0
    physical = psutil.cpu_count(logical=False)
    cores = f"{logical} threads" + (f", {physical} cores" if physical else "")
    lines = [
        f"- wgsextract: {__version__} ({_install_type()})",
        f"- OS: {_os_text()}",
        f"- Python: {platform.python_version()} ({platform.python_implementation()})",
        f"- CPU: {_cpu_model()}, {cores}",
        f"- RAM: {_gib(memory.total)} total, {_gib(memory.available)} available",
    ]
    from wgsextract_cli.core.config import settings

    places = {"current folder": Path.cwd()}
    for key, label in (
        ("output_directory", "output_directory"),
        ("temp_directory", "temp_directory"),
    ):
        if settings.get(key):
            places[label] = Path(str(settings[key])).expanduser()
    free = []
    for label, target in places.items():
        while not target.exists() and target != target.parent:
            target = target.parent
        try:
            free.append(f"{_gib(shutil.disk_usage(target).free)} ({label})")
        except OSError:
            continue
    if free:
        lines.append("- Free disk: " + ", ".join(free))
    return lines


def tool_versions(extra: Iterable[str] = ()) -> dict[str, str]:
    """Versions of the mandatory tools and ``extra`` (those a run used)."""
    from wgsextract_cli.core.results import tool_version
    from wgsextract_cli.core.tool_catalog import required_dependency_tools

    versions: dict[str, str] = {}
    for tool in dict.fromkeys([*required_dependency_tools(False), *extra]):
        version = tool_version(tool)
        versions[tool] = version or "not found"
    return versions


def settings_lines(redactor: Redactor) -> list[str]:
    """The config.toml settings that are set, redacted; secrets never shown."""
    from wgsextract_cli.core.config import KNOWN_SETTINGS, get_config_path, settings
    from wgsextract_cli.core.results import _SECRET_NAME

    lines = []
    for key in sorted(settings):
        value = settings[key]
        if value is None or KNOWN_SETTINGS.get(key, (object(),))[0] == value:
            continue
        if _SECRET_NAME.search(key) or key not in KNOWN_SETTINGS:
            shown = "<set>"
        elif isinstance(value, str):
            shown = redactor.text(value)
        else:
            shown = json.dumps(value)
        lines.append(f"- {key} = {shown}")
    exists = get_config_path().is_file()
    head = "config.toml" if exists else "no config.toml (defaults)"
    return [f"- {head}; settings that differ from the defaults:"] + (
        [f"  {line}" for line in lines] if lines else ["  - none"]
    )


# --- the report --------------------------------------------------------------


@dataclass(frozen=True)
class IssueReport:
    """A redacted bug report, ready to show the user and open on GitHub.

    ``diagnostics`` is the full redacted text, saved locally by
    ``save``; ``link_diagnostics`` is what fits in ``url`` (see
    ``truncated``). Edit ``title``, ``description`` or ``link_diagnostics``
    with ``dataclasses.replace`` and call ``issue_url`` for a new link."""

    title: str
    description: str
    diagnostics: str
    link_diagnostics: str
    url: str
    labels: tuple[str, ...] = LABELS
    source: str = "none"
    redactions: Mapping[str, int] = field(default_factory=dict)
    truncated: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    file_name: str = ""

    @property
    def body(self) -> str:
        """The whole issue as Markdown, as saved in the diagnostics file."""
        description = self.description or (
            "_Describe what you did, what you expected and what happened instead._"
        )
        return (
            f"# {self.title}\n\n## What happened?\n\n{description}\n\n"
            f"## Steps to reproduce\n\n{STEPS_PLACEHOLDER}\n\n"
            f"## Diagnostics\n\n{self.diagnostics}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "description": self.description,
            "labels": list(self.labels),
            "source": self.source,
            "redactions": dict(self.redactions),
            "truncated": list(self.truncated),
            "notes": list(self.notes),
            "url": self.url,
            "url_length": len(self.url),
            "diagnostics": self.diagnostics,
            "link_diagnostics": self.link_diagnostics,
            "file_name": self.file_name,
        }

    def save(self, directory: Path) -> Path:
        """Write ``body`` to ``directory/file_name`` (readable only by the user)."""
        from wgsextract_cli.core.atomic_output import atomic_write

        directory = Path(directory).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / self.file_name
        atomic_write(path, self.body.encode("utf-8"))
        return path


STEPS_PLACEHOLDER = (
    "1. Run `wgsextract ...`\n2. ...\n\nIf you can, reproduce it on public or "
    "fake data (`wgsextract example-genome list`, `wgsextract qc fake-data`) "
    "so the steps can be shared."
)


def issue_url(report: IssueReport, *, max_length: int = MAX_URL_LENGTH) -> str:
    """The GitHub new-issue link for ``report``'s title, description and
    ``link_diagnostics``. If it would exceed ``max_length``, the diagnostics are
    cut at a line, then the description and the title are shortened."""
    lines = report.link_diagnostics.splitlines()
    title, description = report.title, report.description
    cut = False
    while True:
        diagnostics = "\n".join(lines)
        if cut:
            if diagnostics.count("```") % 2:
                diagnostics += "\n```"
            diagnostics += (
                "\n\n_(cut to fit the link; paste the rest from the saved report)_"
            )
        url = _url(title, description, diagnostics, report.labels)
        if len(url) <= max_length:
            return url
        if lines:
            lines, cut = lines[: len(lines) - max(1, len(lines) // 8)], True
        elif len(description) > 100:
            description = description[: len(description) * 3 // 4].rstrip() + " [...]"
        elif len(title) > 40:
            title = title[: len(title) * 3 // 4].rstrip() + "..."
        else:
            return url


def _url(title: str, description: str, diagnostics: str, labels: Sequence[str]) -> str:
    query = {"template": TEMPLATE, "labels": ",".join(labels), "title": title}
    if description:
        query[DESCRIPTION_FIELD] = description
    query[DIAGNOSTICS_FIELD] = diagnostics
    return f"{ISSUES_URL}?{urllib.parse.urlencode(query, quote_via=urllib.parse.quote)}"


@dataclass
class _Sections:
    run: list[str]
    metrics: list[str]
    warnings: list[str]
    log: list[str]
    environment: list[str]
    tools: list[str]
    settings: list[str]

    def render(
        self,
        *,
        log_lines: int,
        warnings: int | None = None,
        tools: bool = True,
        settings: bool = True,
        note: str = "",
    ) -> str:
        parts: list[str] = []
        if self.run:
            parts += ["### Run", "", *self.run, *self.metrics]
            shown = self.warnings if warnings is None else self.warnings[:warnings]
            if shown:
                parts += ["- Warnings:", *shown]
            if len(shown) < len(self.warnings):
                parts.append(f"  - ... {len(self.warnings) - len(shown)} more")
            parts.append("")
        if self.log and log_lines:
            tail = self.log[-log_lines:]
            parts += [f"### Log (last {len(tail)} lines, redacted)", "", "```text"]
            parts += [line.replace("```", "'''") for line in tail] + ["```", ""]
        parts += ["### Environment", "", *self.environment, ""]
        if tools:
            parts += ["### Tools", "", *self.tools, ""]
        if settings:
            parts += ["### Settings", "", *self.settings, ""]
        if note:
            parts += [note, ""]
        return "\n".join(parts).rstrip() + "\n"


def _human_size(n: object) -> str:
    if not isinstance(n, (int, float)):
        return "size unknown"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return ""


def _run_sections(run: RunRecord, redactor: Redactor) -> tuple[list[str], ...]:
    manifest = run.manifest
    provenance = manifest.get("provenance") or {}
    argv = redactor.argv([str(a) for a in provenance.get("argv") or []])
    lines = [f"- Report on: {run.source}"]
    if argv:
        lines.append(f"- Command: `wgsextract {' '.join(argv)}`")
    else:
        lines.append(f"- Command: `wgsextract {redactor.text(run.command)}`")
    status = manifest.get("status", "unknown")
    error = manifest.get("error") or {}
    kind = f", {error['kind']}" if error.get("kind") else ""
    lines.append(f"- Result: {status} (exit code {manifest.get('exit_code')}{kind})")
    if error.get("message"):
        message = redactor.text(error["message"]).strip().replace("\n", " ")
        lines.append(f"- Error: {message}")
    if provenance.get("version") and provenance["version"] != __version__:
        lines.append(f"- Run with wgsextract {provenance['version']}")
    if provenance.get("elapsed_s") is not None:
        lines.append(f"- Elapsed: {provenance['elapsed_s']} s")
    reference = provenance.get("reference") or {}
    if reference.get("path") or reference.get("build"):
        parts = [str(reference["build"])] if reference.get("build") else []
        if reference.get("path"):
            name = redactor.path(str(reference["path"])).removeprefix("<path>/")
            parts.append(f"`{name}`")
        lines.append(f"- Reference: {', '.join(parts)}")
    for label, records in (
        ("Inputs", manifest.get("inputs")),
        ("Outputs", manifest.get("outputs")),
    ):
        described = [
            f"{r.get('role', '?')}: {r.get('kind', '?')}, {_human_size(r.get('size_bytes'))}"
            for r in records or []
            if isinstance(r, Mapping)
        ]
        if described:
            lines.append(f"- {label}: " + "; ".join(described))
    metrics = [
        f"  - {key}: {redactor.text(f'{value:.4g}' if isinstance(value, float) else value)}"
        for key, value in (manifest.get("metrics") or {}).items()
        if _SHAREABLE_METRICS.search(key) and value is not None
    ]
    if metrics:
        metrics.insert(0, "- Input metrics:")
    warnings = [
        f"  - `{w.get('code')}`: {redactor.text(w.get('message', '')).strip()}"
        for w in manifest.get("warnings") or []
        if isinstance(w, Mapping)
    ]
    return lines, metrics, warnings


def _default_title(run: RunRecord | None, redactor: Redactor) -> str:
    if run is None:
        return f"Problem with wgsextract {__version__}"
    error = (run.manifest.get("error") or {}).get("message")
    if run.failed and error:
        first = redactor.text(str(error).strip().splitlines()[0])
        title = f"{run.command}: {first}"
    elif run.failed:
        title = f"{run.command} failed (exit code {run.manifest.get('exit_code')})"
    else:
        title = f"Problem with '{run.command}'"
    return title if len(title) <= 100 else title[:97] + "..."


def build_report(
    *,
    run: RunRecord | None,
    title: str | None = None,
    description: str | None = None,
    log_lines: int = DEFAULT_LOG_LINES,
    max_url_length: int = MAX_URL_LENGTH,
    probe_tools: Callable[[Iterable[str]], Mapping[str, str]] | None = None,
    now: datetime | None = None,
) -> IssueReport:
    """Collect, redact and fit a report; see ``api.support.build_issue_report``."""
    if log_lines < 0:
        raise UsageError("--log-lines must be 0 or more.")
    now = now or datetime.now(timezone.utc)
    manifest = run.manifest if run else {}
    sensitive, safe = _sensitive_names(manifest)
    redactor = Redactor.for_this_computer(sensitive=sensitive, safe_names=safe)

    run_lines: list[str] = []
    metrics: list[str] = []
    warnings: list[str] = []
    log: list[str] = []
    if run is not None:
        run_lines, metrics, warnings = _run_sections(run, redactor)
        log = redactor.text("\n".join(run.log_tail)).splitlines()
    used = (manifest.get("provenance") or {}).get("tools") or {}
    logging.debug("Checking tool versions for the report...")
    versions = dict((probe_tools or tool_versions)(used))
    tools = [f"- {name}: {redactor.text(v)}" for name, v in versions.items()]
    sections = _Sections(
        run=run_lines,
        metrics=metrics,
        warnings=warnings,
        log=log,
        environment=[redactor.text(line) for line in environment_lines()],
        tools=tools,
        settings=settings_lines(redactor),
    )
    title = redactor.text(title).strip() if title else _default_title(run, redactor)
    description = redactor.text(description).strip() if description else ""
    file_name = f"issue-report-{now.strftime('%Y%m%dT%H%M%SZ')}.md"
    diagnostics = sections.render(log_lines=log_lines)

    notes: list[str] = []
    if run is None:
        notes.append(
            "No recorded run to report on; the report describes this installation only."
        )
    elif run.source == "last run" and not run.failed:
        notes.append(
            "The last run succeeded. To report the last failed run instead, "
            "use --last-failed."
        )

    report = IssueReport(
        title=title,
        description=description,
        diagnostics=diagnostics,
        link_diagnostics=diagnostics,
        url="",
        source=run.source if run else "none",
        notes=tuple(notes),
        file_name=file_name,
    )
    report = _fit(report, sections, log_lines, max_url_length)
    return replace(report, redactions=redactor.summary())


def _fit(
    report: IssueReport, sections: _Sections, log_lines: int, max_length: int
) -> IssueReport:
    """Shorten the link's diagnostics until the link fits, least useful first."""
    log_steps = [log_lines]
    while log_steps[-1] > 5:
        log_steps.append(log_steps[-1] // 2)
    log_steps.append(0)
    shown_log = min(log_lines, len(sections.log))
    plans: list[dict[str, Any]] = [{"log_lines": n} for n in log_steps]
    plans += [{"log_lines": 0, "warnings": n} for n in (10, 3, 0)]
    plans += [
        {"log_lines": 0, "warnings": 0, "settings": False},
        {"log_lines": 0, "warnings": 0, "settings": False, "tools": False},
    ]
    for plan in plans:
        cuts = _describe_cuts(plan, sections, shown_log)
        note = (
            "> Shortened to fit in a GitHub link: "
            + "; ".join(cuts)
            + f". The full report is in `{report.file_name}`, saved on your "
            "computer; paste from it if it helps."
            if cuts
            else ""
        )
        text = sections.render(note=note, **plan)
        candidate = replace(report, link_diagnostics=text, truncated=tuple(cuts))
        url = _url(candidate.title, candidate.description, text, candidate.labels)
        if len(url) <= max_length:
            return replace(candidate, url=url)
    candidate = replace(
        candidate, truncated=(*candidate.truncated, "diagnostics cut at a line")
    )
    return replace(candidate, url=issue_url(candidate, max_length=max_length))


def _describe_cuts(
    plan: Mapping[str, Any], sections: _Sections, shown: int
) -> list[str]:
    cuts: list[str] = []
    kept = min(plan["log_lines"], len(sections.log))
    if kept < shown:
        cuts.append(
            f"log tail cut from {shown} to {kept} lines"
            if kept
            else "log tail left out"
        )
    if plan.get("warnings") is not None and plan["warnings"] < len(sections.warnings):
        cuts.append(
            f"{len(sections.warnings) - plan['warnings']} of "
            f"{len(sections.warnings)} warnings left out"
        )
    if plan.get("settings") is False:
        cuts.append("settings left out")
    if plan.get("tools") is False:
        cuts.append("tool versions left out")
    return cuts


def open_in_browser(url: str) -> bool:
    """Open ``url`` in the user's browser; False if no browser could be started."""
    import webbrowser

    try:
        return bool(webbrowser.open(url, new=2))
    except webbrowser.Error:
        return False
