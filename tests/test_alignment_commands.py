import math
import os
import random
import shutil
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from tests.fake_tools import only_tools, python_tool, require_bare_name_fakes
from tests.htslib_helpers import (
    needs_htslib,
    needs_tools,
    run_wgsextract,
    sam_to_bam,
    snp_sample,
    vcf_calls,
    write_fasta,
    write_read_pairs,
)
from wgsextract_cli.commands import align, repair
from wgsextract_cli.commands._extract_helpers import alignments_to_fastq
from wgsextract_cli.core.config import settings
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.utils import WGSExtractError, run_command


def _align_args(**overrides):
    values = {
        "aligner": "auto",
        "platform": "illumina",
        "long_read": False,
        "preset": None,
    }
    values.update(overrides)
    return Namespace(**values)


def _short_reads(work, pairs=120, duplicates=20):
    """A 20 kb chr1 and 100 bp FR pairs from it, the first ``duplicates``
    fragments sequenced twice."""
    rng = random.Random(7)
    genome = "".join(rng.choice("ACGT") for _ in range(20_000))
    (work / "ref").mkdir(parents=True)
    ref = write_fasta(work / "ref" / "ref.fa", {"chr1": genome})
    starts = rng.sample(range(0, 19_600, 50), pairs)
    r1, r2 = work / "reads_R1.fq", work / "reads_R2.fq"
    write_read_pairs(genome, r1, r2, starts + starts[:duplicates], frag=400)
    return Namespace(genome=genome, ref=str(ref), r1=str(r1), r2=str(r2))


def _align(reads, out, *options, paired=True):
    mates = ["--r2", reads.r2] if paired else []
    run_wgsextract(
        "align", "--r1", reads.r1, *mates, "--ref", reads.ref, "--outdir", str(out),
        *options,
    )  # fmt: skip
    (bam,) = out.glob("*_aligned.bam")
    return str(bam)


def _samtools(*args):
    return run_command(["samtools", *args], capture_output=True).stdout


def _programs(bam):
    """The @PG lines of an alignment's header."""
    return [line for line in _samtools("view", "-H", bam).splitlines() if "@PG" in line]


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
def test_select_aligner(overrides, expected):
    assert align.select_aligner(_align_args(**overrides)) == expected


@pytest.mark.parametrize("installed", [True, False])
def test_auto_takes_pbmm2_for_pacbio_reads_when_installed(fake_pixi, installed):
    if installed:
        python_tool(fake_pixi.host_bin, "pbmm2", "")
    chosen = align.select_aligner(_align_args(platform="hifi"))
    assert chosen == ("pbmm2" if installed else "minimap2")


@pytest.mark.parametrize(
    "overrides",
    [
        {"aligner": "bwa", "platform": "ont"},
        {"aligner": "bwa", "long_read": True},
        {"aligner": "bwa", "preset": "sr"},
        {"aligner": "bwa-mem2", "platform": "hifi"},
        {"aligner": "bwa-mem2", "gpu": True},
        {"aligner": "pbmm2", "platform": "nanopore"},
        {"aligner": "pbmm2", "preset": "map-ont"},
    ],
)
def test_select_aligner_rejects_nonsensical_combinations(overrides):
    with pytest.raises(WGSExtractError):
        align.select_aligner(_align_args(**overrides))


def test_reads_stem_keeps_inner_dots():
    assert align.reads_stem("/x/HG002.chr20_R1.fastq.gz") == "HG002.chr20_R1"
    assert align.reads_stem("movie.ccs.bam") == "movie"


@needs_tools("bwa", "samtools", "samblaster")
@pytest.mark.parametrize(
    ("paired", "samblaster", "marker", "duplicates"),
    [
        (False, True, "CL:samtools markdup", 20),  # samblaster aborts on SE
        (True, True, "ID:SAMBLASTER", 40),
        (True, False, "CL:samtools markdup", 40),
    ],
)
def test_bwa_always_marks_duplicates(
    tmp_path, request, paired, samblaster, marker, duplicates
):
    reads = _short_reads(tmp_path)
    if not samblaster:
        only_tools(request, "bwa", "samtools")

    bam = _align(reads, tmp_path / "out", paired=paired)

    assert any(marker in line for line in _programs(bam))
    assert _samtools("view", "-c", "-f", "0x400", bam).strip() == str(duplicates)


