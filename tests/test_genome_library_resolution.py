"""--genome: a folder of the genome library, its genome-config.toml, and the
inputs each command resolves from it."""

import os
from argparse import Namespace
from pathlib import Path

import pytest

from wgsextract_cli.cli.parser import build_parser, parse_args
from wgsextract_cli.core.config import settings
from wgsextract_cli.core.genome_library import (
    GENOME_CONFIG_NAME,
    apply_genome_selection,
    pair_stem,
)
from wgsextract_cli.core.utils import WGSExtractError


@pytest.fixture
def library(tmp_path):
    """An empty genome library set in settings (conftest restores settings)."""
    root = tmp_path / "genomes"
    root.mkdir()
    settings["genome_library"] = str(root)
    return root


def _genome(library: Path, name: str, *files: str) -> Path:
    folder = library / name
    folder.mkdir()
    for rel in files:
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_text("x")
    return folder


def _select(genome: str, explicit: set[str] | None = None, **fields) -> Namespace:
    args = Namespace(genome=genome, outdir=None, input=None, **fields)
    apply_genome_selection(args, explicit or set())
    return args


def test_without_a_genome_nothing_changes(library):
    args = Namespace(genome=None, input="x.bam", outdir=None)
    apply_genome_selection(args, set())
    assert args == Namespace(genome=None, input="x.bam", outdir=None)


def test_genome_needs_genome_library_in_config(library):
    settings.pop("genome_library")
    with pytest.raises(WGSExtractError, match="genome_library"):
        _select("ken", command="info")


def test_unknown_genome_names_its_folder(library):
    with pytest.raises(WGSExtractError, match="not found in genome_library"):
        _select("nobody", command="info")


def test_first_use_writes_the_config_with_placeholders(library):
    genome = _genome(library, "ken", "crams/ken.cram", "ken.cram.crai", ".ken.bam")

    args = _select("ken", command="microarray")

    assert args.input == str(genome / "crams" / "ken.cram")
    assert args.outdir == str(genome)
    assert args.genome_dir == str(genome)
    lines = (genome / GENOME_CONFIG_NAME).read_text().splitlines()
    assert lines[4:] == [
        'alignment = "crams/ken.cram"',
        '# vcf = "relative/path/to/file"',
        '# fastq_r1 = "relative/path/to/file"',
        '# fastq_r2 = "relative/path/to/file"',
    ]


def test_an_unchanged_config_is_not_rewritten(library):
    genome = _genome(library, "ken", "ken.bam", "a.vcf.gz", "b.vcf.gz")
    config = genome / GENOME_CONFIG_NAME
    config.write_text('# my notes\nalignment = "ken.bam"\n')

    args = _select("ken", command="info")

    assert args.input == str(genome / "ken.bam")
    assert config.read_text() == '# my notes\nalignment = "ken.bam"\n'


def test_a_broken_config_is_reported_with_its_path(library):
    genome = _genome(library, "ken", "ken.bam")
    (genome / GENOME_CONFIG_NAME).write_text("alignment = \n")

    with pytest.raises(WGSExtractError, match="Failed to read .*genome-config.toml"):
        _select("ken", command="info")


def test_a_configured_file_that_is_gone_names_the_config(library):
    genome = _genome(library, "ken", "ken.bam")
    (genome / GENOME_CONFIG_NAME).write_text('alignment = "old.bam"\n')

    with pytest.raises(WGSExtractError, match="Configured alignment does not exist"):
        _select("ken", command="info")


def test_a_configured_absolute_path_outside_the_folder_is_used(library, tmp_path):
    elsewhere = tmp_path / "nas" / "ken.cram"
    elsewhere.parent.mkdir()
    elsewhere.write_text("x")
    genome = _genome(library, "ken", "local.bam")
    (genome / GENOME_CONFIG_NAME).write_text(f'alignment = "{elsewhere.as_posix()}"\n')

    assert _select("ken", command="info").input == str(elsewhere)


def test_a_non_string_config_value_is_kept_and_discovery_used(library):
    genome = _genome(library, "ken", "ken.bam")
    (genome / GENOME_CONFIG_NAME).write_text("alignment = 5\n")

    args = _select("ken", command="info")

    assert args.input == str(genome / "ken.bam")
    assert "alignment = 5" in (genome / GENOME_CONFIG_NAME).read_text()


def test_explicit_input_and_outdir_win(library):
    _genome(library, "ken", "ken.bam")

    args = Namespace(genome="ken", input="mine.bam", outdir="mine", command="info")
    apply_genome_selection(args, {"input", "outdir"})

    assert (args.input, args.outdir) == ("mine.bam", "mine")


def test_no_alignment_says_where_to_configure_one(library):
    _genome(library, "ken", "reads.ccs.bam")

    with pytest.raises(WGSExtractError, match="No alignment file found"):
        _select("ken", command="info")


def test_vcf_commands_resolve_the_vcf_and_clear_the_alignment(library):
    genome = _genome(library, "ken", "ken.bam", "calls/ken.vcf.gz")

    args = _select("ken", command="vcf", vcf_cmd="filter", vcf_input=None)

    assert args.vcf_input == str(genome / "calls" / "ken.vcf.gz")
    assert args.input is None


def test_vcf_calling_commands_resolve_the_alignment(library):
    genome = _genome(library, "ken", "ken.bam", "ken.vcf.gz")

    args = _select("ken", command="vcf", vcf_cmd="snp", vcf_input=None)

    assert args.input == str(genome / "ken.bam")
    assert args.vcf_input is None


