"""Sample workspaces: what is known about one sample, recorded once.

A sample workspace is a genome folder: the folder of a ``--genome`` library
entry, or any folder holding ``wgsextract.sample.toml`` or
``genome-config.toml``. Its ``wgsextract.sample.toml`` (schema
``wgsextract.sample/1``) records

- ``[override]``: facts the user set (``wgsextract sample set``), which win
  over detection. A sex override is the person's, so it is folder-wide in a
  genome folder; elsewhere, and for the platform always, it belongs to one file
  (``[override."<file>"]``, under that file's fingerprint);
- ``[detected."<file>"]``: facts commands detected from each input file, with
  the method, confidence, detector version, command and time, under the file's
  fingerprint, so a replaced file's facts are ignored and re-detected;
- ``[outputs]``: the result manifests that ``--genome`` runs wrote there.

Commands ask ``known(fact, file)`` before detecting (override, else this
file's current detected fact) and ``record(...)`` what they detect. Where an
override changes an algorithm, ``resolve`` still detects the fact cheaply and
warns when the reads disagree with the override. Recording
is best effort: a read-only folder, a corrupt file or one written by a newer
release never fails a command, and such a file is never overwritten.
docs/sample-workspace.md describes the format.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import os
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import tomli_w

from wgsextract_cli import __version__
from wgsextract_cli.core.paths import PathInput, absolute_path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

SCHEMA_ID = "wgsextract.sample/1"
SCHEMA_RESOURCE = "assets/schemas/wgsextract.sample.1.schema.json"
WORKSPACE_FILE = "wgsextract.sample.toml"
_SCHEMA_PREFIX = "wgsextract.sample/"
_SCHEMA_VERSION = 1
_LOCK_TIMEOUT = 30
_HEADER = (
    "# WGS Extract sample workspace (wgsextract.sample/1); "
    "see docs/sample-workspace.md.\n"
    "# [override] holds your corrections and wins over detection: edit it here\n"
    "# or with 'wgsextract sample set'. wgsextract rewrites [detected] and\n"
    "# [outputs]; comments are not kept.\n\n"
)

SEXES = ("Male", "Female")
PLATFORMS = ("illumina", "ont", "pacbio", "ultima", "dnbseq")
_ALIASES = {
    "m": "Male",
    "f": "Female",
    "nanopore": "ont",
    "oxford nanopore": "ont",
    "hifi": "pacbio",
    "mgi": "dnbseq",
    "bgi": "dnbseq",
    "bgiseq": "dnbseq",
}

Source = Literal["override", "detected"]
Confidence = Literal["high", "medium", "low"]


@dataclass(frozen=True)
class FactSpec:
    """One kind of fact. ``choices`` makes it user-overridable. ``version`` is
    its detector's: bump it when detection changes so recorded facts are
    detected again."""

    name: str
    description: str
    choices: tuple[str, ...] | None = None
    version: int = 1

    @property
    def overridable(self) -> bool:
        return self.choices is not None

    def normalize(self, value: str) -> str:
        """The canonical spelling of a user's value; ValueError if not allowed."""
        assert self.choices is not None
        text = str(value).strip()
        lowered = _ALIASES.get(text.lower(), text).lower()
        for choice in self.choices:
            if choice.lower() == lowered:
                return choice
        raise ValueError(
            f"{self.name} must be one of {', '.join(self.choices)}, not {value!r}"
        )


FACTS: dict[str, FactSpec] = {
    spec.name: spec
    for spec in (
        FactSpec("sex", "Male or Female, from the chrX/chrY read counts.", SEXES, 2),
        FactSpec(
            "platform",
            "Sequencing platform, from the read groups or read names.",
            PLATFORMS,
        ),
        FactSpec("build", "Reference build the file is aligned to."),
        FactSpec("read_length", "Mean read length in bases."),
        # Version 2: long reads' depth is sampled, not counted (#427).
        FactSpec("mean_coverage", "Mean mapped read depth over the genome.", None, 2),
        FactSpec(
            "target_depths",
            "Mean mapped depth of the autosomes, chrX, chrY and mtDNA.",
            None,
            2,
        ),
    )
}
OVERRIDABLE = tuple(name for name, spec in FACTS.items() if spec.overridable)
# Facts of the person rather than of one file: overridden folder-wide in a
# genome folder, which holds one person's files.
PERSON_FACTS = ("sex",)