@needs_tools("bwa", "samtools")
@pytest.mark.parametrize("threads", ["1", "3"])
def test_bwa_batch_size_does_not_depend_on_threads(tmp_path, threads):
    bam = _align(_short_reads(tmp_path), tmp_path / "out", "--threads", threads)
    (bwa,) = [line for line in _programs(bam) if "\tID:bwa\t" in line]
    # contract: a fixed -K batch makes the alignments independent of -t
    assert f" -K 100000000 -t {threads} " in bwa


@needs_tools("minimap2", "samtools")
@pytest.mark.parametrize(
    ("options", "preset"),
    [
        ((), "sr"),
        (("--long-read",), "map-pb"),
        (("--platform", "ont"), "map-ont"),
        (("--platform", "hifi"), "map-hifi"),
        (("--preset", "ccs"), "map-hifi"),
        (("--preset", "subread"), "map-pb"),
    ],
)
def test_minimap2_preset_follows_platform_and_marks_only_short_reads(
    tmp_path, options, preset
):
    reads = _short_reads(tmp_path)
    bam = _align(
        reads, tmp_path / "out", "--aligner", "minimap2", *options, paired=False
    )

    (minimap2,) = [line for line in _programs(bam) if "\tID:minimap2\t" in line]
    # contract: the preset sets minimap2's scoring for the platform's errors
    assert f"CL:minimap2 -ax {preset} " in minimap2
    flagged = int(_samtools("view", "-c", "-f", "0x400", bam))
    assert (flagged > 0) == (preset == "sr")


_FAKE_PBMM2 = """\
import json, shutil, sys
with open({log!r}, "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
shutil.copyfile({aligned!r}, sys.argv[4])
"""


@needs_tools("samtools")
def test_pbmm2_aligns_hifi_reads_with_the_ccs_preset_and_sample(tmp_path, monkeypatch):
    require_bare_name_fakes()
    reads = _short_reads(tmp_path)
    ccs = sam_to_bam(
        ["@HD\tVN:1.6\tSO:unknown", "m1/1/ccs\t4\t*\t0\t0\t*\t*\t0\t0\tACGT\tIIII"],
        tmp_path / "HG00733.ccs.bam",
    )
    aligned = tmp_path / "pbmm2_output.bam"
    run_command(["samtools", "import", "-0", reads.r1, "-o", str(aligned)])
    log = tmp_path / "pbmm2.log"
    bindir = tmp_path / "bin"
    python_tool(bindir, "pbmm2", _FAKE_PBMM2.format(log=str(log), aligned=str(aligned)))
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    out = tmp_path / "out"

    run_wgsextract(
        "align", "--r1", ccs, "--ref", reads.ref, "--outdir", str(out),
        "--platform", "hifi",
    )  # fmt: skip

    (argv,) = [line for line in log.read_text().splitlines() if '"align"' in line]
    # contract: pbmm2's preset and the read group's sample
    assert '"--preset", "CCS"' in argv and '"--sample", "HG00733"' in argv
    assert (out / "HG00733_aligned.bam.bai").exists()


@needs_tools("samtools")
def test_streamed_alignment_fails_when_an_upstream_stage_fails(tmp_path):
    # It emits a whole SAM, so the sorter completes a BAM that must not survive.
    sam = "@SQ\\tSN:c\\tLN:100\\nr\\t4\\t*\\t0\\t0\\t*\\t*\\t0\\t0\\tA\\tI\\n"
    failing_aligner = [
        sys.executable, "-c", f"import sys; sys.stdout.write('{sam}'); sys.exit(3)"
    ]  # fmt: skip
    with pytest.raises(WGSExtractError, match="exit 3"):
        align.run_streamed_alignment(
            failing_aligner,
            str(tmp_path / "out.bam"),
            threads="1",
            memory="100M",
            fmt="BAM",
            ref="ref.fa",
            duplicates=None,
        )
    assert os.listdir(tmp_path) == []


