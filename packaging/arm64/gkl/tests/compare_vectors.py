"""Compare GklVectorProbe outputs from a candidate (Arm64) and a reference (x64) host."""

from __future__ import annotations

import argparse
import csv
import json
import struct
from pathlib import Path
from typing import Any


def rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def value(bits: str) -> float:
    return float(struct.unpack(">d", int(bits, 16).to_bytes(8, "big"))[0])


def pairhmm(
    candidate: list[dict[str, str]], reference: list[dict[str, str]]
) -> dict[str, Any]:
    if len(candidate) != len(reference):
        raise ValueError("PairHMM case counts differ")
    result: dict[str, Any] = {"cases": len(candidate)}
    for column in ["native_float", "native_double", "java"]:
        diffs = [
            abs(value(c[column]) - value(r[column]))
            for c, r in zip(candidate, reference, strict=True)
            if c[column] != r[column]
        ]
        result[column] = {
            "bit_exact": len(candidate) - len(diffs),
            "different": len(diffs),
            "max_abs_log10_diff": max(diffs, default=0.0),
        }
    for name, data in [("candidate", candidate), ("reference", reference)]:
        errors = [abs(value(r["native_float"]) - value(r["java"])) for r in data]
        result[f"{name}_native_float_vs_java_max_abs"] = max(errors)
    return result


def smithwaterman(
    candidate: list[dict[str, str]], reference: list[dict[str, str]]
) -> dict[str, Any]:
    if len(candidate) != len(reference):
        raise ValueError("Smith-Waterman pair counts differ")
    key = ("native_cigar", "native_offset")
    different = [
        c["strategy"] + ":" + c["index"]
        for c, r in zip(candidate, reference, strict=True)
        if tuple(c[k] for k in key) != tuple(r[k] for k in key)
    ]
    java = sum(
        (c["native_cigar"], c["native_offset"]) != (c["java_cigar"], c["java_offset"])
        for c in candidate
    )
    return {
        "alignments": len(candidate),
        "native_exact": len(candidate) - len(different),
        "native_different": different[:50],
        "candidate_native_vs_java_differences": java,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("reference", type=Path)
    args = parser.parse_args()
    report: dict[str, Any] = {
        "pairhmm": pairhmm(
            rows(args.candidate / "pairhmm.tsv"), rows(args.reference / "pairhmm.tsv")
        ),
        "smithwaterman": smithwaterman(
            rows(args.candidate / "sw.tsv"), rows(args.reference / "sw.tsv")
        ),
    }
    print(json.dumps(report, indent=2))
    hmm = report["pairhmm"]
    if (
        hmm["native_float"]["different"]
        or hmm["native_double"]["different"]
        or report["smithwaterman"]["native_different"]
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