@dataclass(frozen=True)
class Fact:
    """A fact in effect: the user's override or a current detected value."""

    name: str
    value: Any
    source: Source
    file: str | None = None
    fingerprint: str | None = None
    method: str | None = None
    confidence: str | None = None
    version: int | None = None
    by: str | None = None
    at: str | None = None

    def describe(self) -> str:
        if self.source == "override":
            scope = f" for {self.file}" if self.file else ""
            return f"your override{scope} in {WORKSPACE_FILE}"
        detail = ", ".join(x for x in (self.method, self.confidence) if x)
        when = (self.at or "")[:10]
        return (
            f"recorded in {WORKSPACE_FILE} from {self.file}"
            + (f" by {self.by}" if self.by else "")
            + (f" on {when}" if when else "")
            + (f": {detail}" if detail else "")
        )

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class _Document:
    raw: dict[str, Any]
    exists: bool = False
    writable: bool = True
    problem: str | None = None


# --- fingerprints ------------------------------------------------------------


def file_fingerprint(path: PathInput) -> str | None:
    """``results.fingerprint`` of a regular file, computed once per revision."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return _fingerprint(str(absolute_path(path)), st.st_size, st.st_mtime_ns, st.st_ino)


@functools.lru_cache(maxsize=256)
def _fingerprint(path: str, size: int, mtime_ns: int, inode: int) -> str | None:
    from wgsextract_cli.core.results import fingerprint

    return fingerprint(path)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _by() -> str:
    from wgsextract_cli.core.results import current_result

    command = current_result().command
    return f"wgsextract {__version__}" + (f" {command}" if command else "")


# --- the workspace -------------------------------------------------------------

_warned: set[str] = set()
_unwritable: set[Path] = set()


def _warn_once(message: str) -> None:
    if message not in _warned:
        _warned.add(message)
        logging.warning(message)


class SampleWorkspace:
    """One sample folder and its ``wgsextract.sample.toml``."""

    def __init__(self, folder: PathInput) -> None:
        self.folder = absolute_path(folder)
        self.path = self.folder / WORKSPACE_FILE

    def __repr__(self) -> str:
        return f"SampleWorkspace({str(self.folder)!r})"

    def key(self, file: PathInput) -> str:
        """How ``file`` is named in [detected]: relative to the folder, '/'
        separated, or absolute when it lies elsewhere."""
        path = absolute_path(file)
        try:
            return path.relative_to(self.folder).as_posix()
        except ValueError:
            return str(path)

    def file_path(self, key: str) -> Path:
        path = Path(key)
        return path if path.is_absolute() else self.folder / path

    # --- reading ---------------------------------------------------------------

    def _read(self) -> _Document:
        try:
            with open(self.path, "rb") as stream:
                raw = tomllib.load(stream)
        except FileNotFoundError:
            return _Document({"schema": SCHEMA_ID})
        except OSError as e:
            return _Document({}, True, False, f"unreadable: {e}")
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
            return _Document({}, True, False, f"not valid TOML: {e}")
        schema = raw.get("schema")
        if schema == SCHEMA_ID:
            malformed = _malformed(raw)
            if malformed:
                return _Document({}, True, False, malformed)
            return _Document(raw, True)
        version = None
        if isinstance(schema, str) and schema.startswith(_SCHEMA_PREFIX):
            with contextlib.suppress(ValueError):
                version = int(schema.removeprefix(_SCHEMA_PREFIX))
        if schema is None or (version is not None and version < _SCHEMA_VERSION):
            # An older layout: keep the user's overrides and other keys, but
            # detect everything again.
            raw.pop("detected", None)
            raw["schema"] = SCHEMA_ID
            return _Document(raw, True, True, f"older schema {schema!r}; upgraded")
        if version is not None:
            return _Document(
                {}, True, False, f"written by a newer wgsextract ({schema})"
            )
        return _Document({}, True, False, f"unknown schema {schema!r}")

    def _load(self) -> _Document:
        doc = self._read()
        if doc.problem and not doc.writable:
            _warn_once(
                f"Ignoring {self.path}: {doc.problem}. wgsextract won't change it; "
                "fix or delete it to record sample facts again."
            )
        return doc

    @property
    def is_genome_folder(self) -> bool:
        """Whether the folder holds one person's files: the --genome folder, a
        genome-library entry, or a folder with genome-config.toml."""
        from wgsextract_cli.core.config import settings
        from wgsextract_cli.core.genome_library import GENOME_CONFIG_NAME

        if _active is not None and _active.folder == self.folder:
            return True
        if (self.folder / GENOME_CONFIG_NAME).is_file():
            return True
        library = settings.get("genome_library")
        if not library:
            return False
        root = absolute_path(str(library))
        return self.folder != root and self.folder.is_relative_to(root)

    def _valid(self, name: str, value: Any) -> str | None:
        spec = FACTS.get(name)
        if spec is None or not spec.overridable:
            _warn_once(f"{self.path}: ignoring unknown override {name!r}.")
            return None
        try:
            return spec.normalize(value)
        except ValueError as e:
            _warn_once(f"{self.path}: ignoring override: {e}.")
            return None

    def folder_overrides(self, doc: _Document | None = None) -> dict[str, str]:
        """The folder-wide overrides in effect: person facts, in a genome
        folder. Others are reported once and ignored."""
        table = (doc or self._load()).raw.get("override")
        if not isinstance(table, dict):
            return {}
        found: dict[str, str] = {}
        for name, value in table.items():
            if isinstance(value, dict):
                continue
            valid = self._valid(name, value)
            if valid is None:
                continue
            if name not in PERSON_FACTS:
                _warn_once(
                    f"{self.path}: ignoring the folder-wide {name} override; a "
                    f"{name} belongs to one file: 'wgsextract sample set --input "
                    f"FILE --{name} {valid}'."
                )
            elif not self.is_genome_folder:
                _warn_once(
                    f"{self.path}: ignoring the folder-wide {name} override: "
                    f"{self.folder} isn't a genome folder, so it may hold several "
                    f"people's files. Set it per file: 'wgsextract sample set "
                    f"--input FILE --{name} {valid}'."
                )
            else:
                found[name] = valid
        return found

    def file_overrides(
        self, file: PathInput, doc: _Document | None = None
    ) -> dict[str, str]:
        """``file``'s own overrides, while it is the revision they were set on."""
        table = (doc or self._load()).raw.get("override")
        entry = table.get(self.key(file)) if isinstance(table, dict) else None
        if not isinstance(entry, dict):
            return {}
        if entry.get("fingerprint") != file_fingerprint(file):
            _warn_once(
                f"{self.path}: ignoring the overrides of {self.key(file)}: the file "
                "changed since they were set. Set them again if they still hold."
            )
            return {}
        return {
            name: valid
            for name, value in entry.items()
            if name != "fingerprint" and (valid := self._valid(name, value))
        }

    def overrides(
        self, file: PathInput | None = None, doc: _Document | None = None
    ) -> dict[str, str]:
        """The overrides in effect for ``file`` (folder-wide ones without it)."""
        doc = doc or self._load()
        found = self.folder_overrides(doc)
        if file is not None:
            found.update(self.file_overrides(file, doc))
        return found

    def detected(
        self,
        name: str,
        file: PathInput,
        *,
        doc: _Document | None = None,
    ) -> Fact | None:
        """The fact detected from this very file (same fingerprint) by the
        current detector version."""
        doc = doc or self._load()
        key = self.key(file)
        entry = (doc.raw.get("detected") or {}).get(key)
        if not isinstance(entry, dict):
            return None
        fact = entry.get(name)
        if not isinstance(fact, dict) or "value" not in fact:
            return None
        spec = FACTS.get(name)
        if spec is None or fact.get("version") != spec.version:
            return None
        current = file_fingerprint(file)
        if current is None or entry.get("fingerprint") != current:
            return None
        return _detected_fact(name, key, entry["fingerprint"], fact)

    def fact(self, name: str, file: PathInput | None) -> Fact | None:
        """The fact in effect: the override, else ``file``'s current detection."""
        doc = self._load()
        if file is not None:
            own = self.file_overrides(file, doc).get(name)
            if own is not None:
                return Fact(name, own, "override", file=self.key(file))
        shared = self.folder_overrides(doc).get(name)
        if shared is not None:
            return Fact(name, shared, "override")
        return self.detected(name, file, doc=doc) if file else None

    # --- writing ---------------------------------------------------------------

    def _update(
        self, mutate: Callable[[dict[str, Any]], bool], *, strict: bool = False
    ) -> bool:
        """Apply ``mutate`` to the document under the workspace lock; write it
        if it reports a change. Best effort unless ``strict``."""
        from wgsextract_cli.core.atomic_output import atomic_write
        from wgsextract_cli.core.file_lock import exclusive_lock
        from wgsextract_cli.core.utils import WGSExtractError

        if not strict and self.folder in _unwritable:
            return False
        try:
            with exclusive_lock(self.folder / f".{WORKSPACE_FILE}", _LOCK_TIMEOUT):
                doc = self._load()
                if not doc.writable:
                    if strict:
                        raise WGSExtractError(
                            f"Not changing {self.path}: {doc.problem}. Fix or "
                            "delete it first."
                        )
                    return False
                if not mutate(doc.raw):
                    return False
                doc.raw["schema"] = SCHEMA_ID
                atomic_write(self.path, render(doc.raw).encode("utf-8"))
                return True
        except (OSError, WGSExtractError) as e:
            if strict:
                raise WGSExtractError(f"Couldn't update {self.path}: {e}") from e
            _unwritable.add(self.folder)
            logging.debug(f"Not recording sample facts in {self.path}: {e}")
            return False

    def record(
        self,
        name: str,
        file: PathInput,
        value: Any,
        *,
        method: str,
        confidence: Confidence,
    ) -> bool:
        """Record a detected fact for this revision of ``file``."""
        fingerprint = file_fingerprint(file)
        if fingerprint is None or value is None:
            return False
        key = self.key(file)
        new = {
            "value": value,
            "method": method,
            "confidence": confidence,
            "version": FACTS[name].version,
            "by": _by(),
            "at": _now(),
        }

        def mutate(raw: dict[str, Any]) -> bool:
            detected = raw.get("detected")
            if not isinstance(detected, dict):
                detected = raw["detected"] = {}
            entry = detected.get(key)
            if not isinstance(entry, dict) or entry.get("fingerprint") != fingerprint:
                # A replaced file's facts all go.
                entry = detected[key] = {"fingerprint": fingerprint}
            old = entry.get(name)
            # The first method to find a value keeps the credit; rewriting it
            # whenever another method agrees would churn the file.
            if isinstance(old, dict) and all(
                old.get(k) == new[k] for k in ("value", "version")
            ):
                return False
            entry[name] = new
            return True

        return self._update(mutate)

    def set_overrides(
        self, values: Mapping[str, str | None], file: PathInput | None = None
    ) -> dict[str, str]:
        """Set (or, for None, remove) overrides: a person fact folder-wide in a
        genome folder, anything else for ``file`` alone. Returns the overrides
        now in effect for ``file``. Raises on an invalid name or value, a
        missing file, or an unwritable workspace."""
        from wgsextract_cli.core.utils import UsageError

        changes: dict[str, str | None] = {}
        for name, value in values.items():
            spec = FACTS.get(name)
            if spec is None or not spec.overridable:
                raise UsageError(
                    f"{name!r} can't be overridden; choose from "
                    f"{', '.join(OVERRIDABLE)}."
                )
            try:
                changes[name] = None if value is None else spec.normalize(value)
            except ValueError as e:
                raise UsageError(f"{e}.") from None
        shared = {
            name
            for name, value in changes.items()
            if value is not None and name in PERSON_FACTS and self.is_genome_folder
        }
        per_file = {n for n, v in changes.items() if v is not None} - shared
        fingerprint = file_fingerprint(file) if file is not None else None
        if per_file and fingerprint is None:
            name = sorted(per_file)[0]
            why = (
                f"a {name} override belongs to one file"
                if name not in PERSON_FACTS
                else f"{self.folder} isn't a genome folder, so a {name} override "
                "belongs to one file"
            )
            raise UsageError(
                f"Give the file with --input: {why}."
                if file is None
                else f"Can't read {file} to tie the override to it."
            )
        key = self.key(file) if file is not None else None

        def mutate(raw: dict[str, Any]) -> bool:
            table = raw.get("override")
            if not isinstance(table, dict):
                table = raw["override"] = {}
            before = {
                k: dict(v) if isinstance(v, dict) else v for k, v in table.items()
            }
            entry = table.get(key) if key else None
            if (
                key
                and per_file
                and (
                    not isinstance(entry, dict)
                    or entry.get("fingerprint") != fingerprint
                )
            ):
                entry = table[key] = {"fingerprint": fingerprint}
            for name, value in changes.items():
                if name in shared:
                    table[name] = value
                elif name in per_file:
                    assert isinstance(entry, dict)
                    entry[name] = value
                else:
                    table.pop(name, None)
                    if isinstance(entry, dict):
                        entry.pop(name, None)
            if isinstance(entry, dict) and set(entry) <= {"fingerprint"}:
                table.pop(key, None)
            if not table:
                raw.pop("override")
            return table != before or not self.path.exists()

        self._update(mutate, strict=True)
        return self.overrides(file)

    def record_output(self, manifest: PathInput, command: str, status: str) -> bool:
        """Index a result manifest written in this workspace."""
        key = self.key(manifest)
        entry = {"command": command, "status": status, "at": _now()}

        def mutate(raw: dict[str, Any]) -> bool:
            outputs = raw.get("outputs")
            if not isinstance(outputs, dict):
                outputs = raw["outputs"] = {}
            for old in [k for k in outputs if not self.file_path(k).exists()]:
                del outputs[old]
            outputs[key] = entry
            return True

        return self._update(mutate)

    # --- reporting -------------------------------------------------------------

    def _status(self, key: str, entry: Mapping[str, Any]) -> str:
        """Whether the file ``entry`` describes is unchanged, changed or gone."""
        current = file_fingerprint(self.file_path(key))
        if current is None:
            return "missing"
        return "current" if current == entry.get("fingerprint") else "changed"

    def describe(self, file: PathInput | None = None) -> dict[str, Any]:
        """The workspace as JSON-ready data, for ``sample show`` and the API."""
        doc = self._load()
        files: dict[str, Any] = {}
        for key, entry in (doc.raw.get("detected") or {}).items():
            if not isinstance(entry, dict):
                continue
            files[key] = {
                "status": self._status(key, entry),
                "fingerprint": entry.get("fingerprint"),
                "facts": {
                    name: _detected_fact(
                        name, key, entry.get("fingerprint"), fact
                    ).to_dict()
                    for name, fact in entry.items()
                    if isinstance(fact, dict) and "value" in fact
                },
            }
        for key, entry in (doc.raw.get("override") or {}).items():
            if not isinstance(entry, dict):
                continue
            listed = files.setdefault(key, {"facts": {}})
            listed["overrides"] = {
                "status": self._status(key, entry),
                "fingerprint": entry.get("fingerprint"),
                "values": {k: v for k, v in entry.items() if k != "fingerprint"},
            }
            listed.setdefault("status", listed["overrides"]["status"])
        data: dict[str, Any] = {
            "schema": SCHEMA_ID,
            "workspace": str(self.folder),
            "path": str(self.path),
            "exists": doc.exists,
            "problem": doc.problem,
            "genome_folder": self.is_genome_folder,
            "overrides": self.folder_overrides(doc),
            "files": files,
            "outputs": doc.raw.get("outputs") or {},
        }
        if file is not None:
            data["file"] = self.key(file)
            data["effective"] = {
                name: fact.to_dict()
                for name in FACTS
                if (fact := self.fact(name, file)) is not None
            }
        return data