@pytest.mark.parametrize(
    ("tool", "budget", "sort_memory"),
    [
        ("bwa-mem2", "2G", "256M"),
        ("bwa-mem2", "128M", "128M"),
        ("bwa-mem2", "0.125G", "0.125G"),
        ("bwa", "2G", "2G"),
    ],
)
def test_mem2_sort_cap_respects_smaller_explicit_budgets(tool, budget, sort_memory):
    assert align.short_read_sort_memory(tool, budget) == sort_memory


def _coordinate_sorted_pairs(work, pairs=200):
    """A coordinate-sorted BAM of ``pairs`` proper pairs p000, p001, ... in
    coordinate order."""
    read = f"100M\t=\t{{mate}}\t{{tlen}}\t{'ACGT' * 25}\t{'I' * 100}"
    lines = ["@HD\tVN:1.6\tSO:unsorted", "@SQ\tSN:chr1\tLN:100000"]
    for i in range(pairs):
        pos = 1000 + 400 * i
        lines += [
            f"p{i:03d}\t99\tchr1\t{pos}\t60\t" + read.format(mate=pos + 300, tlen=400),
            f"p{i:03d}\t147\tchr1\t{pos + 300}\t60\t"
            + read.format(mate=pos, tlen=-400),
        ]
    return sam_to_bam(lines, work / "pairs.bam")


def _names(fastq):
    return Path(fastq).read_text().splitlines()[::4]


@needs_tools("samtools")
def test_fastq_extraction_shuffles_pairs_out_of_coordinate_order(tmp_path):
    bam = _coordinate_sorted_pairs(tmp_path)
    temp_parent = tmp_path / "work"
    temp_parent.mkdir()
    r1, r2 = tmp_path / "r1.fq", tmp_path / "r2.fq"

    alignments_to_fastq(
        bam, r1, r2, threads="3", memory="100M", temp_parent=temp_parent
    )

    names = _names(r1)
    assert names == _names(r2)
    assert sorted(names) == [f"@p{i:03d}" for i in range(200)]
    # Standard (randomising) collate: fast mode's coordinate order skews
    # aligners' insert-size estimates.
    assert names != sorted(names)
    assert os.listdir(temp_parent) == []


@needs_tools("samtools")
@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0, reason="needs POSIX permissions"
)
def test_fastq_extraction_keeps_its_temporaries_in_temp_parent(tmp_path):
    bam = _coordinate_sorted_pairs(tmp_path)
    temp_parent = tmp_path / "read-only"
    temp_parent.mkdir(mode=0o500)
    with pytest.raises(PermissionError):
        alignments_to_fastq(
            bam, tmp_path / "r1.fq", tmp_path / "r2.fq", threads="1",
            memory="100M", temp_parent=temp_parent,
        )  # fmt: skip


@needs_tools("samtools")
@pytest.mark.parametrize("mode", ["files", "files-se", "stream"])
def test_paired_extraction_warns_about_reads_it_leaves_out(tmp_path, caplog, mode):
    from wgsextract_cli.commands._extract_helpers import paired_fastq_stream
    from wgsextract_cli.core.utils import run_pipeline

    read = "\t*\t0\t0\t*\t*\t0\t0\tACGT\tIIII"
    bam = sam_to_bam(
        [
            "@HD\tVN:1.6\tSO:unsorted",
            f"p\t77{read}",
            f"p\t141{read}",
            f"se\t4{read}",
            f"both\t205{read}",
        ],  # fmt: skip
        tmp_path / "x.bam",
    )
    se = tmp_path / "se.fq"
    common = {"threads": "1", "memory": "100M", "temp_parent": tmp_path}
    if mode == "stream":
        with (
            paired_fastq_stream(bam, **common) as stages,
            (tmp_path / "pairs.fq").open("wb") as pairs,
        ):
            run_pipeline(stages, stdout=pairs)
    else:
        alignments_to_fastq(
            bam, tmp_path / "r1.fq", tmp_path / "r2.fq",
            out_other=se if mode == "files-se" else None, **common,
        )  # fmt: skip
    if mode == "files-se":
        assert se.read_text().count("\n") == 8 and "neither" not in caplog.text
    else:
        assert "2 primary reads flagged neither first nor second" in caplog.text


