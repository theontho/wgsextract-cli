"""Short-read alignment writes a read group with the sample's own name."""

import argparse

from wgsextract_cli.commands.align import _read_group


def test_read_group_uses_the_sample_or_the_reads_name():
    # The aligners expand the literal \t themselves.
    assert _read_group(argparse.Namespace(sample=None), "HG002.R1") == (
        r"@RG\tID:HG002.R1\tSM:HG002\tLB:HG002"
    )
    assert _read_group(argparse.Namespace(sample="Byron"), "reads") == (
        r"@RG\tID:reads\tSM:Byron\tLB:Byron"
    )
    # realign passes a namespace without --sample.
    assert "SM:x" in _read_group(argparse.Namespace(), "x")
