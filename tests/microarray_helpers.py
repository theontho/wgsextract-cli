"""Typed microarray parameters for tests of the generation helpers."""

from pathlib import Path
from typing import Any

from wgsextract_cli.api.microarray import MicroarrayParams


def microarray_params(
    input: str | Path = "sample.bam", **values: Any
) -> MicroarrayParams:
    """``MicroarrayParams`` with only the fields a test sets; ``input`` matters
    only to helpers that read it."""
    values = {k: tuple(v) if isinstance(v, list) else v for k, v in values.items()}
    return MicroarrayParams(input=Path(input), **values)
