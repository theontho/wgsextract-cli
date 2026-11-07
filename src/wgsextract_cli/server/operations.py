"""The operations the server offers: every function of the typed API that takes
a ``Params`` and returns a ``Result``, with request checking and the folders
requests may name."""

from __future__ import annotations

import functools
import inspect
import ntpath
import os
import re
import typing
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Literal

from wgsextract_cli import api
from wgsextract_cli.api._params import (
    UNSET,
    ParamField,
    Params,
    json_schema,
    param_fields,
)
from wgsextract_cli.core.results import CommandResult
from wgsextract_cli.core.utils import UsageError

Kind = Literal["query", "job"]

# Quick reads that describe a sample or the reference library; everything else
# makes new files, takes longer or changes what is recorded about a sample, so
# MCP offers it only with --allow-jobs.
QUERIES = frozenset(
    {
        "info",
        "info chip",
        "sample show",
        "ref status",
        "ref library-list",
        "example-genome list",
        "deps check",
    }
)


# What follows the contig of an interval (chr1:1-100, X:1,000-): a drive
# letter followed by it on Windows is still a region, not a file path.
_INTERVAL_END = re.compile(r"\d[\d,]*(-[\d,]*)?")


class RequestError(Exception):
    """A request the server refuses: ``status`` is its HTTP status and
    ``code`` a stable machine-readable reason."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code

    def to_dict(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": str(self)}}


@dataclass(frozen=True)
class Operation:
    """One API function: ``name`` is its CLI command path ("vcf/snp")."""

    name: str
    params: type[Params]
    run: Callable[..., CommandResult]
    kind: Kind

    @property
    def command(self) -> str:
        return self.params.COMMAND

    @property
    def tool_name(self) -> str:
        """The MCP tool name: ``vcf_snp``."""
        return self.name.replace("/", "_").replace("-", "_")

    @property
    def summary(self) -> str:
        doc = inspect.getdoc(self.params) or ""
        return " ".join(doc.split("\n\n")[0].split()).replace("``", "`")

    def fields(self, *, read_only: bool = False) -> tuple[ParamField, ...]:
        """The fields a request may set; ``read_only`` leaves out those that
        make the operation save files."""
        return tuple(
            f
            for f in param_fields(self.params)
            if not f.spec.local_only and not (read_only and f.spec.writes)
        )

    def schema(self, *, read_only: bool = False) -> dict[str, Any]:
        """The JSON Schema of a request's parameters."""
        schema = json_schema(self.params)
        schema.pop("$schema", None)
        allowed = {f.name for f in self.fields(read_only=read_only)}
        schema["properties"] = {
            k: v for k, v in schema["properties"].items() if k in allowed
        }
        schema["required"] = [k for k in schema["required"] if k in allowed]
        return schema

    def read_only_values(self) -> dict[str, Any]:
        """What a read-only server sets, so the operation saves nothing."""
        return {
            f.name: f.safe_value
            for f in param_fields(self.params)
            if f.spec.safe_value is not UNSET
        }

    @property
    def deletes(self) -> bool:
        """Whether the operation always deletes data it didn't make."""
        return self.params.DELETES

    @property
    def may_delete(self) -> bool:
        """Whether some request to it deletes data it didn't make."""
        return self.deletes or any(f.spec.deletes for f in self.fields())

    def describe(self) -> dict[str, Any]:
        out: dict[str, Any] = {"op": self.name, "kind": self.kind}
        if self.may_delete:
            out["destructive"] = True
        return {**out, "summary": self.summary}


def _group_modules() -> list[ModuleType]:
    return [
        value
        for name in api.__all__
        if isinstance(value := getattr(api, name), ModuleType)
    ]


@functools.cache
def operations() -> dict[str, Operation]:
    """Every operation, by name, in the API's order."""
    found: dict[str, Operation] = {}
    for module in _group_modules():
        for name, function in vars(module).items():
            if (
                name.startswith("_")
                or not inspect.isfunction(function)
                or function.__module__ != module.__name__
            ):
                continue
            hints = typing.get_type_hints(function)
            cls = hints.get("params")
            if not (
                isinstance(cls, type)
                and issubclass(cls, Params)
                and hints.get("return") is CommandResult
                and not cls.LOCAL_ONLY
            ):
                continue
            op_name = cls.COMMAND.replace(" ", "/")
            kind: Kind = "query" if cls.COMMAND in QUERIES else "job"
            found[op_name] = Operation(op_name, cls, function, kind)
    return found