def _malformed(raw: Mapping[str, Any]) -> str | None:
    """What makes a current-schema document unusable, if anything."""
    for section in ("override", "detected", "outputs"):
        table = raw.get(section, {})
        if not isinstance(table, dict):
            return f"[{section}] isn't a table"
        if section == "override" and not all(
            isinstance(v, str) or (isinstance(v, dict) and "fingerprint" in v)
            for v in table.values()
        ):
            return "an entry of [override] is neither a value nor a file's table"
        if section != "override" and not all(
            isinstance(entry, dict) for entry in table.values()
        ):
            return f"an entry of [{section}] isn't a table"
    return None


def _detected_fact(
    name: str, key: str, fingerprint: str | None, fact: Mapping[str, Any]
) -> Fact:
    version = fact.get("version")
    return Fact(
        name=name,
        value=fact["value"],
        source="detected",
        file=key,
        fingerprint=fingerprint,
        method=_text(fact.get("method")),
        confidence=_text(fact.get("confidence")),
        version=version if isinstance(version, int) else None,
        by=_text(fact.get("by")),
        at=_text(fact.get("at")),
    )


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def render(raw: Mapping[str, Any]) -> str:
    """The TOML text: schema first, then [override], [detected], [outputs]."""
    order = ("schema", "override", "detected", "outputs")
    ordered = {k: raw[k] for k in order if k in raw}
    ordered.update({k: v for k, v in raw.items() if k not in order})
    text: str = tomli_w.dumps(ordered)
    return _HEADER + text


