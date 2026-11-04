"""``sample``: show and correct what is recorded about a sample in its
workspace, ``wgsextract.sample.toml`` (see docs/sample-workspace.md)."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wgsextract_cli.core.results import CommandResult
from wgsextract_cli.core.sample_workspace import FACTS, PLATFORMS, SEXES
from wgsextract_cli.core.utils import UsageError

from ._params import Params, param
from ._runtime import CancelToken, ProgressCallback, execute
from ._shared import optional_input_param

_INPUT = (
    "BAM, CRAM or VCF of the sample; its folder is the workspace unless "
    "workspace is given, and its facts are shown."
)


def _workspace_param() -> Any:
    return param(
        None,
        help="Sample folder holding wgsextract.sample.toml (default: the "
        "--genome folder, else the --input's folder).",
        path="directory",
        metavar="DIR",
    )


def _choice(name: str) -> Callable[[str], str]:
    def parse(text: str) -> str:
        try:
            return FACTS[name].normalize(text)
        except ValueError as e:
            raise argparse.ArgumentTypeError(str(e)) from None

    parse.__name__ = name
    return parse


@dataclass(frozen=True, kw_only=True)
class _SampleParams(Params):
    input: Path | None = optional_input_param(_INPUT)
    workspace: Path | None = _workspace_param()

    def __post_init__(self) -> None:
        for name in ("sex", "platform"):
            value = getattr(self, name, None)
            if isinstance(value, str):
                try:
                    object.__setattr__(self, name, FACTS[name].normalize(value))
                except ValueError as e:
                    raise UsageError(f"--{name}: {e}.") from None
        super().__post_init__()
        if self.input is None and self.workspace is None:
            raise UsageError("Give --genome, --workspace or --input.")

    @property
    def folder(self) -> Path:
        """The workspace folder."""
        if self.workspace is not None:
            return self.workspace
        assert self.input is not None
        return self.input.expanduser().absolute().parent


@dataclass(frozen=True, kw_only=True)
class SampleShowParams(_SampleParams):
    """Show a sample's overrides, the facts recorded for each of its files and
    the result manifests indexed in its workspace."""

    COMMAND = "sample show"


@dataclass(frozen=True, kw_only=True)
class SampleSetParams(_SampleParams):
    """Set overrides: facts every command uses instead of detecting them."""

    COMMAND = "sample set"

    sex: str | None = param(
        None,
        help="The sample's sex (m and f are accepted too).",
        choices=SEXES,
        cli_type=_choice("sex"),
        metavar="SEX",
    )
    platform: str | None = param(
        None,
        help="The sequencing platform (nanopore, hifi, mgi and bgi are accepted too).",
        choices=PLATFORMS,
        cli_type=_choice("platform"),
        metavar="PLATFORM",
    )

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.sex is None and self.platform is None:
            raise UsageError("Give --sex and/or --platform to set.")


@dataclass(frozen=True, kw_only=True)
class SampleUnsetParams(_SampleParams):
    """Remove overrides, so commands detect those facts again."""

    COMMAND = "sample unset"

    sex: bool = param(False, help="Remove the sex override.", action="store_true")
    platform: bool = param(
        False, help="Remove the platform override.", action="store_true"
    )

    def __post_init__(self) -> None:
        super().__post_init__()
        if not (self.sex or self.platform):
            raise UsageError("Give --sex and/or --platform to unset.")


def show(
    params: SampleShowParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Read the workspace. ``details`` holds ``overrides``, ``files`` (each
    recorded file's ``status`` — current, changed or missing — and ``facts``),
    ``outputs`` and, with an input, ``effective``: the facts in effect for it,
    each with its ``source`` (override or detected)."""
    from wgsextract_cli.commands.sample import run_show

    return execute(params, run_show, progress=progress, cancel=cancel, check=check)


def set_overrides(
    params: SampleSetParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Set overrides; ``details`` is the workspace afterwards, as ``show``."""
    from wgsextract_cli.commands.sample import run_set

    return execute(params, run_set, progress=progress, cancel=cancel, check=check)


def unset_overrides(
    params: SampleUnsetParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Remove overrides; ``details`` is the workspace afterwards, as ``show``."""
    from wgsextract_cli.commands.sample import run_unset

    return execute(params, run_unset, progress=progress, cancel=cancel, check=check)
