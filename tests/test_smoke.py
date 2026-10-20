"""Fast argv-to-handler wiring checks for every command.

Nothing is executed: dispatch is intercepted after parsing. Running the commands
against real tools on synthetic data is covered by tests/test_smoke_real_*.py.
"""

import argparse
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from wgsextract_cli.cli.parser import build_parser
from wgsextract_cli.main import main

COMMANDS: list[tuple[str, list[str]]] = [
    ("info", ["info", "--input", "{bam}"]),
    ("info --detailed", ["info", "--input", "{bam}", "--detailed"]),
    ("info calculate-coverage", ["info", "calculate-coverage", "--input", "{bam}"]),
    ("info coverage-sample", ["info", "coverage-sample", "--input", "{bam}"]),
    ("bam sort", ["bam", "sort", "--input", "{bam}"]),
    ("bam index", ["bam", "index", "--input", "{bam}"]),
    ("bam unindex", ["bam", "unindex", "--input", "{bam}"]),
    ("bam unsort", ["bam", "unsort", "--input", "{bam}"]),
    ("bam to-cram", ["bam", "to-cram", "--input", "{bam}"]),
    ("bam to-bam", ["bam", "to-bam", "--input", "{bam}"]),
    (
        "bam unalign",
        ["bam", "unalign", "--input", "{bam}", "--r1", "out.r1", "--r2", "out.r2"],
    ),
    ("bam identify", ["bam", "identify", "--input", "{bam}"]),
    ("extract bam-subset", ["extract", "bam-subset", "--input", "{bam}", "-f", "0.1"]),
    (
        "extract mito-fasta",
        ["extract", "mito-fasta", "--input", "{bam}", "--ref", "{ref}"],
    ),
    (
        "extract ydna-bam",
        ["extract", "ydna-bam", "--input", "{bam}", "--ref", "{ref}"],
    ),
    ("extract unmapped", ["extract", "unmapped", "--input", "{bam}"]),
    ("extract mt-bam", ["extract", "mt-bam", "--input", "{bam}", "--ref", "{ref}"]),
    ("vcf snp", ["vcf", "snp", "--input", "{bam}", "--ref", "{ref}"]),
    ("vcf indel", ["vcf", "indel", "--input", "{bam}", "--ref", "{ref}"]),
    (
        "vcf annotate",
        ["vcf", "annotate", "--input", "{vcf}", "--ann-vcf", "{vcf}", "--cols", "ID"],
    ),
    ("vcf filter", ["vcf", "filter", "--input", "{vcf}", "--expr", "QUAL>30"]),
    (
        "vcf filter --gene",
        ["vcf", "filter", "--input", "{vcf}", "--ref", "{dir}", "--gene", "BRCA1"],
    ),
    ("vcf cnv", ["vcf", "cnv", "--input", "{bam}", "--ref", "{ref}"]),
    ("vcf sv", ["vcf", "sv", "--input", "{bam}", "--ref", "{ref}"]),
    ("vcf freebayes", ["vcf", "freebayes", "--input", "{bam}", "--ref", "{ref}"]),
    (
        "vcf trio",
        [
            "vcf",
            "trio",
            "--proband",
            "{vcf}",
            "--mother",
            "{vcf}",
            "--father",
            "{vcf}",
            "--mode",
            "denovo",
        ],
    ),
    ("microarray", ["microarray", "--input", "{bam}", "--ref", "{ref}"]),
    (
        "lineage mt-haplogroup",
        ["lineage", "mt-haplogroup", "--input", "{vcf}", "--haplogrep-path", "x.jar"],
    ),
    (
        "lineage y-haplogroup",
        [
            "lineage",
            "y-haplogroup",
            "--input",
            "{bam}",
            "--yleaf-path",
            "fake.py",
            "--pos-file",
            "fake.txt",
        ],
    ),
    ("repair ftdna-bam", ["repair", "ftdna-bam", "--input", "{bam}"]),
    ("repair ftdna-vcf", ["repair", "ftdna-vcf", "--input", "{vcf}"]),
    ("qc fastp", ["qc", "fastp", "--r1", "{fastq}", "--r2", "{fastq}"]),
    ("qc fastqc", ["qc", "fastqc", "--input", "{bam}"]),
    ("qc vcf", ["qc", "vcf", "--input", "{vcf}"]),
    (
        "pet-align",
        ["pet-align", "--r1", "{fastq}", "--species", "dog", "--ref", "{ref}"],
    ),
    ("ref download", ["ref", "download", "--url", "http://fake", "--out", "out.fa"]),
    ("ref index", ["ref", "index", "--ref", "fake.fa"]),
    ("ref library", ["ref", "library"]),
    ("ref gene-map", ["ref", "gene-map"]),
    (
        "align",
        ["align", "--ref", "{ref}", "--r1", "{fastq}", "--r2", "{fastq}"],
    ),
    ("vep", ["vep", "--input", "{vcf}", "--format", "vcf"]),
    ("vep download", ["vep", "download", "--assembly", "GRCh38"]),
    ("sign create", ["sign", "create", "{bam}"]),
    ("sign verify", ["sign", "verify", "{dir}/SHA256SUMS"]),
    ("typing cyp2d6", ["typing", "cyp2d6", "--input", "{bam}"]),
    ("typing hla", ["typing", "hla", "--input", "{bam}", "--hla-index", "x.fa"]),
    ("typing str", ["typing", "str", "--input", "{bam}", "--sex", "male"]),
]


def _leaf_parsers(
    parser: argparse.ArgumentParser, path: tuple[str, ...] = ()
) -> Iterator[tuple[tuple[str, ...], argparse.ArgumentParser]]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                yield from _leaf_parsers(sub, (*path, name))
            return
    yield path, parser


def test_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code in (0, None)
    assert "usage" in capsys.readouterr().out.lower()


def test_every_leaf_command_has_a_handler() -> None:
    parser, _ = build_parser()
    leaves = [path for path, _ in _leaf_parsers(parser) if path]
    assert leaves
    for path, sub in _leaf_parsers(parser):
        if path:
            assert callable(sub.get_default("func")), " ".join(path)


@pytest.mark.parametrize(
    "args", [pytest.param(args, id=name) for name, args in COMMANDS]
)
def test_command_dispatch(args: list[str], tmp_path: Path) -> None:
    paths = {
        "dir": str(tmp_path),
        "bam": str(tmp_path / "fake.bam"),
        "ref": str(tmp_path / "fake_ref.fa"),
        "fastq": str(tmp_path / "dummy.fastq"),
        "vcf": str(tmp_path / "dummy.vcf.gz"),
    }
    argv = ["--outdir", paths["dir"], *(a.format(**paths) for a in args)]
    with patch("wgsextract_cli.main.dispatch_command") as dispatch:
        main(argv)
    dispatch.assert_called_once()
    parsed = dispatch.call_args.args[0]
    assert callable(parsed.func)
    assert parsed.func.__module__.startswith("wgsextract_cli.commands")
    assert parsed.outdir == paths["dir"]
    for flag, key in (("--input", "input"), ("--ref", "ref")):
        if flag in argv:
            assert getattr(parsed, key) == argv[argv.index(flag) + 1]