def find(name: str) -> Operation:
    op = operations().get(name.strip("/"))
    if op is None:
        raise RequestError(404, "unknown_op", f"No operation {name!r}; see /v1/ops.")
    return op


def find_tool(tool_name: str) -> Operation | None:
    return next((o for o in operations().values() if o.tool_name == tool_name), None)


def params_from_json(op: Operation, data: object, *, read_only: bool = False) -> Params:
    """Build ``op``'s Params from a JSON object, checking names and types as
    the JSON Schema says. Raises ``RequestError`` for a bad request (400) or,
    when ``read_only``, one that would save files (403)."""
    if not isinstance(data, Mapping):
        raise RequestError(400, "usage", "The parameters must be a JSON object.")
    fields = {f.name: f for f in param_fields(op.params)}
    missing = [f.name for f in op.fields() if f.required and f.name not in data]
    if missing:
        raise RequestError(
            400, "usage", f"Missing required parameter(s): {', '.join(missing)}."
        )
    values: dict[str, Any] = {}
    for key, value in data.items():
        item = fields.get(key)
        if item is None:
            known = ", ".join(f.name for f in op.fields())
            raise RequestError(
                400, "usage", f"Unknown parameter {key!r}; {op.name} takes: {known}."
            )
        if item.spec.local_only:
            raise RequestError(
                400,
                "usage",
                f"{key!r} can't be set by a server request; set it in config.toml "
                "or on the command line.",
            )
        if (
            read_only
            and item.spec.writes
            and value is not None
            and value != item.safe_value
        ):
            raise RequestError(
                403,
                "read_only",
                f"{key!r} saves files, which this read-only server doesn't do "
                "(serve --mcp --allow-jobs allows it).",
            )
        values[key] = _coerce(item, value)
    try:
        return op.params(**values)
    except UsageError as e:
        raise RequestError(400, "usage", str(e)) from None


def _coerce(item: ParamField, value: object) -> object:
    if value is None:
        if type(None) not in typing.get_args(item.type):
            raise _type_error(item, "set (it can't be null)")
        return None
    base = item.base_type
    if typing.get_origin(base) is tuple:
        element = typing.get_args(base)[0]
        if element is float:
            if not (isinstance(value, list) and all(_is_number(v) for v in value)):
                raise _type_error(item, "an array of numbers")
            return tuple(float(v) for v in value)
        if not (isinstance(value, list) and all(isinstance(v, str) for v in value)):
            raise _type_error(item, "an array of strings")
        return tuple(_expand(item, v) for v in value)
    if item.spec.path or base is Path or base is str:
        if not isinstance(value, str):
            raise _type_error(item, "a string")
        return _expand(item, value)
    if base is bool:
        if not isinstance(value, bool):
            raise _type_error(item, "true or false")
        return value
    if base is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise _type_error(item, "an integer")
        return value
    if base is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _type_error(item, "a number")
        return float(value)
    return value


def expand_paths(op: Operation, data: Mapping[str, Any]) -> dict[str, Any]:
    """A request with ``~`` expanded in its path fields, as the operation
    will read them, for code that works on the request (job locks)."""
    fields = {f.name: f for f in param_fields(op.params)}
    expanded = dict(data)
    for key, value in data.items():
        item = fields.get(key)
        if item is None:
            continue
        if isinstance(value, str):
            expanded[key] = _expand(item, value)
        elif isinstance(value, list):
            expanded[key] = [
                _expand(item, v) if isinstance(v, str) else v for v in value
            ]
    return expanded


def _expand(item: ParamField, value: str) -> str:
    """A path field's ``~`` as the operation will read it, so the path checks
    see the folder it names."""
    return os.path.expanduser(value) if item.spec.path else value


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _type_error(item: ParamField, expected: str) -> RequestError:
    return RequestError(400, "usage", f"{item.name!r} must be {expected}.")


def is_network_path(text: str) -> bool:
    """Whether Windows would reach ``text`` over the network or as a device:
    a UNC (``\\\\host\\share``), ``\\\\?\\`` or ``\\\\.\\`` path, with either slash.
    Opening one sends this user's NTLM credentials to the host, so it is
    refused on every OS before anything touches the filesystem."""
    head = text[:4].replace("/", "\\")
    return (
        head.startswith("\\\\")
        or head == "\\??\\"
        or ntpath.splitdrive(text)[0].startswith("\\\\")
    )


