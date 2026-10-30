"""Opt-in microarray call policies; legacy defaults deliberately remain unchanged."""

import argparse
from dataclasses import dataclass
from math import isfinite
from typing import Any

from wgsextract_cli.core.config import settings
from wgsextract_cli.core.utils import WGSExtractError


@dataclass(frozen=True)
class MicroarrayQuality:
    min_gq: int = 0
    min_dp: int = 0
    assume_hom_ref: bool = True
    exclude_filtered: bool = False
    call_reasons: bool = False

    @property
    def needs_fields(self) -> bool:
        return bool(
            self.min_gq or self.min_dp or self.exclude_filtered or self.call_reasons
        )

    def rejection(self, dp: str, gq: str, record_filter: str) -> str | None:
        if self.exclude_filtered and record_filter not in {".", "PASS"}:
            return "filtered"
        for field, threshold, value in (
            ("dp", self.min_dp, dp),
            ("gq", self.min_gq, gq),
        ):
            if not threshold:
                continue
            try:
                measured = float(value)
            except ValueError:
                return f"missing_{field}"
            if not isfinite(measured) or measured < 0:
                return f"missing_{field}"
            if measured < threshold:
                return f"low_{field}"
        return None


def quality_policy(args: argparse.Namespace) -> MicroarrayQuality:
    values: dict[str, Any] = {}
    for key, default in (
        ("min_gq", 0),
        ("min_dp", 0),
        ("assume_hom_ref", True),
        ("exclude_filtered", False),
        ("call_reasons", False),
    ):
        value = getattr(args, key, None)
        if value is None:
            value = settings.get(f"microarray_{key}", default)
        if key in {"min_gq", "min_dp"}:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise WGSExtractError(
                    f"microarray {key} must be a non-negative integer."
                )
        elif not isinstance(value, bool):
            raise WGSExtractError(f"microarray {key} must be true or false.")
        values[key] = value
    return MicroarrayQuality(**values)
