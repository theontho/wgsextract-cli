"""The operations the server offers: every function of the typed API that takes
a ``Params`` and returns a ``Result``, with request checking and the folders
requests may name."""

from __future__ import annotations

import functools
import inspect
import os
import typing
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Literal

from wgsextract_cli import api
from wgsextract_cli.api._params import ParamField, Params, json_schema, param_fields
from wgsextract_cli.core.results import CommandResult
from wgsextract_cli.core.utils import UsageError

Kind = Literal["query", "job"]

# Quick reads that describe a sample; everything else makes new files, takes
# longer or changes what is recorded about a sample, so MCP offers it only
# with --allow-jobs.
QUERIES = frozenset({"info", "info chip", "sample show"})


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

    def describe(self) -> dict[str, Any]:
        return {"op": self.name, "kind": self.kind, "summary": self.summary}


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
        if read_only and item.spec.writes and value is not None:
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
        if not (isinstance(value, list) and all(isinstance(v, str) for v in value)):
            raise _type_error(item, "an array of strings")
        return tuple(value)
    if item.spec.path or base is Path or base is str:
        if not isinstance(value, str):
            raise _type_error(item, "a string")
        return value
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


def _type_error(item: ParamField, expected: str) -> RequestError:
    return RequestError(400, "usage", f"{item.name!r} must be {expected}.")


def _real(path: Path) -> Path:
    return Path(os.path.realpath(path))


class PathGuard:
    """The folders requests may name. A path is allowed when, with symbolic
    links resolved, it is one of them or inside one."""

    def __init__(self, roots: Iterable[str | os.PathLike[str]]) -> None:
        unique: dict[Path, None] = {}
        for root in roots:
            unique[_real(Path(root).expanduser())] = None
        self.roots = tuple(unique)

    def check_path(self, name: str, value: Path) -> None:
        path = value.expanduser()
        if not path.is_absolute():
            raise RequestError(
                400, "usage", f"{name!r} must be an absolute path, not {str(value)!r}."
            )
        real = _real(path)
        if not any(real == root or real.is_relative_to(root) for root in self.roots):
            raise RequestError(
                403,
                "path_not_allowed",
                f"{name!r}: {path} is outside the folders this server may use "
                "(serve --allow DIR, or config server_allowed_paths).",
            )

    def check(self, params: Params) -> None:
        for item in param_fields(type(params)):
            value = getattr(params, item.name)
            for path in value if isinstance(value, tuple) else (value,):
                if isinstance(path, Path):
                    self.check_path(item.name, path)