class PathGuard:
    """What requests may do: the folders they may name, and whether they may
    delete data an operation didn't make (``allow_destructive``). A path is
    allowed when it is one of the folders or inside one, both as written and
    with symbolic links resolved.

    ``pathmod`` is the path flavour (``os.path``); everything before the
    final ``realpath`` is lexical, so a refused path is never opened.
    """

    def __init__(
        self,
        roots: Iterable[str | os.PathLike[str]],
        *,
        allow_destructive: bool = False,
        pathmod: Any = os.path,
    ) -> None:
        self.allow_destructive = allow_destructive
        self.pathmod = pathmod
        unique: dict[str, None] = {}
        lexical: dict[str, None] = {}
        for root in roots:
            text = pathmod.expanduser(os.fspath(root))
            unique[pathmod.realpath(text)] = None
            lexical[self._lexical(text)] = None
        self.roots = tuple(Path(r) for r in unique)
        self._real_roots = tuple(unique)
        # A root named through a symbolic link is allowed as written, too.
        self._lexical_roots = tuple({**lexical, **unique})

    def _lexical(self, text: str) -> str:
        return str(self.pathmod.normpath(self.pathmod.abspath(text)))

    def _inside(self, path: str, roots: tuple[str, ...]) -> bool:
        case, sep = self.pathmod.normcase, self.pathmod.sep
        path = case(path)
        for root in map(case, roots):
            if path == root or path.startswith(root.rstrip(sep) + sep):
                return True
        return False

    def allows(self, path: str | os.PathLike[str]) -> bool:
        """Whether a request may name ``path``."""
        try:
            self.check_path("path", path)
        except RequestError:
            return False
        return True

    def check_path(self, name: str, value: str | os.PathLike[str]) -> None:
        text = os.fspath(value)
        refused = RequestError(
            403,
            "path_not_allowed",
            f"{name!r}: {text} is outside the folders this server may use "
            "(serve --allow DIR, or config server_allowed_paths).",
        )
        text = self.pathmod.expanduser(text)
        if is_network_path(os.fspath(value)) or is_network_path(text):
            raise refused
        if not self.pathmod.isabs(text):
            raise RequestError(
                400, "usage", f"{name!r} must be an absolute path, not {text!r}."
            )
        if not self._inside(self._lexical(text), self._lexical_roots):
            raise refused
        if not self._inside(self.pathmod.realpath(text), self._real_roots):
            raise refused

    def check(self, params: Params) -> None:
        if params.deletes_data() and not self.allow_destructive:
            raise RequestError(
                403,
                "destructive",
                f"{params.COMMAND} would delete data; the server does that only "
                "when started with serve --allow-destructive.",
            )
        for item in param_fields(type(params)):
            value = getattr(params, item.name)
            for path in value if isinstance(value, tuple) else (value,):
                if isinstance(path, Path):
                    self.check_path(item.name, path)
                elif isinstance(path, str) and item.spec.splits:
                    for one in item.spec.splits(params, path):
                        self._check_text(item.name, one)
                elif isinstance(path, str) and item.spec.may_name_file:
                    self._check_text(item.name, path)
                elif isinstance(path, str) and item.spec.expression and "@" in path:
                    raise RequestError(
                        403,
                        "path_not_allowed",
                        f"{item.name!r}: a server request can't read a file "
                        "with an @FILE operand.",
                    )
        for name, path in params.implied_paths():
            self.check_path(name, path)

    def _check_text(self, name: str, text: str) -> None:
        """A text value that names a file when one exists there (a BED file
        given as a region): written like a path, it must be an allowed one;
        a bare name must not be a file in the server's working folder.
        On Windows an interval such as ``X:1-100`` counts as a bare name, so
        it is refused only when a file of that name exists."""
        pathmod = self.pathmod
        if text.startswith("~"):
            # The command would use it literally: a `~` folder here.
            raise RequestError(
                400, "usage", f"{name!r}: name a file by its absolute path, not ~."
            )
        drive, rest = pathmod.splitdrive(text)
        if (
            "/" in text
            or "\\" in text
            or pathmod.isabs(text)
            # D:evil.bam is in D's working folder; X:1-100 is an interval.
            or (drive and not _INTERVAL_END.fullmatch(rest))
            or is_network_path(text)
        ):
            self.check_path(name, text)
        elif pathmod.exists(text):
            raise RequestError(
                400, "usage", f"{name!r}: name a file by its absolute path."
            )
