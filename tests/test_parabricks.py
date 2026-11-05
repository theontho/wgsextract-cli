"""Parabricks (GPU) alignment and calling: setup checks, and the runs.

A real GPU run can't happen in CI. FakeParabricks stands in for the GPU host:
its Docker runs a pbrun that aligns with the real bwa and minimap2 and calls a
variant at the start of each interval, in a container that sees only the
folders mounted into it.
"""

import os
import platform
import random
import sys

import pytest

from tests.fake_tools import FakeParabricks
from tests.htslib_helpers import (
    needs_tools,
    run_wgsextract,
    snp_sample,
    vcf_calls,
    write_fasta,
    write_read_pairs,
)
from wgsextract_cli.core import parabricks
from wgsextract_cli.core.config import settings
from wgsextract_cli.core.utils import (
    MissingDependencyError,
    UsageError,
    WGSExtractError,
    run_command,
)

posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="Parabricks runs on Linux; fakes run by shebang"
)


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")


@pytest.fixture
def gpu(tmp_path, monkeypatch, linux):
    """A GPU host: its nvidia-smi and Docker come first on PATH."""
    host = FakeParabricks(tmp_path / "gpu", parabricks.DEFAULT_IMAGE)
    monkeypatch.setenv("PATH", f"{host.bin}{os.pathsep}{os.environ['PATH']}")
    return host


@posix_only
@pytest.mark.parametrize(
    ("problem", "message"),
    [
        ("no docker", "Docker not found"),
        ("no driver", "No NVIDIA GPU driver"),
        ("daemon refuses", "docker group"),
        ("no nvidia runtime", "NVIDIA runtime"),
        ("no image", "docker pull"),
    ],
)
def test_setup_problems_are_named(gpu, monkeypatch, problem, message):
    # Only the GPU host's own tools, not this machine's.
    monkeypatch.setenv("PATH", str(gpu.bin))
    if problem == "no docker":
        (gpu.bin / "docker").unlink()
    elif problem == "no driver":
        (gpu.bin / "nvidia-smi").unlink()
    elif problem == "daemon refuses":
        gpu.docker.set(info_error="permission denied")
    elif problem == "no nvidia runtime":
        gpu.docker.set(runtimes='{"runc":{}}')
    else:
        monkeypatch.setitem(settings, "parabricks_image", "pb:not-pulled")
    # A missing setup piece is a missing dependency: exit 3.
    with pytest.raises(MissingDependencyError, match=message):
        parabricks.verify_parabricks()


@posix_only
def test_a_complete_setup_passes(gpu):
    parabricks.verify_parabricks()


