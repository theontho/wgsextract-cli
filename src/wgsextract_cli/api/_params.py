"""Parameter declarations: frozen dataclasses whose field metadata also drives
the CLI parser, the reference docs and (later) server-mode schemas."""

from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import types
import typing
from collections.abc import Callable, Collection
from dataclasses import MISSING, dataclass
from pathlib import Path
from typing import Any, ClassVar, Literal, TypeVar

from wgsextract_cli.core.utils import UsageError

PathKind = Literal["file", "directory", "file_or_directory"]
Action = Literal["store", "store_true", "append", "boolean_optional"]

_SPEC = "wgsextract_param"

P = TypeVar("P", bound="Params")


@dataclass(frozen=True)
class ParamSpec:
    """How one Params field is documented and exposed on the command line.

    ``shared`` fields (``--input``, ``--ref``, ``--outdir``, ``--threads``) are
    global options every command parser already has; the generator doesn't add
    them again. ``config_key`` names the config.toml setting a ``None`` value
    falls back to. ``required_message`` is the usage error raised when a
    required value is missing on the command line. ``local_only`` fields are
    set only on the command line, in config.toml or by a Python caller, never
    by a server request: their value names a program to run, or a file the
    server's path checks can't see.
    """

    help: str | None = None
    flags: tuple[str, ...] = ()
    metavar: str | None = None
    choices: tuple[Any, ...] | None = None
    path: PathKind | None = None
    shared: bool = False
    action: Action = "store"
    cli_type: Callable[[str], Any] | None = None
    exclusive: str | None = None
    dest: str | None = None
    config_key: str | None = None
    required_message: str | None = None
    doc: str | None = None
    local_only: bool = False

    @property
    def description(self) -> str:
        """API documentation: ``doc``, else the CLI help."""
        return plain_help(self.doc or self.help)


def param(
    default: Any = MISSING,
    *,
    help: str | None = None,
    flags: tuple[str, ...] = (),
    metavar: str | None = None,
    choices: tuple[Any, ...] | None = None,
    path: PathKind | None = None,
    shared: bool = False,
    action: Action | None = None,
    cli_type: Callable[[str], Any] | None = None,
    exclusive: str | None = None,
    dest: str | None = None,
    config_key: str | None = None,
    required_message: str | None = None,
    doc: str | None = None,
    local_only: bool = False,
) -> Any:
    """Declare a Params field; omit ``default`` for a required one.

    ``help`` is the CLI help text (an argparse %-template); ``doc`` documents
    the field for API users where the CLI help is missing or CLI-specific.
    """
    spec = ParamSpec(
        help=help,
        flags=flags,
        metavar=metavar,
        choices=choices,
        path=path,
        shared=shared,
        action=action or "store",
        cli_type=cli_type,
        exclusive=exclusive,
        dest=dest,
        config_key=config_key,
        required_message=required_message,
        doc=doc,
        local_only=local_only,
    )
    return dataclasses.field(default=default, metadata={_SPEC: spec})


@dataclass(frozen=True)
class ParamField:
    """A Params field with its resolved type and spec."""

    name: str
    type: Any
    spec: ParamSpec
    default: Any
    required: bool

    @property
    def dest(self) -> str:
        return self.spec.dest or self.name

    @property
    def flags(self) -> tuple[str, ...]:
        return self.spec.flags or ("--" + self.name.replace("_", "-"),)

    @property
    def base_type(self) -> Any:
        """The annotation without ``| None``."""
        args = [a for a in typing.get_args(self.type) if a is not type(None)]
        if _is_union(self.type) and len(args) == 1:
            return args[0]
        return self.type


def _is_union(tp: Any) -> bool:
    return typing.get_origin(tp) in (typing.Union, types.UnionType)


@dataclass(frozen=True, kw_only=True)
class Params:
    """Base class of every operation's parameters.

    Subclasses are frozen, keyword-only dataclasses. ``COMMAND`` names the CLI
    command ("vcf snp") and becomes the result's ``command``. Path fields accept
    ``str`` or ``os.PathLike`` and are stored as ``Path`` (an empty string is
    ``None``); ``choices`` are checked on construction.
    """

    COMMAND: ClassVar[str] = ""

    def __post_init__(self) -> None:
        for item in param_fields(type(self)):
            value = getattr(self, item.name)
            if item.spec.path and isinstance(value, (str, os.PathLike)):
                value = Path(value) if os.fspath(value) else None
                object.__setattr__(self, item.name, value)
            if item.required and value is None:
                raise UsageError(
                    item.spec.required_message or f"{item.flags[0]} required."
                )
            if (
                item.spec.choices is not None
                and value is not None
                and value not in item.spec.choices
            ):
                allowed = ", ".join(map(str, item.spec.choices))
                raise UsageError(
                    f"{item.flags[0]} must be one of {allowed}, not {value!r}."
                )

    def to_dict(self) -> dict[str, Any]:
        """The parameters as JSON-ready values (paths as strings)."""
        out: dict[str, Any] = {}
        for item in param_fields(type(self)):
            value = getattr(self, item.name)
            if isinstance(value, os.PathLike):
                value = os.fspath(value)
            elif isinstance(value, tuple):
                value = list(value)
            out[item.name] = value
        return out