@needs_tools("samtools")
def test_streamed_extraction_orders_pairs_like_file_extraction(tmp_path):
    from wgsextract_cli.commands._extract_helpers import paired_fastq_stream
    from wgsextract_cli.core.utils import run_pipeline

    bam = _coordinate_sorted_pairs(tmp_path)
    common = {"threads": "2", "memory": "100M", "temp_parent": tmp_path}
    alignments_to_fastq(bam, tmp_path / "r1.fq", tmp_path / "r2.fq", **common)
    with (
        paired_fastq_stream(bam, **common) as stages,
        (tmp_path / "pairs.fq").open("wb") as pairs,
    ):
        run_pipeline(stages, stdout=pairs)

    assert _names(tmp_path / "pairs.fq")[::2] == _names(tmp_path / "r1.fq")


def test_collate_buckets_fit_the_memory_budget(tmp_path):
    from wgsextract_cli.commands._extract_helpers import _collate_buckets

    cram = tmp_path / "x.cram"
    with open(cram, "wb") as f:
        f.truncate(8 * 1024**3)  # 30x CRAM: ~560 GB of records in memory
    assert _collate_buckets(str(cram), "12", "1300M") == 64
    assert _collate_buckets(str(cram), "4", "1G") == 140
    assert _collate_buckets(str(cram), "1", "512M") == 200
    assert _collate_buckets(str(tmp_path / "missing.bam"), "1", "1G") == 64


def test_streamed_collate_uses_file_extractions_bucket_count(tmp_path):
    from wgsextract_cli.commands._extract_helpers import (
        _collate_buckets,
        paired_fastq_stream,
    )

    bam = tmp_path / "large.bam"
    with bam.open("wb") as output:
        output.truncate(5 * 1024**3)
    for threads, memory in (("8", "2G"), ("2", "512M"), ("1", "100M")):
        with paired_fastq_stream(
            bam, threads=threads, memory=memory, temp_parent=tmp_path
        ) as stages:
            collate = stages[1]
        # contract: collate's pair order depends on its bucket count, and
        # streamed realignment must reproduce file extraction's alignments
        assert collate[collate.index("-n") + 1] == str(
            _collate_buckets(bam, threads, memory)
        )


def test_collate_working_memory_holds_the_largest_bucket_of_records(tmp_path):
    from wgsextract_cli.commands._extract_helpers import collate_working_memory

    bam = tmp_path / "x.bam"
    with bam.open("wb") as output:
        output.truncate(64 * 1024**3)  # 200 buckets at 6 x 1G
    mib = 1024**2
    whole_genome = collate_working_memory(
        bam, "6", "1G", records=800_000_000, max_record_bytes=600
    )
    # 4M records per bucket, each in a 1 KiB buffer plus its overhead.
    assert 4_000_000 * 1152 < whole_genome - 64 * mib < 4_020_000 * 1152
    assert collate_working_memory(
        bam, "6", "1G", records=800_000_000, max_record_bytes=1025
    ) > 1.8 * (whole_genome - 64 * mib)
    assert (
        collate_working_memory(
            bam, "6", "1G", records=400_000_000, max_record_bytes=600
        )
        < whole_genome
    )
    # Without a count or sample: the file-size expansion of one bucket.
    expected = math.ceil(64 * 1024**3 * 30 / 200) + 64 * mib
    assert collate_working_memory(bam, "6", "1G") == expected