def test_explicit_vcf_input_is_kept(library):
    _genome(library, "ken", "a.vcf.gz", "b.vcf.gz")

    args = _select(
        "ken", {"vcf_input"}, command="vcf", vcf_cmd="filter", vcf_input="mine.vcf"
    )

    assert args.vcf_input == "mine.vcf"


def test_ambiguous_vcfs_are_listed(library):
    _genome(library, "ken", "a.vcf.gz", "b.vcf")

    with pytest.raises(
        WGSExtractError, match=r"Ambiguous vcf files.*a\.vcf\.gz, b\.vcf"
    ):
        _select("ken", command="vcf", vcf_cmd="annotate", vcf_input=None)


def test_no_vcf_says_where_to_configure_one(library):
    _genome(library, "ken", "ken.bam")

    with pytest.raises(WGSExtractError, match="No vcf file found"):
        _select("ken", qc_cmd="vcf", command="qc", vcf_input=None)


def test_trio_keeps_an_explicit_proband(library):
    _genome(library, "child", "child.vcf.gz")

    args = _select(
        "child",
        {"proband"},
        command="vcf",
        vcf_cmd="trio",
        proband="mine.vcf.gz",
        vcf_input=None,
    )

    assert (args.proband, args.vcf_input) == ("mine.vcf.gz", None)


def test_fastp_resolves_the_read_pair(library):
    genome = _genome(library, "ken", "ken_R1.fq.gz", "ken_R2.fq.gz", "ken.bam")

    args = _select("ken", command="qc", qc_cmd="fastp", r1=None, r2=None)

    assert (args.r1, args.r2) == (
        str(genome / "ken_R1.fq.gz"),
        str(genome / "ken_R2.fq.gz"),
    )
    assert args.input is None


def test_explicit_r2_finds_its_r1(library):
    genome = _genome(library, "ken", "ken_1.fastq", "ken_2.fastq")

    args = _select(
        "ken", {"r2"}, command="align", r1=None, r2=str(genome / "ken_2.fastq")
    )

    assert args.r1 == str(genome / "ken_1.fastq")


def test_configured_reads_are_used(library):
    genome = _genome(library, "ken", "a_R1.fq", "a_R2.fq", "b_R1.fq", "b_R2.fq")
    (genome / GENOME_CONFIG_NAME).write_text(
        'fastq_r1 = "b_R1.fq"\nfastq_r2 = "b_R2.fq"\n'
    )

    args = _select("ken", command="align", r1=None, r2=None)

    assert (args.r1, args.r2) == (str(genome / "b_R1.fq"), str(genome / "b_R2.fq"))


def test_configured_r1_finds_its_r2(library):
    genome = _genome(library, "ken", "a_R1.fq", "a_R2.fq", "b_R1.fq", "b_R2.fq")
    (genome / GENOME_CONFIG_NAME).write_text('fastq_r1 = "b_R1.fq"\n')

    args = _select("ken", command="align", r1=None, r2=None)

    assert (args.r1, args.r2) == (str(genome / "b_R1.fq"), str(genome / "b_R2.fq"))


def test_several_pacbio_read_bams_are_ambiguous(library):
    _genome(library, "ken", "a.ccs.bam", "b.subreads.bam")

    with pytest.raises(WGSExtractError, match="Ambiguous PacBio read BAM"):
        _select("ken", command="align", r1=None, r2=None)


def test_a_genome_without_reads_leaves_align_inputs_alone(library):
    _genome(library, "ken", "ken.bam")

    args = _select("ken", command="align", r1=None, r2=None)

    assert (args.r1, args.r2) == (None, None)


def test_the_parsed_command_line_resolves_genome_inputs(library):
    genome = _genome(library, "ken mcdonald", "ken.cram")

    args, explicit = parse_args(
        *build_parser(), ["--genome", "ken mcdonald", "microarray", "--formats", "all"]
    )
    apply_genome_selection(args, explicit)

    assert (args.input, args.outdir) == (str(genome / "ken.cram"), str(genome))


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
def test_a_symlinked_genome_folder_is_allowed(library, tmp_path):
    nas = tmp_path / "nas" / "ken"
    nas.mkdir(parents=True)
    (nas / "ken.bam").write_text("x")
    (library / "ken").symlink_to(nas, target_is_directory=True)

    assert _select("ken", command="info").input == str(library / "ken" / "ken.bam")


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs chmod")
def test_a_read_only_genome_folder_still_resolves(library):
    genome = _genome(library, "ken", "ken.bam")
    genome.chmod(0o555)
    try:
        args = Namespace(genome="ken", input=None, outdir="out", command="info")
        apply_genome_selection(args, {"outdir"})
    finally:
        genome.chmod(0o755)

    assert args.input == str(genome / "ken.bam")


@pytest.mark.parametrize(
    ("stem", "expected"),
    [
        ("HG00096_R1", "HG00096"),
        ("sample.2", "sample"),
        ("s_S1_L001_R2_001", "s_S1_L001_001"),
        ("s_R1_val_1", "s_val_1"),
        ("reads_1_001", "reads_001"),
        ("R1", "R1"),
        ("run12", "run12"),
    ],
)
def test_pair_stem_drops_the_mate_marker(stem, expected):
    assert pair_stem(stem) == expected
