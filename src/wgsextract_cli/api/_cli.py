"""Generating a command's argparse options from its Params, and the CLI handler
that turns parsed arguments back into Params."""

from __future__ import annotations

import argparse
import os
import typing
from collections.abc import Callable
from typing import Any, TypeVar

from ._params import Params, fixed_length, from_namespace, param_fields
from ._runtime import run_inline

P = TypeVar("P", bound=Params)


def add_arguments(
    parser: argparse.ArgumentParser, cls: type[Params], *, config_defaults: bool = False
) -> None:
    """Add an option for every non-shared field of ``cls``, in field order.

    With ``config_defaults``, an option whose field has a ``config_key``
    defaults to that setting, so the run's recorded parameters show the value
    in effect (as the shared options' do).
    """
    from wgsextract_cli.core.config import settings

    groups: dict[str, argparse._MutuallyExclusiveGroup] = {}
    sections: dict[str, argparse._ArgumentGroup] = {}
    for item in param_fields(cls):
        spec = item.spec
        if spec.shared:
            continue
        if spec.positional:
            positional: dict[str, Any] = {"metavar": spec.metavar, "help": spec.help}
            if typing.get_origin(item.base_type) is tuple:
                positional["nargs"] = "+" if item.required else "*"
            elif not item.required:
                positional.update(nargs="?", default=item.default)
            parser.add_argument(item.dest, **positional)
            continue
        target: argparse._ActionsContainer = parser
        if spec.group:
            title, description = spec.group
            if title not in sections:
                sections[title] = parser.add_argument_group(title, description)
            target = sections[title]
        if spec.exclusive:
            if spec.exclusive not in groups:
                groups[spec.exclusive] = target.add_mutually_exclusive_group()
            target = groups[spec.exclusive]
        kwargs: dict[str, Any] = {"dest": item.dest, "help": spec.help}
        if item.required:
            kwargs["required"] = True
        elif spec.action == "append":
            kwargs["default"] = None if item.default is None else list(item.default)
        elif config_defaults and spec.config_key:
            kwargs["default"] = settings.get(spec.config_key, item.default)
        elif isinstance(item.default, os.PathLike):
            kwargs["default"] = os.fspath(item.default)
        else:
            kwargs["default"] = item.default
        if spec.action == "store_true":
            kwargs["action"] = "store_true"
        elif spec.action == "append":
            kwargs["action"] = "append"
        elif spec.action == "boolean_optional":
            kwargs["action"] = argparse.BooleanOptionalAction
        if item.tuple_length is not None:
            kwargs["nargs"] = item.tuple_length
        elif spec.action == "store" and typing.get_origin(item.base_type) is tuple:
            kwargs["nargs"] = "+"
        if spec.action in ("store", "append"):
            value_type = spec.cli_type or _value_type(_element_type(item.base_type))
            if value_type is not None:
                kwargs["type"] = value_type
            if spec.choices is not None:
                kwargs["choices"] = spec.choices
            if spec.metavar is not None:
                kwargs["metavar"] = spec.metavar
        target.add_argument(*item.flags, **kwargs)


def _value_type(tp: Any) -> Callable[[str], Any] | None:
    return tp if tp in (int, float) else None


def _element_type(tp: Any) -> Any:
    """A fixed-length tuple's element type (``tuple[float, float]``: float)."""
    return typing.get_args(tp)[0] if fixed_length(tp) is not None else tp


def cli_handler(
    cls: type[P], impl: Callable[..., None], **render: str
) -> Callable[[argparse.Namespace], None]:
    """The ``func`` of a command parser: parsed arguments → ``cls`` → ``impl``.

    ``render`` maps keyword arguments of ``impl`` to CLI-only presentation
    options (``as_csv="csv"``) that are not part of the API.
    """

    def handler(args: argparse.Namespace) -> None:
        options = {name: getattr(args, dest, False) for name, dest in render.items()}
        typed: set[str] = getattr(args, "_explicit_dests", set())
        explicit = {item.name for item in param_fields(cls) if item.dest in typed}
        run_inline(from_namespace(cls, args), lambda p: impl(p, **options), explicit)

    handler.__module__ = impl.__module__
    handler.__name__ = handler.__qualname__ = f"cli_{cls.__name__}"
    handler.__doc__ = f"Run {cls.COMMAND!r} from parsed CLI arguments."
    return handler
