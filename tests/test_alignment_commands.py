import os
import shutil
import subprocess
import sys
from argparse import Namespace

import pytest

from wgsextract_cli.commands import align, bam, extract, realign, repair
from wgsextract_cli.commands._extract_helpers import (
    BaseArgs,
    alignments_to_fastq,
    get_base_args,
)
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.utils import WGSExtractError
from wgsextract_cli.core.variant_files import calculate_bam_md5


def _align_args(**overrides):
    values = {
        "aligner": "auto",
        "platform": "illumina",
        "long_read": False,
        "preset": None,
    }
    values.update(overrides)
    return Namespace(**values)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, "bwa"),
        ({"platform": "ont"}, "minimap2"),
        ({"long_read": True}, "minimap2"),
        ({"preset": "sr"}, "minimap2"),
        ({"aligner": "minimap2", "preset": "ccs"}, "minimap2"),
        ({"aligner": "pbmm2", "platform": "hifi", "preset": "map-hifi"}, "pbmm2"),
    ],
)
def test_select_aligner(monkeypatch, overrides, expected):
    monkeypatch.setattr(align, "get_tool_path", lambda _tool: None)
    assert align.select_aligner(_align_args(**overrides)) == expected


@pytest.mark.parametrize(
    "overrides",
    [
        {"aligner": "bwa", "platform": "ont"},
        {"aligner": "bwa", "long_read": True},
        {"aligner": "bwa", "preset": "sr"},
        {"aligner": "pbmm2", "platform": "nanopore"},
        {"aligner": "pbmm2", "preset": "map-ont"},
    ],
)
def test_select_aligner_rejects_nonsensical_combinations(overrides):
    with pytest.raises(WGSExtractError):
        align.select_aligner(_align_args(**overrides))


def test_minimap2_maps_pacbio_preset_names():
    assert align._minimap2_preset(_align_args(preset="ccs")) == "map-hifi"
    assert align._minimap2_preset(_align_args(preset="subread")) == "map-pb"


def test_reads_stem_keeps_inner_dots():
    assert align.reads_stem("/x/HG002.chr20_R1.fastq.gz") == "HG002.chr20_R1"
    assert align.reads_stem("movie.ccs.bam") == "movie"


