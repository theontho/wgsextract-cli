import argparse
import gzip
import logging
import shutil
import sys
from collections import Counter
from pathlib import Path

import pytest

from wgsextract_cli.cli.parser import build_parser
from wgsextract_cli.cli.platform_support import verify_platform_support
from wgsextract_cli.commands import methylation
from wgsextract_cli.core import dependency_checks, pixi_tools
from wgsextract_cli.core.utils import WGSExtractError

LENGTHS = {"chr20": 64_444_167, "chr21": 46_709_983}


def parse(*argv: str) -> argparse.Namespace:
    return build_parser()[0].parse_args(["methylation", "pileup", *argv])


@pytest.fixture
def no_config(monkeypatch):
    for key in (
        "methylation_context",
        "methylation_5hmc",
        "methylation_combine_strands",
    ):
        monkeypatch.delitem(methylation.settings, key, raising=False)


def test_default_command_is_cpg_5mc_combined(no_config):
    options = methylation.pileup_options(parse("-i", "r.bam"))
    intervals = methylation.region_intervals("chr20:10000001-11000000", LENGTHS)
    command = methylation.modkit_command(
        "r.bam", "out.gz", "ref.fa", options, "8", intervals, "regions.bed"
    )
    assert command[:8] == [
        "modkit", "pileup", "r.bam", "out.gz", "--ref", "ref.fa",
        "--modified-bases", "5mC",
    ]  # fmt: skip
    assert command[command.index("--threads") + 1] == "8"
    assert command[command.index("--io-threads") + 1] == "4"
    assert {"--cpg", "--combine-strands", "--bgzf"} <= set(command)
    # modkit's --region is 0-based, half-open.
    assert command[-2:] == ["--region", "chr20:10000000-11000000"]


def test_options_5hmc_all_context_and_several_regions(no_config):
    options = methylation.pileup_options(
        parse("-i", "r.bam", "--5hmc", "--context", "all")
    )
    intervals = methylation.region_intervals("chr20:5-10, chr21", LENGTHS)
    assert intervals == [("chr20", 4, 10), ("chr21", 0, LENGTHS["chr21"])]
    command = methylation.modkit_command(
        "r.bam", "o.gz", "ref.fa", options, "2", intervals, "regions.bed"
    )
    modified = command.index("--modified-bases")
    assert command[modified + 1 : modified + 3] == ["5mC", "5hmC"]
    assert "--cpg" not in command and "--combine-strands" not in command
    assert command[-2:] == ["--include-bed", "regions.bed"]


@pytest.mark.parametrize("region", ["chr22:1-5", "chr20:10-5", "chr20:1-99999999"])
def test_bad_regions_are_refused(region):
    with pytest.raises(WGSExtractError):
        methylation.region_intervals(region, LENGTHS)


def test_cli_overrides_config(monkeypatch):
    monkeypatch.setitem(methylation.settings, "methylation_5hmc", True)
    monkeypatch.setitem(methylation.settings, "methylation_combine_strands", False)
    assert methylation.pileup_options(parse()) == methylation.PileupOptions(
        "cpg", True, False
    )
    assert methylation.pileup_options(
        parse("--no-5hmc", "--combine-strands")
    ) == methylation.PileupOptions("cpg", False, True)

    monkeypatch.setitem(methylation.settings, "methylation_context", "chg")
    with pytest.raises(WGSExtractError, match="methylation_context"):
        methylation.pileup_options(parse())


def test_combine_strands_needs_cpg(no_config):
    # The default quietly gives way; an explicit request is an error.
    assert not methylation.pileup_options(parse("--context", "all")).combine_strands
    with pytest.raises(WGSExtractError, match="needs --context cpg"):
        methylation.pileup_options(parse("--context", "all", "--combine-strands"))


