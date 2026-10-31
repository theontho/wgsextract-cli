"""Score generated kits against an independent public or approved donated chip.

No sample identifiers, paths, marker IDs or genotypes are written to the report.
"""

import argparse
import json
from pathlib import Path

from wgsextract_cli.commands.benchmark.chip import read_chip_export, score_chip


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--truth", type=Path, required=True)
    parser.add_argument("--truth-format", default="CombinedKit")
    parser.add_argument("--kit", action="append", required=True, metavar="FORMAT=PATH")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    truth = read_chip_export(args.truth, args.truth_format)
    reports = {}
    for binding in args.kit:
        name, separator, path = binding.partition("=")
        if not separator or name in reports:
            parser.error("--kit requires a unique FORMAT=PATH binding")
        reports[name] = score_chip(read_chip_export(Path(path), name), truth).as_dict()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(reports, indent=2) + "\n", encoding="utf-8", newline="\n"
    )


if __name__ == "__main__":
    main()