_FIELD_CACHE: dict[type[Params], tuple[ParamField, ...]] = {}


def param_fields(cls: type[Params]) -> tuple[ParamField, ...]:
    """The documented fields of a Params class, in declaration order."""
    cached = _FIELD_CACHE.get(cls)
    if cached is not None:
        return cached
    hints = typing.get_type_hints(cls)
    fields = []
    for item in dataclasses.fields(cls):
        spec = item.metadata.get(_SPEC)
        if not isinstance(spec, ParamSpec):
            raise TypeError(f"{cls.__name__}.{item.name} must be declared with param()")
        fields.append(
            ParamField(
                name=item.name,
                type=hints[item.name],
                spec=spec,
                default=None if item.default is MISSING else item.default,
                required=item.default is MISSING,
            )
        )
    result = tuple(fields)
    _FIELD_CACHE[cls] = result
    return result


def with_config_defaults(params: P, keep: Collection[str] = ()) -> P:
    """Fill ``None`` fields that have a ``config_key`` from config.toml, except
    the fields named in ``keep``."""
    from wgsextract_cli.core.config import settings

    updates: dict[str, Any] = {}
    for item in param_fields(type(params)):
        key = item.spec.config_key
        if not key or item.name in keep or getattr(params, item.name) is not None:
            continue
        value = settings.get(key)
        if value is not None and item.spec.cli_type is not None:
            try:
                value = item.spec.cli_type(str(value))
            except (ValueError, argparse.ArgumentTypeError) as e:
                logging.warning(f"Ignoring invalid config {key}: {e}")
                value = None
        if value is not None:
            updates[item.name] = value
    return dataclasses.replace(params, **updates) if updates else params


def from_namespace(cls: type[P], args: argparse.Namespace | Any) -> P:
    """Build ``cls`` from parsed CLI arguments.

    Missing attributes take the field default, so the partial namespaces older
    callers build keep working. A required field without a value raises the
    field's ``required_message`` as a usage error.
    """
    values: dict[str, Any] = {}
    for item in param_fields(cls):
        value = getattr(args, item.dest, None)
        if value is None:
            if item.required:
                raise UsageError(
                    item.spec.required_message or f"{item.flags[0]} required."
                )
            continue
        if item.spec.action == "append":
            value = tuple(value)
        values[item.name] = value
    return cls(**values)


def to_namespace(params: Params) -> argparse.Namespace:
    """A Namespace view (string paths, list appends) for code not yet typed."""
    values: dict[str, Any] = {}
    for item in param_fields(type(params)):
        value = getattr(params, item.name)
        if isinstance(value, os.PathLike):
            value = os.fspath(value)
        elif isinstance(value, tuple):
            value = list(value)
        values[item.dest] = value
    return argparse.Namespace(**values)


def json_schema(cls: type[Params]) -> dict[str, Any]:
    """A JSON Schema (draft 2020-12) of the parameters, for server mode and docs."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    for item in param_fields(cls):
        schema = _type_schema(item.type, item.spec)
        if item.spec.description:
            schema["description"] = item.spec.description
        if item.required:
            required.append(item.name)
        else:
            default = item.default
            schema["default"] = list(default) if isinstance(default, tuple) else default
        properties[item.name] = schema
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": cls.__name__,
        "description": (cls.__doc__ or "").strip().split("\n\n")[0],
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _type_schema(tp: Any, spec: ParamSpec) -> dict[str, Any]:
    args = typing.get_args(tp) if _is_union(tp) else (tp,)
    nullable = type(None) in args
    base = next((a for a in args if a is not type(None)), str)
    schema: dict[str, Any]
    if spec.path or base is Path:
        schema = {"type": "string", "format": "path"}
    elif base is bool:
        schema = {"type": "boolean"}
    elif base is int:
        schema = {"type": "integer"}
    elif base is float:
        schema = {"type": "number"}
    elif typing.get_origin(base) is tuple:
        schema = {"type": "array", "items": {"type": "string"}}
    else:
        schema = {"type": "string"}
    if spec.choices is not None:
        schema["enum"] = list(spec.choices)
    if nullable:
        schema["type"] = [schema["type"], "null"]
        if "enum" in schema:
            schema["enum"].append(None)
    return schema


def plain_help(text: str | None) -> str:
    """Help strings are argparse %-templates; render ``%%`` outside argparse."""
    return (text or "").replace("%%", "%")


def type_name(tp: Any) -> str:
    """A readable annotation: ``Path | None``, ``tuple[str, ...]``."""
    if _is_union(tp):
        return " | ".join(type_name(a) for a in typing.get_args(tp))
    if tp is type(None):
        return "None"
    if typing.get_origin(tp) is tuple:
        return "tuple[" + ", ".join(type_name(a) for a in typing.get_args(tp)) + "]"
    if tp is Ellipsis:
        return "..."
    return str(getattr(tp, "__name__", tp))
