import os

from wgsextract_cli.commands.bam import _fastq_output_path


def test_bare_name_goes_in_outdir() -> None:
    assert _fastq_output_path("r1.fastq.gz", "/out") == os.path.join(
        "/out", "r1.fastq.gz"
    )


def test_relative_path_with_directory_is_used_as_given() -> None:
    assert _fastq_output_path("data/r1.fastq.gz", "data") == "data/r1.fastq.gz"


def test_absolute_path_is_used_as_given() -> None:
    assert _fastq_output_path("/tmp/r1.fastq.gz", "/out") == "/tmp/r1.fastq.gz"
