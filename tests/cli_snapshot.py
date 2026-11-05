"""Snapshot the parsers of the commands migrated onto ``wgsextract_cli.api``.

The golden file was written from the hand-built parsers before the migration;
``test_api_cli_parity`` checks the generated parsers still match it, both
structurally (every action's flags, defaults, types, choices and help) and as
rendered ``--help`` text and parsed namespaces. After a deliberate help or flag
change to a migrated command, regenerate it with
``pixi run python tests/cli_snapshot.py``. Before migrating another group, add
it to ``MIGRATED`` and regenerate from the unmigrated code.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
from pathlib import Path
from typing import Any

GOLDEN = Path(__file__).parent / "fixtures" / "api_cli_snapshot.json"

MIGRATED = (
    ("info",),
    ("info", "calculate-coverage"),
    ("info", "coverage-sample"),
    ("info", "chip"),
    ("vcf", "snp"),
    ("vcf", "indel"),
    ("lineage", "mt-haplogroup"),
    ("lineage", "y-haplogroup"),
    ("lineage", "y-str"),
    ("microarray",),
    ("report",),
    ("extract",),
    ("extract", "mito-fasta"),
    ("extract", "mito-vcf"),
    ("extract", "mt-bam"),
    ("extract", "bam-subset"),
    ("extract", "ydna-bam"),
    ("extract", "ydna-vcf"),
    ("extract", "y-mt-extract"),
    ("extract", "unmapped"),
    ("extract", "custom"),
)

# Command lines whose parsed namespaces must not change.
ARGVS = (
    ["info", "-i", "x.bam"],
    ["info", "-i", "x.bam", "--detailed", "--csv", "--ref", "r.fa"],
    ["info", "--input", "x.cram", "calculate-coverage", "-r", "chrM", "--bins"],
    ["info", "coverage-sample", "-i", "x.bam", "--region", "chrY"],
    ["info", "chip", "-i", "kit.txt", "--list"],
    ["info", "-i", "x.bam", "--json", "--outdir", "o", "--debug"],
    ["vcf", "snp", "-i", "x.bam", "-o", "out", "-r", "chrM", "--ploidy", "1"],
    ["vcf", "snp", "-i", "x.bam", "--ploidy-file", "p.txt", "--parallel"],
    ["vcf", "indel", "-i", "x.bam", "--qc-min-depth", "4.5", "-t", "2"],
    ["-t", "4", "vcf", "snp", "-i", "x.bam", "--ref", "r.fa", "--quiet"],
    ["lineage", "mt-haplogroup", "-i", "x.bam", "--tree-links"],
    ["lineage", "mt-haplogroup", "-i", "x.vcf.gz", "--haplogrep-path", "h.jar"],
    [
        "lineage",
        "y-haplogroup",
        "-i",
        "x.bam",
        "--build",
        "hg38",
        "--tree",
        "isogg",
        "--min-reads",
        "3",
        "--base-quality",
        "15",
        "--base-majority",
        "80",
        "--min-score",
        "0.9",
        "--ancient",
        "--no-ref-inference",
        "--pos-file",
        "pos.txt",
    ],
    ["lineage", "y-str", "-i", "x.bam", "--panel", "Y-37", "--min-fraction", "0.7"],
    ["microarray", "-i", "x.bam"],
    ["extract", "mito-fasta", "-i", "x.bam", "--ref", "r.fa", "-t", "2"],
    ["extract", "mito-vcf", "-i", "x.cram", "-o", "o", "-m", "2G"],
    ["extract", "mt-bam", "-i", "x.bam"],
    ["extract", "bam-subset", "-i", "x.bam", "-f", "0.25", "-r", "chr1"],
    ["extract", "bam-subset", "-i", "x.bam", "--fraction", "0.5", "--gene", "BRCA1"],
    ["extract", "ydna-bam", "-i", "x.bam", "--poz"],
    ["extract", "ydna-vcf", "-i", "x.bam", "--ybrowse"],
    ["extract", "y-mt-extract", "-i", "x.bam", "--json"],
    ["extract", "unmapped", "-i", "x.bam", "--fastq"],
    ["extract", "custom", "-i", "x.bam", "--region", "chrM:1-100,chr1:5-9"],
    ["extract", "custom", "-i", "x.bam", "--gene", "KCNQ2", "--ref", "r.fa"],
    ["report", "--workspace", "s", "--results", "a", "--results", "b"],
    ["report", "-i", "x.cram", "--format", "parquet", "-o", "out"],
    [
        "microarray",
        "-i",
        "x.bam",
        "--formats",
        "23andme_v5,ancestry_v2",
        "--indel-manifest",
        "a=b.csv",
        "--indel-manifest",
        "c=d.csv",
        "--strand-ambiguous",
        "no-call",
        "--resolve-rsids",
        "--no-assume-hom-ref",
        "--quirks",
        "vendor",
        "--min-gq",
        "20",
        "--min-dp",
        "5",
        "--exclude-filtered",
        "--call-reasons",
        "--aadr-sample-id",
        "s1",
        "--aadr-sex",
        "F",
        "--parallel",
        "--ref-vcf-tab",
        "t.tab",
        "--ploidy-file",
        "p.txt",
        "-r",
        "chr20",
        "--qc-min-depth",
        "0",
    ],
)


def _plain(value: Any) -> Any:
    if value is argparse.SUPPRESS:
        return "==SUPPRESS=="
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_plain(v) for v in value)
    if isinstance(value, type):
        return value.__name__
    if callable(value):
        return getattr(value, "__qualname__", repr(value))
    return repr(value)


def _subparser(parser: argparse.ArgumentParser, path: tuple[str, ...]) -> Any:
    current = parser
    for name in path:
        action = next(
            a for a in current._actions if isinstance(a, argparse._SubParsersAction)
        )
        current = action.choices[name]
    return current


def describe(parser: argparse.ArgumentParser) -> dict[str, Any]:
    """Everything about a parser that shapes its help text or parse result."""
    groups = {
        id(action): index
        for index, group in enumerate(parser._mutually_exclusive_groups)
        for action in group._group_actions
    }
    actions = []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            actions.append(
                {
                    "subparsers": action.dest,
                    "required": action.required,
                    "choices": list(action.choices),
                    "help": [(a.dest, a.help) for a in action._choices_actions],
                }
            )
            continue
        actions.append(
            {
                "class": type(action).__name__,
                "option_strings": action.option_strings,
                "dest": action.dest,
                "default": _plain(action.default),
                "type": _plain(action.type),
                "choices": _plain(action.choices),
                "help": action.help,
                "metavar": _plain(action.metavar),
                "nargs": _plain(action.nargs),
                "const": _plain(action.const),
                "required": action.required,
                "exclusive_group": groups.get(id(action)),
            }
        )
    return {
        "prog": parser.prog,
        "description": parser.description,
        "formatter_class": parser.formatter_class.__name__,
        "defaults": {k: _plain(v) for k, v in sorted(parser._defaults.items())},
        "actions": actions,
    }


def _namespace(argv: list[str]) -> dict[str, Any]:
    from wgsextract_cli.cli.parser import build_parser, parse_args

    sys.argv[0] = "wgsextract"
    parser, base = build_parser()
    args, explicit = parse_args(parser, base, argv)
    values = {
        key: _plain(value)
        for key, value in sorted(vars(args).items())
        if key not in {"func", "_explicit_dests"}
    }
    values["func"] = _plain(getattr(args, "func", None))
    values["_explicit_dests"] = sorted(explicit)
    return values


def snapshot() -> dict[str, Any]:
    saved = os.environ.get("COLUMNS"), sys.argv[0]
    os.environ["COLUMNS"] = "100"
    try:
        return _snapshot()
    finally:
        if saved[0] is None:
            os.environ.pop("COLUMNS", None)
        else:
            os.environ["COLUMNS"] = saved[0]
        sys.argv[0] = saved[1]


def _snapshot() -> dict[str, Any]:
    from wgsextract_cli.cli.parser import build_parser

    sys.argv[0] = "wgsextract"
    parser, _base = build_parser()
    commands = {}
    for path in MIGRATED:
        sub = _subparser(parser, path)
        text = io.StringIO()
        with contextlib.redirect_stdout(text):
            sub.print_help()
        commands[" ".join(path)] = {"parser": describe(sub), "help": text.getvalue()}
    namespaces = {" ".join(argv): _namespace(argv) for argv in ARGVS}
    return {"commands": commands, "namespaces": namespaces}


def normalise_funcs(document: dict[str, Any]) -> dict[str, Any]:
    """Drop the handler names: migrated commands route through new adapters."""
    copied = json.loads(json.dumps(document))
    for ns in copied["namespaces"].values():
        ns.pop("func", None)
    for command in copied["commands"].values():
        command["parser"]["defaults"].pop("func", None)
    return copied


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
    GOLDEN.write_text(json.dumps(snapshot(), indent=1, sort_keys=True) + "\n")
    print(f"Wrote {GOLDEN}")
