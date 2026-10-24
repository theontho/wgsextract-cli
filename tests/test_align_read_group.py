"""Short-read alignment writes a read group with the sample's own name."""

import argparse

from wgsextract_cli.commands.align import read_group


def _rg(stem, **kwargs):
    return read_group(argparse.Namespace(**kwargs), stem)


def test_read_group_uses_the_sample_or_the_reads_name():
    # The aligners expand the literal \t themselves.
    assert _rg("HG002_R1", sample=None, platform="illumina") == (
        r"@RG\tID:HG002_R1\tSM:HG002\tLB:HG002\tPL:ILLUMINA"
    )
    assert "SM:Byron" in _rg("reads", sample="Byron", platform="illumina")
    # realign passes a namespace without --sample or --platform.
    assert _rg("x", sample=None).endswith(r"SM:x\tLB:x\tPL:ILLUMINA")


def test_different_people_keep_different_sample_names():
    # Dots in a name are meaningful: only the mate marker goes.
    mother = _rg("family.mother_R1", sample=None, platform="illumina")
    father = _rg("family.father_R1", sample=None, platform="illumina")
    assert "SM:family.mother" in mother and "SM:family.father" in father


def test_read_group_platform_follows_platform():
    assert _rg("r", sample=None, platform="ont").endswith("PL:ONT")
    assert _rg("r", sample=None, platform="hifi").endswith("PL:PACBIO")