def load_schema() -> dict[str, Any]:
    """The wgsextract.sample/1 JSON Schema shipped with the package."""
    import json
    from importlib import resources

    text = resources.files("wgsextract_cli").joinpath(SCHEMA_RESOURCE).read_text()
    schema: dict[str, Any] = json.loads(text)
    return schema


# --- finding the workspace of a file -------------------------------------------

_active: SampleWorkspace | None = None
_active_files: frozenset[Path] = frozenset()


@contextlib.contextmanager
def active_workspace(
    folder: PathInput | None, files: Iterable[PathInput | None] = ()
) -> Iterator[None]:
    """Make ``folder`` (a --genome folder) the workspace of the files in it and
    of ``files`` (its inputs, which genome-config.toml may place elsewhere)."""
    global _active, _active_files
    previous = (_active, _active_files)
    if folder is not None:
        _active = SampleWorkspace(folder)
        _active_files = frozenset(absolute_path(f) for f in files if f)
    try:
        yield
    finally:
        _active, _active_files = previous


def current_workspace() -> SampleWorkspace | None:
    """The --genome workspace of the running command, if any."""
    return _active


def workspace_for(file: PathInput | None) -> SampleWorkspace | None:
    """The workspace ``file`` belongs to: the --genome folder for its files and
    inputs, else the file's own folder when that is a sample or genome folder."""
    if not file:
        return None
    path = absolute_path(file)
    if _active is not None and (
        path in _active_files or path.is_relative_to(_active.folder)
    ):
        return _active
    from wgsextract_cli.core.genome_library import GENOME_CONFIG_NAME

    folder = path.parent
    if (folder / WORKSPACE_FILE).is_file() or (folder / GENOME_CONFIG_NAME).is_file():
        return SampleWorkspace(folder)
    return None