def test_parabricks_is_linux_only(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    with pytest.raises(UsageError, match="Linux only"):
        parabricks.verify_parabricks()


def _view(path, ref, *args):
    view = ["samtools", "view", "-T", str(ref), *args, str(path)]
    return run_command(view, capture_output=True).stdout


@pytest.fixture
def genome(tmp_path):
    """Reads, the reference and the output each in their own folder, so the
    container must mount all three. The reference has an ALT contig."""
    rng = random.Random(4)
    seq = "".join(rng.choice("ACGT") for _ in range(6_000))
    alt = "".join(rng.choice("ACGT") for _ in range(500))
    (tmp_path / "ref").mkdir()
    ref = write_fasta(tmp_path / "ref" / "ref.fa", {"chr1": seq, "chr1_KI1_alt": alt})
    (tmp_path / "reads").mkdir()
    return seq, ref


@posix_only
@needs_tools("bwa", "samtools")
@pytest.mark.parametrize("paired", [True, False])
def test_fq2bam_aligns_with_the_read_group_and_alt_file(tmp_path, gpu, genome, paired):
    seq, ref = genome
    r1, r2 = tmp_path / "reads" / "ladan_R1.fq", tmp_path / "reads" / "ladan_R2.fq"
    write_read_pairs(seq, r1, r2, range(0, 5_000, 50))
    out = tmp_path / "out"

    run_wgsextract(
        "align", "--gpu", "--r1", str(r1), *(["--r2", str(r2)] if paired else []),
        "--ref", str(ref), "--outdir", str(out), "--format", "CRAM",
        "--sample", "ladan",
    )  # fmt: skip

    # Mates go into one output, named without the mate marker.
    stem = "ladan" if paired else "ladan_R1"
    cram = out / f"{stem}_aligned.cram"
    header = _view(cram, ref, "-H").splitlines()
    (rg,) = [line for line in header if line.startswith("@RG")]
    # fq2bam needs a platform unit; the sample is the one asked for.
    assert "\tSM:ladan" in rg and f"\tPU:{stem}" in rg
    reads = _view(cram, ref, "-F", "4").splitlines()
    assert len(reads) == 100 * (2 if paired else 1)
    # BWA's ALT list sits beside the reference, as for align --aligner bwa.
    assert (tmp_path / "ref" / "ref.fa.alt").read_text() == "chr1_KI1_alt\n"
    # Parabricks' chromosome list isn't left beside the output.
    assert sorted(p.name for p in out.iterdir() if not p.name.startswith(".")) == [
        f"{stem}_aligned.cram",
        f"{stem}_aligned.cram.crai",
    ]


def _hifi_fastq(seq, path):
    with open(path, "w") as out:
        for i, start in enumerate(range(0, 5_000, 250)):
            read = seq[start : start + 1_000]
            out.write(f"@m{i}\n{read}\n+\n{'I' * len(read)}\n")


@posix_only
@needs_tools("minimap2", "samtools")
@pytest.mark.parametrize(
    ("aligner", "preset"), [("minimap2", "map-hifi"), ("pbmm2", "map-pbmm2")]
)
def test_gpu_hifi_reads_use_parabricks_minimap2(tmp_path, gpu, genome, aligner, preset):
    seq, ref = genome
    reads = tmp_path / "reads" / "movie.fq"
    _hifi_fastq(seq, reads)
    out = tmp_path / "out"

    run_wgsextract(
        "align", "--gpu", "--r1", str(reads), "--ref", str(ref),
        "--outdir", str(out), "--platform", "hifi", "--aligner", aligner,
        "--sample", "HG00733",
    )  # fmt: skip

    bam = out / "movie_aligned.bam"
    header = _view(bam, ref, "-H")
    assert "\tSM:HG00733" in header and "\tPL:PACBIO" in header
    assert len(_view(bam, ref, "-F", "4").splitlines()) == 20
    ((tool, args),) = gpu.runs()
    # contract: pbmm2 stands in as Parabricks' map-pbmm2 preset.
    assert (tool, args[args.index("--preset") + 1]) == ("minimap2", preset)


@posix_only
@needs_tools("minimap2", "samtools")
def test_unaligned_bam_read_groups_without_a_sample_get_one(tmp_path, gpu, genome):
    """PacBio's unaligned BAMs carry read groups without SM, which Parabricks keeps."""
    seq, ref = genome
    lines = ["@HD\tVN:1.6\tSO:unknown", "@RG\tID:movie\tPL:PACBIO"]
    for i, start in enumerate(range(0, 5_000, 250)):
        read = seq[start : start + 1_000]
        lines.append(f"m{i}\t4\t*\t0\t0\t*\t*\t0\t0\t{read}\t{'I' * len(read)}\tRG:Z:movie")  # fmt: skip
    unaligned = tmp_path / "reads" / "movie.hifi_reads.bam"
    sam = tmp_path / "reads" / "movie.sam"
    sam.write_text("\n".join(lines) + "\n")
    run_command(["samtools", "view", "-b", "-o", str(unaligned), str(sam)])
    out = tmp_path / "out"

    run_wgsextract(
        "align", "--gpu", "--r1", str(unaligned), "--ref", str(ref),
        "--outdir", str(out), "--platform", "hifi", "--sample", "HG00732",
    )  # fmt: skip

    (bam,) = out.glob("*_aligned.bam")
    assert "@RG\tID:movie\tPL:PACBIO\tSM:HG00732" in _view(bam, ref, "-H")
    assert len(_view(bam, ref, "-F", "4").splitlines()) == 20


@pytest.mark.parametrize(
    ("options", "message"),
    [
        (["--platform", "clr", "--aligner", "minimap2"], "no minimap2 preset for map-pb"),
        (["--platform", "clr", "--aligner", "pbmm2"], "not CLR"),
        (["--aligner", "minimap2", "--r2", "R2"], "no minimap2 preset for sr"),
        (["--platform", "ont", "--aligner", "minimap2"], "preserve MM/ML"),
    ],
)  # fmt: skip
def test_gpu_alignment_without_a_parabricks_equivalent_is_refused(
    tmp_path, linux, options, message
):
    ref = write_fasta(tmp_path / "ref.fa", {"chr1": "ACGT" * 100})
    for name in ("r1.fq", "r2.fq"):
        (tmp_path / name).write_text("@r\nACGT\n+\nIIII\n")
    options = [str(tmp_path / "r2.fq") if o == "R2" else o for o in options]
    out = tmp_path / "out"
    with pytest.raises(WGSExtractError, match=message):
        run_wgsextract(
            "align", "--gpu", "--r1", str(tmp_path / "r1.fq"), "--ref", str(ref),
            "--outdir", str(out), *options,
        )  # fmt: skip
    assert not out.exists() or not any(out.iterdir())


@posix_only
@needs_tools("samtools", "bcftools", "tabix")
@pytest.mark.parametrize(
    ("command", "options", "read_group", "mode"),
    [
        ("deepvariant", [], "@RG\tID:rg\tSM:S", []),
        ("deepvariant", ["--wes"], "@RG\tID:rg\tSM:S", ["--use-wes-model"]),
        ("deepvariant", ["--pacbio"], "@RG\tID:rg\tSM:S", ["--mode", "pacbio"]),
        ("deepvariant", ["--ont"], "@RG\tID:rg\tSM:S", ["--mode", "ont"]),
        ("deepvariant", ["--model-type", "ONT_R104"], "@RG\tID:rg\tSM:S", ["--mode", "ont"]),
        ("deepvariant", [], "@RG\tID:rg\tSM:S\tPL:ONT", ["--mode", "ont"]),
        ("gatk", [], "@RG\tID:rg\tSM:S", []),
    ],
)  # fmt: skip
def test_gpu_calling_runs_the_parabricks_caller_over_each_region(
    tmp_path, gpu, command, options, read_group, mode
):
    sample = snp_sample(tmp_path, read_group=read_group)
    out = tmp_path / "out"

    run_wgsextract(
        "vcf", command, "--gpu", "--input", sample.bam, "--ref", sample.ref,
        "--outdir", str(out), "-r", "chr1:1000-1100,chr1:2000-2100", *options,
    )  # fmt: skip

    vcf = out / ("deepvariant.vcf.gz" if command == "deepvariant" else "gatk.vcf.gz")
    assert [(c[0], c[1]) for c in vcf_calls(vcf)] == [("chr1", "1000"), ("chr1", "2000")]  # fmt: skip
    ((tool, args),) = gpu.runs()
    assert tool == ("deepvariant" if command == "deepvariant" else "haplotypecaller")
    # contract: the model Parabricks calls with.
    assert [
        a for a in ("--use-wes-model", "--mode", "pacbio", "ont") if a in args
    ] == mode


@posix_only
@needs_tools("samtools", "bcftools", "tabix")
def test_gpu_calling_reads_a_bed_region_from_its_own_folder(tmp_path, gpu):
    """A BED --region is an interval file Parabricks can see, not an -L
    interval, and its folder is mounted into the container."""
    (tmp_path / "sample").mkdir()
    sample = snp_sample(tmp_path / "sample")
    bed = tmp_path / "targets" / "panel.bed"
    bed.parent.mkdir()
    bed.write_text("chr1\t1499\t1600\nchr1\t2199\t2300\n")
    out = tmp_path / "out"

    run_wgsextract(
        "vcf", "deepvariant", "--gpu", "--input", sample.bam, "--ref", sample.ref,
        "--outdir", str(out), "-r", os.path.relpath(bed),
    )  # fmt: skip

    calls = vcf_calls(out / "deepvariant.vcf.gz")
    assert [(c[0], c[1]) for c in calls] == [("chr1", "1500"), ("chr1", "2200")]


def test_gpu_deepvariant_refuses_a_custom_checkpoint(tmp_path, linux):
    with pytest.raises(UsageError, match="checkpoint"):
        run_wgsextract(
            "vcf", "deepvariant", "--gpu", "--input", str(tmp_path / "x.bam"),
            "--checkpoint", str(tmp_path / "model.ckpt"),
        )  # fmt: skip