def test_streamed_alignment_fails_when_an_upstream_stage_fails(tmp_path, monkeypatch):
    drain = [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"]
    monkeypatch.setattr(align, "get_sam_sort_cmd", lambda *_a, **_k: drain)
    index_calls = []
    monkeypatch.setattr(align, "run_command", index_calls.append)

    failing_aligner = [sys.executable, "-c", "import sys; sys.exit(3)"]
    with pytest.raises(WGSExtractError, match="exit 3"):
        align.run_streamed_alignment(
            failing_aligner,
            str(tmp_path / "out.bam"),
            threads="1",
            memory="1G",
            fmt="BAM",
            ref="ref.fa",
            mark_duplicates=False,
        )
    assert index_calls == []


def test_get_base_args_raises_without_input():
    with pytest.raises(WGSExtractError, match="--input"):
        get_base_args(Namespace(input=None))


def _bam_args(path, **overrides):
    values = {
        "input": str(path),
        "outdir": None,
        "ref": None,
        "threads": None,
        "memory": None,
        "region": None,
        "gene": None,
        "cram_version": "3.0",
    }
    values.update(overrides)
    return Namespace(**values)


def _fake_base(outdir, ref=None):
    return BaseArgs("1", "1G", str(outdir), ["-T", ref] if ref else [], ref)


def _no_deps(monkeypatch, module):
    monkeypatch.setattr(module, "verify_dependencies", lambda _tools: None)
    monkeypatch.setattr(module, "log_dependency_info", lambda _tools: None)


@pytest.mark.parametrize(
    ("name", "command"), [("x.cram", bam.cmd_tocram), ("x.bam", bam.cmd_tobam)]
)
def test_conversion_refuses_to_overwrite_input(tmp_path, monkeypatch, name, command):
    source = tmp_path / name
    source.write_bytes(b"data")
    _no_deps(monkeypatch, bam)
    monkeypatch.setattr(bam, "get_base_args", lambda _a: _fake_base(tmp_path))
    calls = []
    monkeypatch.setattr(bam, "run_command", calls.append)

    with pytest.raises(WGSExtractError, match="overwrite"):
        command(_bam_args(source))
    assert calls == []
    assert source.read_bytes() == b"data"


def test_sorting_cram_writes_cram_with_temp_files_in_outdir(tmp_path, monkeypatch):
    source = tmp_path / "HG002.chr20.cram"
    source.write_bytes(b"data")
    outdir = tmp_path / "out"
    outdir.mkdir()
    _no_deps(monkeypatch, bam)
    monkeypatch.setattr(bam, "print_warning", lambda *_a, **_k: None)
    monkeypatch.setattr(
        bam, "get_base_args", lambda _a: _fake_base(outdir, str(tmp_path / "ref.fa"))
    )
    pipelines = []
    monkeypatch.setattr(
        bam, "run_pipeline", lambda stages, **_k: pipelines.append(stages)
    )

    bam.cmd_sort(_bam_args(source, outdir=str(outdir)))

    (view_cmd, sort_cmd) = pipelines[0]
    assert sort_cmd[sort_cmd.index("-o") + 1] == str(outdir / "HG002.chr20_sorted.cram")
    assert sort_cmd[sort_cmd.index("-O") + 1] == "CRAM"
    assert sort_cmd[sort_cmd.index("-T") + 1].startswith(str(outdir))


def test_fastq_extraction_avoids_dev_null_and_system_temp(tmp_path, monkeypatch):
    from wgsextract_cli.commands import _extract_helpers

    pipelines = []

    def fake_run_pipeline(stages, **kwargs):
        pipelines.append((stages, kwargs))

    monkeypatch.setattr(_extract_helpers, "run_pipeline", fake_run_pipeline)
    alignments_to_fastq(
        "in.bam", "r1.fq.gz", "r2.fq.gz", threads="1", memory="1G", temp_parent=tmp_path
    )

    stages, kwargs = pipelines[0]
    argv = [str(arg) for stage in stages for arg in stage]
    assert "/dev/null" not in argv
    assert kwargs["stdout"] == subprocess.DEVNULL
    sort_cmd = stages[1]
    assert sort_cmd[sort_cmd.index("-T") + 1].startswith(str(tmp_path))


def test_extract_custom_requires_region(tmp_path, monkeypatch):
    _no_deps(monkeypatch, extract)
    monkeypatch.setattr(extract, "get_base_args", lambda _a: _fake_base(tmp_path))
    with pytest.raises(WGSExtractError, match="--region"):
        extract.cmd_custom(_bam_args(tmp_path / "x.bam"))


def test_mito_vcf_calls_haploid_and_cleans_temp_bam_on_failure(tmp_path, monkeypatch):
    _no_deps(monkeypatch, extract)
    monkeypatch.setattr(extract, "print_warning", lambda *_a, **_k: None)
    monkeypatch.setattr(extract, "get_chr_name", lambda *_a: "chrM")
    monkeypatch.setattr(
        extract, "get_base_args", lambda _a: _fake_base(tmp_path, "ref.fa")
    )

    def fake_run_command(cmd, **_kwargs):
        if "-o" in cmd:
            open(cmd[cmd.index("-o") + 1], "wb").close()

    pipelines = []

    def failing_pipeline(stages, **_kwargs):
        pipelines.append(stages)
        raise WGSExtractError("Variant calling failed in bcftools mpileup (exit 1)")

    monkeypatch.setattr(extract, "run_command", fake_run_command)
    monkeypatch.setattr(extract, "run_pipeline", failing_pipeline)

    with pytest.raises(WGSExtractError, match="mpileup"):
        extract.cmd_mito_vcf(_bam_args(tmp_path / "x.cram"))

    call_cmd = pipelines[0][1]
    assert call_cmd[call_cmd.index("--ploidy") + 1] == "1"
    assert not (tmp_path / "x_MT_temp.bam").exists()


def test_realign_rejects_bad_aligner_before_extracting(tmp_path, monkeypatch):
    source = tmp_path / "x.bam"
    source.write_bytes(b"data")
    extracted = []
    monkeypatch.setattr(realign, "alignments_to_fastq", extracted.append)
    args = Namespace(
        input=str(source),
        outdir=str(tmp_path),
        threads=None,
        memory=None,
        r1=None,
        r2=None,
        force_extract=False,
        no_preflight=True,
        keep_fastq=False,
        aligner="bwa",
        platform="ont",
        long_read=False,
        preset=None,
    )
    with pytest.raises(WGSExtractError, match="short-read"):
        realign.run(args)
    assert extracted == []


@pytest.mark.skipif(get_tool_path("samtools") is None, reason="samtools missing")
def test_repair_bam_streams_through_real_samtools(tmp_path):
    sam = tmp_path / "ftdna.sam"
    sam.write_text(
        "@HD\tVN:1.6\tSO:unsorted\n"
        "@SQ\tSN:chr1\tLN:1000\n"
        "read 1\t0\tchr1\t100\t60\t10M\t*\t0\t0\tAAAAAAAAAA\t##########\n",
        encoding="utf-8",
    )
    source = tmp_path / "ftdna.bam"
    subprocess.run(
        [shutil.which("samtools") or get_tool_path("samtools"), "view", "-b"]
        + ["-o", str(source), str(sam)],
        check=True,
    )

    repair.repair_bam(
        Namespace(
            input=str(source), outdir=None, output=None, _explicit_dests={"input"}
        )
    )

    output = tmp_path / "ftdna_repaired.bam"
    view = subprocess.run(
        [shutil.which("samtools") or get_tool_path("samtools"), "view", str(output)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert view.stdout.startswith("read:1\t")


def test_repair_bam_reports_unreadable_input(tmp_path):
    if get_tool_path("samtools") is None:
        pytest.skip("samtools missing")
    source = tmp_path / "broken.bam"
    source.write_bytes(b"not a bam")
    with pytest.raises(WGSExtractError, match="repair failed"):
        repair.repair_bam(
            Namespace(
                input=str(source), outdir=None, output=None, _explicit_dests={"input"}
            )
        )
    assert not os.path.exists(tmp_path / "broken_repaired.bam")


@pytest.mark.parametrize(("r2", "expect_samblaster"), [(None, False), ("r2", True)])
def test_bwa_marks_duplicates_only_for_paired_reads(
    tmp_path, monkeypatch, r2, expect_samblaster
):
    ref = tmp_path / "ref.fa"
    for path in (tmp_path / "r1.fq", tmp_path / "r2.fq", ref, tmp_path / "ref.fa.bwt"):
        path.write_text("x")
    _no_deps(monkeypatch, align)
    monkeypatch.setattr(align, "print_warning", lambda *_a, **_k: None)
    monkeypatch.setattr(align, "get_tool_path", lambda tool: tool)
    monkeypatch.setattr(align, "run_command", lambda *_a, **_k: None)
    pipelines = []
    monkeypatch.setattr(
        align, "run_pipeline", lambda stages, **_k: pipelines.append(stages)
    )
    args = Namespace(
        r1=str(tmp_path / "r1.fq"),
        r2=str(tmp_path / r2) + ".fq" if r2 else None,
        ref=str(ref),
        input=None,
        outdir=str(tmp_path),
        format="BAM",
        threads=None,
        memory=None,
    )

    align.align_bwa(args)

    assert (["samblaster"] in pipelines[0]) is expect_samblaster


def test_gene_lookup_uses_detected_build_and_rejects_t2t(monkeypatch):
    from wgsextract_cli.commands._extract_helpers import resolve_region_or_gene
    from wgsextract_cli.core import reference_resolver

    class FakeLibrary:
        def __init__(self, *_args, **_kwargs):
            self.build = "chm13v2.0"

    monkeypatch.setattr(reference_resolver, "ReferenceLibrary", FakeLibrary)
    args = Namespace(region=None, gene="BRCA1", input="x.bam")
    with pytest.raises(WGSExtractError, match="hg19/hg38"):
        resolve_region_or_gene(args, "/refs/hg38_named_but_t2t.fa.gz")


def _write_indexed_fasta(path, contigs):
    path.write_text("".join(f">{name}\nACGT\n" for name in contigs))
    (path.parent / (path.name + ".fai")).write_text(
        "".join(f"{name}\t{length}\t0\t4\t5\n" for name, length in contigs.items())
    )


def test_realign_cram_input_reference_matches_library_signature(tmp_path, monkeypatch):
    from wgsextract_cli.core.config import settings

    genomes = tmp_path / "genomes"
    genomes.mkdir()
    _write_indexed_fasta(genomes / "other.fa", {"chr1": 100})
    _write_indexed_fasta(genomes / "match.fa", {"chr1": 200, "chrM": 16569})
    header = "@SQ\tSN:chr1\tLN:200\n@SQ\tSN:chrM\tLN:16569\n"
    monkeypatch.setitem(settings, "reference_library", str(tmp_path))
    monkeypatch.setattr(
        realign, "calculate_bam_md5", lambda path: calculate_bam_md5("", header=header)
    )
    opt = realign._input_cram_reference(Namespace(input="in.cram", input_ref=None))
    assert opt == ["-T", str(genomes / "match.fa")]
    assert (
        realign._input_cram_reference(Namespace(input="in.bam", input_ref=None)) == []
    )


def test_realign_cram_input_without_matching_reference_errors(tmp_path, monkeypatch):
    from wgsextract_cli.core.config import settings

    monkeypatch.setitem(settings, "reference_library", str(tmp_path))
    monkeypatch.setattr(realign, "calculate_bam_md5", lambda path: "0" * 32)
    with pytest.raises(WGSExtractError, match="--input-ref"):
        realign._input_cram_reference(Namespace(input="in.cram", input_ref=None))


@pytest.mark.parametrize(
    ("stem", "expected"),
    [
        ("HG00096.hg19-mini_R1", "HG00096.hg19-mini"),
        ("sample_1", "sample"),
        ("sample_R1_001", "sample_001"),
        ("sample.2_001", "sample_001"),
        ("R1_sample", "sample"),
        ("sample_read1.trimmed", "sample.trimmed"),
        ("sample", "sample"),
    ],
)
def test_pair_stem_drops_only_the_mate_marker(stem, expected):
    from wgsextract_cli.core.genome_library import pair_stem

    assert pair_stem(stem) == expected
