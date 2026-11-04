"""Generating a command's argparse options from its Params, and the CLI handler
that turns parsed arguments back into Params."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from typing import Any, TypeVar

from ._params import Params, from_namespace, param_fields
from ._runtime import run_inline

P = TypeVar("P", bound=Params)


def add_arguments(parser: argparse.ArgumentParser, cls: type[Params]) -> None:
    """Add an option for every non-shared field of ``cls``, in field order."""
    groups: dict[str, argparse._MutuallyExclusiveGroup] = {}
    for item in param_fields(cls):
        spec = item.spec
        if spec.shared:
            continue
        target: argparse.ArgumentParser | argparse._MutuallyExclusiveGroup = parser
        if spec.exclusive:
            if spec.exclusive not in groups:
                groups[spec.exclusive] = parser.add_mutually_exclusive_group()
            target = groups[spec.exclusive]
        kwargs: dict[str, Any] = {"dest": item.dest, "help": spec.help}
        if item.required:
            kwargs["required"] = True
        elif spec.action == "append":
            kwargs["default"] = list(item.default)
        else:
            kwargs["default"] = item.default
        if spec.action == "store_true":
            kwargs["action"] = "store_true"
        elif spec.action == "append":
            kwargs["action"] = "append"
        elif spec.action == "boolean_optional":
            kwargs["action"] = argparse.BooleanOptionalAction
        if spec.action in ("store", "append"):
            value_type = spec.cli_type or _value_type(item.base_type)
            if value_type is not None:
                kwargs["type"] = value_type
            if spec.choices is not None:
                kwargs["choices"] = spec.choices
            if spec.metavar is not None:
                kwargs["metavar"] = spec.metavar
        target.add_argument(*item.flags, **kwargs)


def _value_type(tp: Any) -> Callable[[str], Any] | None:
    return tp if tp in (int, float) else None


def cli_handler(
    cls: type[P], impl: Callable[..., None], **render: str
) -> Callable[[argparse.Namespace], None]:
    """The ``func`` of a command parser: parsed arguments → ``cls`` → ``impl``.

    ``render`` maps keyword arguments of ``impl`` to CLI-only presentation
    options (``as_csv="csv"``) that are not part of the API.
    """

    def handler(args: argparse.Namespace) -> None:
        options = {name: getattr(args, dest, False) for name, dest in render.items()}
        run_inline(from_namespace(cls, args), lambda p: impl(p, **options))

    handler.__module__ = impl.__module__
    handler.__name__ = handler.__qualname__ = f"cli_{cls.__name__}"
    handler.__doc__ = f"Run {cls.COMMAND!r} from parsed CLI arguments."
    return handler
