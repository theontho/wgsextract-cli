"""Compare native probes with pinned stock Linux AVX/AVX2 results."""

import argparse
import csv
import json
import math
from pathlib import Path


def rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream, delimiter="\t"))


def compare(candidate: Path, golden: Path) -> dict[str, object]:
    reference = {(row["kind"], row["mode"], row["case"]): row for row in rows(golden)}
    mismatches: list[dict[str, object]] = []
    changed: list[dict[str, object]] = []
    maximum = 0.0
    count = 0
    seen: set[tuple[str, str, str]] = set()
    for row in rows(candidate):
        case = row["case"].split(":")[-1] if row["kind"] == "pair" else row["case"]
        key = row["kind"], row["mode"], case
        expected = reference.get(key)
        count += 1
        seen.add(key)
        if expected is None:
            mismatches.append({"row": row, "reason": "unexpected case"})
            continue
        if row["kind"] == "sw":
            if (row["value"], row["bits"]) != (expected["value"], expected["bits"]):
                mismatches.append({"row": row, "expected": expected})
            continue
        value, baseline = float.fromhex(row["value"]), float.fromhex(expected["value"])
        error = abs(value - baseline) if value != baseline else 0.0
        maximum = max(maximum, error)
        if row["bits"] != expected["bits"]:
            changed.append({"row": row, "expected": expected, "absolute_error": error})
        if not math.isfinite(error) or error > 1e-5:
            mismatches.append(
                {"row": row, "expected": expected, "absolute_error": error}
            )
    missing = sorted(set(reference) - seen)
    return {
        "pass": not mismatches and not missing,
        "rows": count,
        "strict_bit_differences": len(changed),
        "maximum_likelihood_absolute_error": maximum,
        "likelihood_stop_bound": 1e-5,
        "missing_cases": missing,
        "mismatches": mismatches,
        "all_nonexact_likelihoods": changed,
        "scope": "native synthetic probes, not chr20 site/genotype parity",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = compare(args.candidate, Path(__file__).with_name("linux-avx2-golden.tsv"))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(
        f"Native parity: pass={result['pass']}, rows={result['rows']}, "
        f"bit differences={result['strict_bit_differences']}, "
        f"max absolute error={result['maximum_likelihood_absolute_error']}"
    )
    raise SystemExit(0 if result["pass"] else 1)


if __name__ == "__main__":
    main()