_LABELS = {
    "sex": "Sex",
    "platform": "Platform",
    "build": "Build",
    "read_length": "Read length",
    "mean_coverage": "Mean coverage",
    "target_depths": "Target depths",
}
_reported: set[tuple[str, str]] = set()


def known(name: str, file: PathInput | None) -> Fact | None:
    """The fact in effect for ``file``, if its workspace has one: the user's
    override, else one detected from this revision of the file by the current
    detector. Logged once per run."""
    workspace = workspace_for(file)
    if workspace is None or file is None:
        return None
    try:
        fact = workspace.fact(name, file)
    except (OSError, ValueError) as e:
        logging.debug(f"Couldn't read sample facts from {workspace.path}: {e}")
        return None
    if fact is not None and (name, workspace.key(file)) not in _reported:
        _reported.add((name, workspace.key(file)))
        logging.info(f"{_LABELS.get(name, name)}: {fact.value} ({fact.describe()})")
    return fact


def record(
    name: str,
    file: PathInput | None,
    value: Any,
    *,
    method: str,
    confidence: Confidence,
) -> bool:
    """Record a detected fact in ``file``'s workspace, if it has one, and warn
    if it contradicts the user's override."""
    workspace = workspace_for(file)
    if workspace is None or file is None:
        return False
    recorded = workspace.record(name, file, value, method=method, confidence=confidence)
    check_override(name, file, value, method)
    return recorded