@needs_htslib
def test_mito_vcf_calls_haploid_genotypes(tmp_path):
    sample = snp_sample(tmp_path, contig="chrM")
    out = tmp_path / "out"
    run_wgsextract(
        "extract", "mito-vcf", "-i", sample.bam, "--ref", sample.ref, "-o", str(out)
    )
    assert vcf_calls(out / "s_MT.vcf.gz") == [(*call[:4], "1") for call in sample.calls]
    assert sorted(os.listdir(out)) == ["s_MT.vcf.gz", "s_MT.vcf.gz.tbi"]


@needs_htslib
def test_mito_vcf_failure_removes_its_temporary_bam(tmp_path):
    sample = snp_sample(tmp_path, contig="chrM")
    # A corrupt reference index: mpileup fails, after the chrM reads are
    # extracted.
    Path(sample.ref + ".fai").write_text("chrM\tnot-a-length\n")
    out = tmp_path / "out"
    with pytest.raises(WGSExtractError, match="bcftools mpileup"):
        run_wgsextract(
            "extract", "mito-vcf", "-i", sample.bam, "--ref", sample.ref, "-o", str(out)
        )
    assert os.listdir(out) == []


@needs_tools("samtools")
def test_realign_rejects_bad_aligner_before_extracting(tmp_path):
    sample = snp_sample(tmp_path)
    out = tmp_path / "out"
    with pytest.raises(WGSExtractError, match="short-read"):
        run_wgsextract(
            "realign", "-i", sample.bam, "--ref", sample.ref, "-o", str(out),
            "--aligner", "bwa", "--platform", "ont",
        )  # fmt: skip
    assert not out.exists() or os.listdir(out) == []


@needs_tools("samtools")
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


@needs_tools("samtools")
def test_markdup_fallback_marks_a_duplicate_pair(tmp_path):
    """Two identical pairs from the aligner: the second is flagged a duplicate."""
    seq, qual = "ACGT" * 25, "I" * 100
    sam = tmp_path / "aligned.sam"
    sam.write_text(
        "@SQ\tSN:chr1\tLN:10000\n"
        + "".join(
            f"p{i}\t99\tchr1\t100\t60\t100M\t=\t400\t400\t{seq}\t{qual}\n"
            f"p{i}\t147\tchr1\t400\t60\t100M\t=\t100\t-400\t{seq}\t{qual}\n"
            for i in range(2)
        )
    )
    out = str(tmp_path / "out.bam")
    align.run_streamed_alignment(
        # The aligner's stand-in: prints the SAM (Windows has no `cat`).
        [
            sys.executable,
            "-c",
            "import shutil, sys; shutil.copyfileobj(open(sys.argv[1], 'rb'), sys.stdout.buffer)",
            str(sam),
        ],
        out,
        threads="1",
        memory="100M",
        fmt="BAM",
        ref="unused.fa",
        duplicates="markdup",
    )
    flags = run_command(
        ["samtools", "view", out], capture_output=True
    ).stdout.splitlines()
    assert sorted(bool(int(r.split("\t")[1]) & 0x400) for r in flags) == [
        False,
        False,
        True,
        True,
    ]


@needs_tools("samtools")
def test_gene_lookup_uses_the_inputs_build_and_rejects_t2t(tmp_path):
    from wgsextract_cli.commands._extract_helpers import region_or_gene

    t2t = {"chr1": 248387328, "chrX": 154259566, "chrY": 62460029, "chrM": 16569}
    bam = sam_to_bam(
        ["@HD\tVN:1.6\tSO:coordinate"]
        + [f"@SQ\tSN:{name}\tLN:{length}" for name, length in t2t.items()],
        tmp_path / "x.bam",
    )
    ref = tmp_path / "hg38_named_but_t2t.fa"
    ref.write_text("".join(f">{name}\nA\n" for name in t2t))
    (tmp_path / "hg38_named_but_t2t.fa.fai").write_text(
        "".join(f"{name}\t{length}\t0\t1\t2\n" for name, length in t2t.items())
    )
    with pytest.raises(WGSExtractError, match="hg19/hg38"):
        region_or_gene(None, "BRCA1", bam, str(ref))


