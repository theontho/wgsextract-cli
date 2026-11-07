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
from pathlib import Path, PurePath
from typing import Any

GOLDEN = Path(__file__).parent / "fixtures" / "api_cli_snapshot.json"

MIGRATED = (
    ("info",),
    ("info", "calculate-coverage"),
    ("info", "coverage-sample"),
    ("info", "chip"),
    ("vcf",),
    ("vcf", "annotate"),
    ("vcf", "filter"),
    ("vcf", "table"),
    ("vcf", "to-fasta"),
    ("vcf", "trio"),
    ("vcf", "clinvar"),
    ("vcf", "revel"),
    ("vcf", "phylop"),
    ("vcf", "cadd"),
    ("vcf", "gerp"),
    ("vcf", "gnomad"),
    ("vcf", "spliceai"),
    ("vcf", "alphamissense"),
    ("vcf", "pharmgkb"),
    ("vcf", "pharmcat"),
    ("vcf", "chain-annotate"),
    ("vcf", "snp"),
    ("vcf", "indel"),
    ("vcf", "cnv"),
    ("vcf", "sv"),
    ("vcf", "freebayes"),
    ("vcf", "gatk"),
    ("vcf", "deepvariant"),
    ("vcf", "clair3"),
    ("vcf", "phase"),
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
    ("bam",),
    ("bam", "sort"),
    ("bam", "index"),
    ("bam", "unindex"),
    ("bam", "unsort"),
    ("bam", "to-cram"),
    ("bam", "to-bam"),
    ("bam", "unalign"),
    ("bam", "identify"),
    ("bam", "merge"),
    ("bam", "to-ubam"),
    ("qc",),
    ("qc", "fastp"),
    ("qc", "fastqc"),
    ("qc", "multiqc"),
    ("qc", "multiqc-metrics"),
    ("qc", "vcf"),
    ("qc", "fake-data"),
    ("align",),
    ("realign",),
    ("basecall",),
    ("ref",),
    ("ref", "aadr"),
    ("ref", "ybrowse"),
    ("ref", "download"),
    ("ref", "index"),
    ("ref", "count-ns"),
    ("ref", "verify"),
    ("ref", "library"),
    ("ref", "library-list"),
    ("ref", "status"),
    ("ref", "gene-map"),
    ("ref", "clinvar"),
    ("ref", "revel"),
    ("ref", "phylop"),
    ("ref", "gnomad"),
    ("ref", "spliceai"),
    ("ref", "alphamissense"),
    ("ref", "pharmgkb"),
    ("ref", "cadd"),
    ("ref", "gerp"),
    ("ref", "bootstrap"),
    ("pet-align",),
    ("analyze",),
    ("analyze", "comprehensive"),
    ("analyze", "batch-gen"),
    ("repair",),
    ("repair", "ftdna-bam"),
    ("repair", "ftdna-vcf"),
    ("sign",),
    ("sign", "create"),
    ("sign", "verify"),
    ("deps",),
    ("deps", "check"),
    ("example-genome",),
    ("example-genome", "list"),
    ("example-genome", "download"),
    ("typing",),
    ("typing", "cyp2d6"),
    ("typing", "hla"),
    ("typing", "str"),
    ("methylation",),
    ("methylation", "pileup"),
    ("vep",),
    ("vep", "run"),
    ("vep", "download"),
    ("vep", "verify"),
    ("benchmark",),
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
    ["vcf", "annotate", "-i", "x.vcf.gz", "--ann-vcf", "a.vcf.gz", "--cols", "ID"],
    ["vcf", "annotate", "--vcf-input", "x.vcf.gz", "-i", "y.bam"],
    ["vcf", "filter", "-i", "x.vcf.gz", "--expr", "QUAL>30", "--gene", "BRCA1"],
    ["vcf", "filter", "-i", "x.vcf.gz", "--exclude-near-gaps", "-r", "chr1"],
    ["vcf", "table", "-i", "x.vcf.gz", "--sample", "S", "--all-alleles", "--no-sqlite"],
    [
        "vcf",
        "table",
        "-i",
        "x.vcf.gz",
        "--pharmcat-json",
        "p.json",
        "--format",
        "parquet",
    ],
    ["vcf", "to-fasta", "-i", "x.vcf.gz", "--haplotype", "1", "--uncalled", "n"],
    ["vcf", "to-fasta", "-i", "x.vcf.gz", "--mask", "m.bed", "--chain", "-r", "chr2"],
    ["vcf", "to-fasta", "-i", "x.vcf.gz", "--include-filtered", "--sample", "S"],
    ["vcf", "trio", "--proband", "p.vcf.gz", "--mother", "m.vcf", "--father", "f.vcf"],
    [
        "vcf",
        "trio",
        "-i",
        "p.bam",
        "--mother",
        "m.bam",
        "--father",
        "f.bam",
        "--mode",
        "all",
        "--min-depth",
        "10",
        "--proband-sex",
        "female",
        "-r",
        "chr20",
    ],
    ["vcf", "clinvar", "-i", "x.vcf.gz", "--clinvar-file", "c.vcf.gz"],
    ["vcf", "clinvar", "-i", "x.vcf.gz", "--confidence-depth", "5", "15"],
    ["vcf", "clinvar", "-i", "x.vcf.gz", "--confidence-gq", "10", "25.5"],
    ["vcf", "revel", "-i", "x.vcf.gz", "--revel-file", "r.tsv", "--min-score", "0.5"],
    ["vcf", "phylop", "-i", "x.vcf.gz", "--phylop-file", "p.bw", "--min-score", "2"],
    ["vcf", "cadd", "-i", "x.vcf.gz", "--cadd-file", "c.tsv.gz", "--min-score", "20"],
    ["vcf", "cadd", "-i", "x.vcf.gz", "--cadd-indel-file", "i.tsv.gz"],
    ["vcf", "gerp", "-i", "x.vcf.gz", "--gerp-file", "g.bw", "--min-score", "2.0"],
    [
        "vcf",
        "gnomad",
        "-i",
        "x.vcf.gz",
        "--gnomad-file",
        "g.vcf.gz",
        "--max-af",
        "0.01",
    ],
    ["vcf", "gnomad", "-i", "x.vcf.gz", "--confidence-depth", "4", "8"],
    ["vcf", "spliceai", "-i", "x.vcf.gz", "--spliceai-file", "s.vcf.gz"],
    ["vcf", "alphamissense", "-i", "x.vcf.gz", "--am-file", "a.tsv.gz"],
    ["vcf", "alphamissense", "-i", "x.vcf.gz", "--min-score", "0.5"],
    ["vcf", "pharmgkb", "-i", "x.vcf.gz", "--pharmgkb-file", "p.tsv"],
    ["vcf", "pharmcat", "-i", "x.vcf.gz", "--pharmcat-dir", "d", "--absent-to-ref"],
    [
        "vcf",
        "pharmcat",
        "-i",
        "x.vcf.gz",
        "--outside-call",
        "a.tsv",
        "--outside-call",
        "b.tsv",
        "--confidence-gq",
        "1",
        "2",
    ],
    ["vcf", "chain-annotate", "-i", "x.vcf.gz", "--annotations", "clinvar,revel"],
    ["vcf", "chain-annotate", "--vcf-input", "x.vcf.gz", "--keep-intermediates"],
    ["vcf", "cnv", "-i", "x.bam", "-r", "chr1", "--ploidy", "3", "-M", "m.gz"],
    ["vcf", "cnv", "-i", "x.bam", "--qc-min-depth", "2"],
    ["vcf", "sv", "-i", "x.bam", "--caller", "pbsv", "--ccs", "--pacbio"],
    ["vcf", "sv", "-i", "x.bam", "--ont", "--long-read", "--tandem-repeats", "t.bed"],
    ["vcf", "freebayes", "-i", "x.bam", "-r", "chrM", "-o", "out"],
    ["vcf", "gatk", "-i", "x.cram", "--region", "chrM", "--scatter"],
    ["vcf", "gatk", "-i", "x.bam", "--gpu", "--qc-min-depth", "3"],
    ["vcf", "deepvariant", "-i", "x.bam", "--wes", "--batch-size", "256"],
    [
        "vcf",
        "deepvariant",
        "-i",
        "x.bam",
        "--model-type",
        "PACBIO",
        "--checkpoint",
        "c",
        "--force-step",
        "call_variants",
        "--dry-run",
    ],
    ["vcf", "deepvariant", "-i", "x.bam", "--ont", "--pacbio", "--gpu", "--force"],
    ["vcf", "clair3", "-i", "x.bam", "--model", "r1041", "-r", "chr20:1-100"],
    [
        "vcf",
        "phase",
        "-i",
        "x.bam",
        "--vcf-input",
        "c.vcf.gz",
        "--phaser",
        "whatshap",
        "--haplotag",
        "-r",
        "chr20",
    ],
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
    ["bam", "sort", "-i", "x.cram", "-r", "chr1", "--ref", "r.fa", "-m", "1G"],
    ["bam", "sort", "-i", "x.bam", "--gene", "BRCA1"],
    ["bam", "index", "-i", "x.bam"],
    ["bam", "unindex", "-i", "x.bam"],
    ["bam", "unsort", "-i", "x.bam", "-o", "o"],
    ["bam", "to-cram", "-i", "x.bam", "--cram-version", "3.1", "-r", "chrM"],
    ["bam", "to-cram", "-i", "x.bam", "--gene", "KCNQ2", "-t", "2"],
    ["bam", "to-bam", "-i", "x.cram", "--region", "chr2:1-5"],
    ["bam", "unalign", "-i", "x.bam", "--r1", "a.fq", "--r2", "b.fq", "--se", "c.fq"],
    ["bam", "unalign", "-i", "x.bam", "--r1", "a", "--r2", "b", "--gene", "BRCA2"],
    ["bam", "identify", "-i", "x.cram", "--ref", "r.fa"],
    ["bam", "merge", "a.bam", "b.bam", "c.bam", "--output", "m", "--cram"],
    ["bam", "merge", "a.bam", "b.bam", "--attach-rg", "-o", "out"],
    ["bam", "to-ubam", "-i", "x.bam", "--sample", "S", "--platform", "ONT"],
    ["bam", "to-ubam", "--r1", "a.fq", "--r2", "b.fq", "--library", "L2"],
    ["qc", "fastp", "--r1", "a.fq", "--r2", "b.fq", "-t", "3"],
    ["qc", "fastqc", "-i", "a.fq"],
    ["qc", "multiqc", "-i", "d", "--force"],
    ["qc", "multiqc-metrics", "-i", "d", "-o", "o"],
    ["qc", "vcf", "-i", "x.vcf.gz"],
    ["qc", "vcf", "--vcf-input", "x.vcf.gz", "-o", "o"],
    ["qc", "fake-data", "--coverage", "2.5", "--build", "hg19", "--type", "bam,vcf"],
    ["qc", "fake-data", "--full-size", "--legacy-bam", "--seed", "3"],
    ["report", "-i", "x.cram", "--format", "parquet", "-o", "out"],
    ["align", "--r1", "a.fq", "--r2", "b.fq", "--ref", "r.fa", "--sample", "S"],
    [
        "align",
        "--r1",
        "a.fq",
        "--platform",
        "ont",
        "--preset",
        "map-ont",
        "--long-read",
    ],
    ["align", "--r1", "a.fq", "--aligner", "bwa-mem2", "--gpu", "--format", "CRAM"],
    ["realign", "-i", "x.cram", "--input-ref", "o.fa", "--ref", "r.fa", "--keep-fastq"],
    ["realign", "-i", "x.bam", "--r1", "a.fq", "--r2", "b.fq", "--aligner", "minimap2"],
    [
        "realign",
        "-i",
        "x.bam",
        "--platform",
        "ultima",
        "--force-extract",
        "--sample",
        "S",
    ],
    ["realign", "-i", "x.bam", "--force", "--force-step", "align", "--dry-run"],
    [
        "realign",
        "-i",
        "x.bam",
        "--no-resume",
        "--force-step",
        "extract",
        "--force-step",
        "index",
        "--no-preflight",
        "--gpu",
        "--format",
        "CRAM",
        "--preset",
        "sr",
        "--long-read",
    ],
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
    ["ref", "aadr", "--install", "--ref", "lib"],
    ["ref", "ybrowse", "--install", "--snapshot", "y.vcf.gz"],
    ["ref", "download", "--url", "https://x/r.fa.gz", "--out", "r.fa.gz"],
    ["ref", "index", "--ref", "r.fa", "--aligner", "bwa-mem2"],
    ["ref", "count-ns", "--ref", "r.fa", "-o", "out"],
    ["ref", "verify", "--ref", "r.fa.gz"],
    ["ref", "library", "--install", "hs38", "--ref", "lib"],
    ["ref", "library", "--list"],
    ["ref", "library-list", "--ref", "lib"],
    ["ref", "status", "--genome-library", "g", "--annotation-vcf", "a.vcf.gz"],
    ["ref", "status", "-i", "x.bam", "--values"],
    ["ref", "gene-map", "--delete"],
    ["ref", "clinvar", "--ref", "lib"],
    ["ref", "revel"],
    ["ref", "phylop", "--build", "hg19"],
    ["ref", "gnomad", "--release", "4.1", "-t", "3"],
    ["ref", "spliceai"],
    ["ref", "alphamissense"],
    ["ref", "pharmgkb"],
    ["ref", "cadd", "--build", "hg19"],
    ["ref", "gerp"],
    ["ref", "bootstrap", "--ref", "lib"],
    ["pet-align", "--r1", "a.fq", "--species", "dog"],
    [
        "pet-align",
        "--r1",
        "a.fq",
        "--r2",
        "b.fq",
        "--species",
        "cat",
        "--format",
        "CRAM",
        "--ref",
        "r.fa",
    ],
    ["analyze", "comprehensive", "-i", "x.bam"],
    [
        "analyze",
        "comprehensive",
        "--vcf-inputs",
        "a.vcf.gz",
        "b.vcf.gz",
        "--skip-calling",
        "--confidence-depth",
        "5",
        "15",
        "--confidence-gq",
        "10",
        "20",
    ],
    [
        "analyze",
        "comprehensive",
        "--batch",
        "b.csv",
        "--force",
        "--force-step",
        "annotate",
        "--force-step",
        "snp",
        "--dry-run",
    ],
    ["analyze", "batch-gen", "--directory", "d"],
    ["analyze", "batch-gen", "--directory", "d", "--output", "b.tsv"],
    ["repair", "ftdna-bam", "-i", "x.bam", "--output", "y.bam"],
    ["repair", "ftdna-bam", "--ref", "r.fa", "-o", "out"],
    ["repair", "ftdna-vcf", "-i", "x.vcf.gz", "--json"],
    ["sign", "create", "a", "b/c.txt"],
    ["sign", "create", "d", "--output", "m/SUMS", "--key", "k", "-t", "2"],
    ["sign", "verify", "SHA256SUMS"],
    ["sign", "verify", "S", "--signature", "s.sig", "--public-key", "k.pub"],
    ["sign", "verify", "S", "--allowed-signers", "a", "--identity", "me"],
    ["deps", "check"],
    ["deps", "check", "--tool", "samtools", "--debug"],
    ["example-genome", "list", "--tag", "pacbio", "--tag", "hifi"],
    ["example-genome", "download", "a", "b", "--method", "https", "--force"],
    ["example-genome", "download", "--all", "--dry-run", "--target-root", "lib"],
    ["example-genome", "download", "--aspera-key", "k", "--", "--debug"],
    ["typing", "cyp2d6", "-i", "x.bam", "--ref", "r.fa", "-t", "4"],
    ["typing", "hla", "-i", "x.cram", "--hla-index", "h.fa", "-o", "out"],
    ["typing", "str", "-i", "x.bam", "--catalog", "c.json", "--sex", "male"],
    ["methylation", "pileup", "-i", "x.bam"],
    ["methylation", "pileup", "-i", "x.cram", "--ref", "r.fa", "-r", "chr1,chr2:5-9"],
    ["methylation", "pileup", "-i", "x.bam", "--5hmc", "--context", "all"],
    ["methylation", "pileup", "-i", "x.bam", "--no-5hmc", "--no-combine-strands"],
    ["methylation", "pileup", "--combine-strands", "--format", "parquet", "-o", "o"],
    ["-t", "3", "methylation", "pileup", "--input", "x.bam", "--region", "chrM"],
    ["vep", "-i", "x.vcf.gz"],
    ["vep", "run", "-i", "x.vcf.gz", "--assembly", "GRCh37", "--format", "tab"],
    ["vep", "run", "-i", "d", "--vep-assembly", "GRCh38", "--force", "--add-chr"],
    ["vep", "-i", "x.bam", "--ref", "r.fa", "-r", "chrM", "--ploidy", "1"],
    [
        "vep",
        "run",
        "-i",
        "x.bam",
        "--ploidy-file",
        "p.txt",
        "--vep-cache",
        "c",
        "--vep-cache-version",
        "115",
        "--vcf-type",
        "sv",
        "--preset",
        "fast",
        "--vep-args",
        "--pick --no_stats",
    ],
    ["vep", "download"],
    ["vep", "download", "--mirror", "ebi", "--assembly", "GRCh37", "--vep-cache", "c"],
    ["vep", "download", "--species", "mus_musculus", "--vep-version", "110"],
    ["vep", "verify", "--ref", "lib", "--vep-version", "115"],
    ["vep", "verify", "--vep-cache", "c", "--assembly", "GRCh37"],
    ["benchmark"],
    ["benchmark", "--profile", "smoke", "--suite", "core", "-o", "o"],
    ["benchmark", "-r", "chrM", "--coverage", "2", "--target-count", "5"],
    ["benchmark", "--seed", "7", "--region", "chr20:1-9"],
    ["benchmark", "--full-size", "--build", "hg19", "--keep-going", "-t", "2"],
    ["benchmark", "--dataset", "real", "--dataset-sha256", ""],
    ["benchmark", "--dataset", "real", "--dataset-zip", "z.zip"],
    ["benchmark", "--dataset", "real", "--dataset-url", "https://h/x.zip"],
    ["-o", "o", "--debug", "benchmark", "--dataset-cache-dir", "c", "--quiet"],
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
    if isinstance(value, PurePath):  # the same on every system
        return f"Path({value.as_posix()!r})"
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