_checked: set[tuple[str, str]] = set()


def check_override(
    name: str, file: PathInput | None, detected: Any, method: str | None
) -> None:
    """Warn, into the result manifest, when the reads contradict the override
    in effect for ``file``: it changes the algorithm, so a wrong one must not
    go unnoticed. Once per fact and file per run."""
    workspace = workspace_for(file)
    if workspace is None or file is None or detected in (None, "Unknown", "unknown"):
        return
    try:
        override = workspace.overrides(file).get(name)
    except (OSError, ValueError):
        return
    key = (name, str(absolute_path(file)))
    if override is None or override == detected or key in _checked:
        return
    _checked.add(key)
    from wgsextract_cli.core.results import warn

    how = f" ({method})" if method else ""
    warn(
        f"{name}_override_differs",
        f"Using your {name} override {override} for {Path(file).name}, but its "
        f"reads look {detected}{how}. If the override is wrong, remove it with "
        f"'wgsextract sample unset --{name}'.",
        file=workspace.key(file),
        detected=str(detected),
        override=override,
    )


def resolve(
    name: str, file: PathInput | None, detect: Callable[[], tuple[Any, str | None]]
) -> Any:
    """The value of a fact that changes an algorithm: the recorded or
    detected one, or the user's override. With an override the fact is still
    detected (``detect`` returns (value, method) and records it) or read from
    the record, so a contradicting override is warned about."""
    fact = known(name, file)
    if fact is not None and fact.source == "detected":
        return fact.value
    workspace = workspace_for(file) if fact is not None else None
    recorded = workspace.detected(name, file) if workspace and file else None
    value, method = (recorded.value, recorded.method) if recorded else detect()
    if fact is None:
        return value
    check_override(name, file, value, method)
    return fact.value