def write_sam(path: Path, tags: list[str], platform: str = "ONT") -> Path:
    lines = [
        "@HD\tVN:1.6\tSO:coordinate",
        "@SQ\tSN:chr20\tLN:1000",
        f"@RG\tID:a\tPL:{platform}",
    ]
    for i, tag in enumerate(tags):
        extra = f"\t{tag}" if tag else ""
        lines.append(
            f"r{i}\t0\tchr20\t{i + 1}\t60\t4M\t*\t0\t0\tACGT\tIIII\tRG:Z:a{extra}"
        )
    # A secondary alignment is not sampled.
    lines.append(
        "s\t256\tchr20\t1\t60\t4M\t*\t0\t0\tACGT\tIIII\tMM:Z:C+m?,0;\tML:B:C,200"
    )
    path.write_text("\n".join(lines) + "\n")
    return path


needs_samtools = pytest.mark.skipif(
    shutil.which("samtools") is None, reason="samtools required"
)


@needs_samtools
def test_modification_tags_are_sampled(tmp_path):
    simplex = "MM:Z:C+m?,0;\tML:B:C,200"
    duplex = "MM:Z:C+m?,0;G-m?,0;\tML:B:C,200,10\tdx:i:1"
    jasmine = "MM:Z:C+m?,0;C+h?;G-h?;A+a.;T-a.,0;\tML:B:C,200,250"
    reads = [simplex, "", "Mm:Z:C+m,0;\tMl:B:C,9", duplex, jasmine]
    sample = methylation.sample_modification_tags(
        str(write_sam(tmp_path / "r.sam", reads)), None
    )
    assert sample == methylation.TagSample(
        reads=5, tagged=4, opposite_strand=2, duplex=1
    )
    untagged = write_sam(tmp_path / "u.sam", ["", "MM:Z:C+m?,0;"])
    assert methylation.sample_modification_tags(
        str(untagged), None
    ) == methylation.TagSample(2, 0)


@needs_samtools
def test_unreadable_input_is_an_error_not_missing_tags(tmp_path):
    broken = tmp_path / "broken.bam"
    broken.write_bytes(b"not a bam")
    with pytest.raises(WGSExtractError, match="samtools could not read"):
        methylation.sample_modification_tags(str(broken), None)


@needs_samtools
def test_reference_without_fai_is_indexed(tmp_path):
    fasta = tmp_path / "ref.fa"
    fasta.write_text(">chr20\nACGTACGT\n")
    methylation.index_fasta(str(fasta))
    assert (tmp_path / "ref.fa.fai").read_text().startswith("chr20\t8\t")


@pytest.mark.parametrize(
    ("platform", "reason"),
    [
        ("ont", "wgsextract realign` keeps them"),
        ("pacbio", "Jasmine or primrose"),
        (None, "short reads never do"),
    ],
)
def test_reads_without_tags_are_refused(platform, reason):
    with pytest.raises(WGSExtractError, match=f"None of 200 .*MM/ML.*{reason}"):
        methylation.check_modification_tags(methylation.TagSample(200, 0), platform)


def test_partly_tagged_reads_warn(caplog):
    with caplog.at_level(logging.WARNING):
        methylation.check_modification_tags(methylation.TagSample(200, 150), "ont")
        assert not caplog.records
        methylation.check_modification_tags(methylation.TagSample(200, 20), "ont")
    assert "Only 20 of 200" in caplog.text


@pytest.mark.parametrize(
    ("platform", "duplex", "source"),
    [
        ("pacbio", 0, "Jasmine writes them for 5hmC .* only its 5mC model"),
        ("ont", 200, "Dorado writes them for duplex reads"),
        (None, 200, "Dorado writes them for duplex reads"),
        (None, 0, "Remove those modification calls"),
    ],
)
def test_opposite_strand_reads_are_refused_with_their_source(platform, duplex, source):
    sample = methylation.TagSample(200, 200, opposite_strand=200, duplex=duplex)
    with pytest.raises(WGSExtractError, match=f"All 200 .*skips such.*{source}"):
        methylation.check_modification_tags(sample, platform)