def _paired_cram(work, genome):
    """Read pairs from ``genome``, aligned and encoded as a CRAM against
    ``work/encoded/ref.fa``."""
    reads = Namespace(
        ref=str(write_fasta(work / "encoded" / "ref.fa", {"chr1": genome})),
        r1=str(work / "in_R1.fq"),
        r2=str(work / "in_R2.fq"),
    )
    rng = random.Random(3)
    starts = [rng.randrange(0, len(genome) - 400) for _ in range(100)]
    write_read_pairs(genome, Path(reads.r1), Path(reads.r2), starts, frag=400)
    bam = _align(reads, work / "aligned")
    cram = str(work / "in.cram")
    run_command(["samtools", "view", "-C", "-T", reads.ref, "-o", cram, bam])
    return cram, reads.ref


@needs_tools("bwa", "samtools")
@pytest.mark.parametrize("decoder", ["library", "target"])
def test_realign_decodes_a_cram_input_with_the_fasta_it_was_encoded_with(
    tmp_path, monkeypatch, decoder
):
    rng = random.Random(5)
    genome = "".join(rng.choice("ACGT") for _ in range(20_000))
    cram, encoded = _paired_cram(tmp_path, genome)
    library = tmp_path / "library"
    write_fasta(library / "genomes" / "other.fa", {"chr1": genome[:5000]})
    if decoder == "library":
        shutil.copy(encoded, library / "genomes" / "match.fa")
        shutil.copy(encoded + ".fai", library / "genomes" / "match.fa.fai")
        # Realign onto another build whose chr1 can't decode the CRAM: the
        # reads' sequence is chr2 there.
        unrelated = "".join(rng.choice("ACGT") for _ in range(20_000))
        target = write_fasta(
            tmp_path / "target" / "ref.fa", {"chr1": unrelated, "chr2": genome}
        )
    else:
        target = Path(encoded)
    Path(encoded).unlink()  # decoding can only use a copy
    if decoder == "target":
        write_fasta(target, {"chr1": genome})
    monkeypatch.setitem(settings, "reference_library", str(library))
    out = tmp_path / "out"

    run_wgsextract("realign", "-i", cram, "--ref", str(target), "-o", str(out))

    (bam,) = out.glob("*.bam")
    mapped = _samtools("view", "-F", "0x904", str(bam)).splitlines()
    assert len(mapped) == 200
    contig = "chr2" if decoder == "library" else "chr1"
    assert {r.split("\t")[2] for r in mapped} == {contig}
    fastq = tmp_path.glob("in_R?.fq")
    inputs = sorted(line for f in fastq for line in f.read_text().splitlines()[1::4])
    recovered = _samtools("fastq", "-F", "0x900", str(bam)).splitlines()[1::4]
    assert sorted(recovered) == inputs


@needs_tools("samtools")
def test_realign_cram_input_without_matching_reference_errors(tmp_path, monkeypatch):
    rng = random.Random(5)
    genome = "".join(rng.choice("ACGT") for _ in range(3000))
    sample = snp_sample(tmp_path)
    library = tmp_path / "library"
    write_fasta(library / "genomes" / "other.fa", {"chr1": genome[:1000]})
    target = write_fasta(tmp_path / "target" / "ref.fa", {"chr1": genome})
    monkeypatch.setitem(settings, "reference_library", str(library))
    with pytest.raises(WGSExtractError, match="--input-ref"):
        run_wgsextract(
            "realign", "-i", sample.cram, "--ref", str(target),
            "-o", str(tmp_path / "out"),
        )  # fmt: skip


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