def test_some_opposite_strand_reads_warn(caplog):
    with caplog.at_level(logging.WARNING):
        methylation.check_modification_tags(
            methylation.TagSample(200, 200, opposite_strand=30, duplex=30), "ont"
        )
    assert "30 of 200 sampled reads" in caplog.text


@needs_samtools
def test_pacbio_5mc_only_reads_pass(tmp_path):
    primrose = "MM:Z:C+m?,0,1;\tML:B:C,200,5"
    reads = write_sam(tmp_path / "pb.sam", [primrose] * 3, platform="PACBIO")
    sample = methylation.sample_modification_tags(str(reads), None)
    assert sample == methylation.TagSample(3, 3)
    methylation.check_modification_tags(sample, "pacbio")


@pytest.mark.parametrize("platform", ["ont", "pacbio", None])
def test_long_read_platforms_are_accepted(platform):
    methylation.require_modification_platform(platform)


@pytest.mark.parametrize("platform", ["illumina", "ultima", "dnbseq"])
def test_short_read_platforms_are_refused(platform):
    with pytest.raises(WGSExtractError, match=f"looks like {platform} short reads"):
        methylation.require_modification_platform(platform)


def test_missing_modkit_names_its_pixi_environment(tmp_path, monkeypatch):
    reads = tmp_path / "reads.bam"
    reads.write_bytes(b"")
    monkeypatch.setattr(methylation, "require_supported_platform", lambda: None)
    monkeypatch.setattr(dependency_checks, "require_native_tool", lambda tool: None)
    monkeypatch.setattr(pixi_tools, "_locked_package_available", lambda *args: True)
    monkeypatch.setattr(
        dependency_checks, "check_dependencies", lambda tools: [methylation.MODKIT]
    )
    with pytest.raises(WGSExtractError, match=r"modkit.*-e modkit"):
        methylation.cmd_pileup(parse("-i", str(reads)))


def test_windows_is_refused_before_inputs_are_resolved(monkeypatch):
    args = parse("--input", "reads.bam")
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(WGSExtractError, match="not available on Windows.*WSL2"):
        verify_platform_support(args)


def test_bedmethyl_summary(tmp_path):
    def row(code, start, valid, percent, modified):
        return (
            f"chr20\t{start}\t{start + 1}\t{code}\t{valid}\t.\t{start}\t{start + 1}"
            f"\t255,0,0\t{valid}\t{percent}\t{modified}\t{valid - modified}"
            "\t0\t0\t0\t0\t0\n"
        )

    path = tmp_path / "x.bedmethyl.gz"
    with gzip.open(path, "wt") as out:
        out.write(row("m", 1, 10, "90.00", 9))
        out.write(row("m", 3, 30, "10.00", 3))
        out.write(row("m", 5, 2, "100.00", 2))  # below the coverage floor
        out.write(row("m", 7, 20, "50.00", 10))
        out.write(row("h", 1, 10, "0.00", 0))
    m, h = methylation.summarize_bedmethyl(str(path))
    assert (m.modification, m.sites, m.summary_sites) == ("5mC", 4, 3)
    assert m.weighted_percent == pytest.approx(36.67, abs=0.01)  # 22 / 60
    assert m.mean_site_percent == pytest.approx(50.0)
    assert m.median_coverage == 20
    assert m.high_sites_percent == m.low_sites_percent == pytest.approx(100 / 3)
    assert (h.modification, h.weighted_percent) == ("5hmC", 0.0)


@pytest.mark.parametrize(
    ("counts", "median"), [({}, 0), ({5: 1}, 5), ({5: 1, 9: 1}, 7), ({1: 2, 8: 1}, 1)]
)
def test_coverage_median_from_counts(counts, median):
    assert methylation._counter_median(Counter(counts)) == median
