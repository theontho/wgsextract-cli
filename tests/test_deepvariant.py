"""CPU DeepVariant: Google's image through Docker, or run_deepvariant on PATH.

The image is too big for CI, so a fake Docker runs fake DeepVariant tools
(tests/fake_tools.py) that plan, stage and call like the real ones, on real
reads with real samtools and bcftools.
"""

import gzip
import json
import os
import re
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.disk_volumes import GIB, Volumes
from tests.fake_tools import (
    FakeDeepVariant,
    FakeDocker,
    fake_nvidia_smi,
    require_bare_name_fakes,
    set_gpus,
)
from tests.htslib_helpers import (
    require_runnable,
    run_wgsextract,
    sam_to_bam,
    snp_sample,
    vcf_calls,
    write_fasta,
)
from wgsextract_cli import api
from wgsextract_cli.commands.vcf import deepvariant
from wgsextract_cli.core import alignment_memory, containers
from wgsextract_cli.core.dependencies import clear_tool_caches, get_tool_path
from wgsextract_cli.core.utils import (
    MissingDependencyError,
    UsageError,
    WGSExtractError,
    run_command,
)

# Found before a test replaces PATH.
CORE_TOOLS = {tool: shutil.which(tool) for tool in ("samtools", "bcftools", "tabix")}
STAGES = ["make_examples", "call_variants", "postprocess_variants"]
ONT = "@RG\tID:rg\tSM:S\tPL:ONT\tDS:basecall_model=dna_r10.4.1_e8.2_400bps_sup@v5.0.0"
ULTIMA = (
    "@RG\tID:rg\tSM:S\tPL:ULTIMA\tFO:TGCA",
    f"\ttp:B:c{',0' * 100}\tt0:Z:{'I' * 100}",
)


@pytest.fixture
def linux_x86(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(deepvariant.platform, "machine", lambda: "x86_64")


@pytest.fixture
def dv(tmp_path, monkeypatch, linux_x86):
    """Reads, Google's image in a fake Docker, 64 GiB of free RAM, and no
    run_deepvariant on PATH."""
    require_bare_name_fakes()
    require_runnable("samtools", "bcftools", "tabix")
    (tmp_path / "reads").mkdir()
    fake = FakeDeepVariant(
        tmp_path / "deepvariant", CORE_TOOLS["samtools"], CORE_TOOLS["bcftools"]
    )
    docker = FakeDocker(tmp_path / "docker" / "bin", fake.bin)
    docker.add_image(deepvariant.DEFAULT_IMAGE)
    monkeypatch.setenv(
        "PATH", f"{docker.root.parent / 'bin'}{os.pathsep}{os.environ['PATH']}"
    )
    monkeypatch.setattr(alignment_memory, "available_memory", lambda: 64 * GIB)
    # A failed container's OOM event comes at once here.
    monkeypatch.setattr(containers, "OOM_EVENT_GRACE", 0.5)
    clear_tool_caches()
    return SimpleNamespace(
        sample=snp_sample(tmp_path / "reads"),
        deepvariant=fake,
        docker=docker,
        out=tmp_path / "out",
        tmp=tmp_path,
    )


@pytest.fixture
def arm64(dv, monkeypatch):
    """Linux ARM64, with a native engine and the native image built."""
    monkeypatch.setattr(deepvariant.platform, "machine", lambda: "aarch64")
    dv.docker.set(arch="aarch64")
    dv.docker.add_image(deepvariant.DEFAULT_ARM64_IMAGE, platform="linux/arm64")
    return dv


def call(dv, *options, reads=None, out=None, threads="4"):
    run_wgsextract(
        "vcf", "deepvariant", "--input", str(reads or dv.sample.bam),
        "--ref", dv.sample.ref, "--outdir", str(out or dv.out), "--threads", threads,
        *options,
    )  # fmt: skip


def rerun(dv, *options, **kwargs):
    """Forget earlier runs, stop failing and call again."""
    dv.deepvariant.fail(None)
    dv.deepvariant.clear()
    dv.docker.clear()
    call(dv, *options, **kwargs)


def other_sample(dv, *args, **kwargs):
    work = dv.tmp / "other"
    work.mkdir()
    return snp_sample(work, *args, **kwargs)


def visible(out):
    """What a run left in ``out`` for the user, beside its step journal."""
    return sorted(
        p.name
        for p in out.iterdir()
        if not p.name.startswith(".") and not p.name.endswith(".steps.json")
    )


def dv_flags(dv):
    """The options run_deepvariant was last planned with."""
    return dv.deepvariant.runs("run_deepvariant")[-1]["opts"]


def stage_runs(dv):
    return [r for r in dv.docker.runs() if r["command"][:1] == ["/bin/bash"]]


def as_cram(sample, version, name=None):
    """``sample``'s reads as an indexed CRAM ``version``."""
    cram = str(Path(sample.bam).with_name(name or f"v{version}.cram"))
    run_command(
        ["samtools", "view", "-C", "--output-fmt-option", f"version={version}",
         "-T", sample.ref, "-o", cram, sample.bam]
    )  # fmt: skip
    run_command(["samtools", "index", "--csi" if name else "-c", cram])
    return cram


def read_names(alignment, ref, *regions):
    view = [
        "samtools",
        "view",
        *(["-M"] if regions else []),
        "-T",
        ref,
        alignment,
        *regions,
    ]
    out = run_command(view, capture_output=True).stdout
    return sorted({line.split("\t")[0] for line in out.splitlines()})


# Docker setup -------------------------------------------------------------


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        ("no docker", "Docker not found.*docker pull google/deepvariant"),
        ("no daemon", "permission denied.*docker group"),
        ("no image", "image isn't downloaded: docker pull google/deepvariant"),
    ],
)
def test_missing_docker_setup_is_named(dv, monkeypatch, setup, message):
    if setup == "no docker":
        tools = {str(Path(path).parent) for path in CORE_TOOLS.values()}
        monkeypatch.setenv("PATH", os.pathsep.join(tools))
    elif setup == "no daemon":
        dv.docker.set(info_error="permission denied while trying to connect")
    else:
        dv.docker.set(images={})
    with pytest.raises(WGSExtractError, match=message):
        call(dv)
    assert dv.docker.runs() == []


def test_windows_is_refused(dv, monkeypatch):
    get_tool_path("run_deepvariant")  # Looked up as on Linux: not found.
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(WGSExtractError, match="Windows"):
        call(dv)


def test_the_image_calls_reads_from_folders_it_mounts(dv):
    """The reference, reads, checkpoint and output are in separate folders,
    each mounted into the container (the fake fails on any other path)."""
    reads = dv.tmp / "bam" / "s.bam"
    reads.parent.mkdir()
    for suffix in ("", ".bai"):
        shutil.copy(dv.sample.bam + suffix, f"{reads}{suffix}")
    model = dv.tmp / "model" / "model.ckpt"
    model.parent.mkdir()
    Path(f"{model}.index").write_text("weights")

    call(
        dv, "--model-type", "WES", "--checkpoint", str(model), "-r", "chr1", reads=reads
    )

    assert vcf_calls(dv.out / "deepvariant.vcf.gz") == dv.sample.calls
    assert visible(dv.out) == ["deepvariant.vcf.gz", "deepvariant.vcf.gz.tbi"]
    assert not (dv.out / deepvariant.WORK_DIRECTORY).exists()
    flags = dv_flags(dv)
    # contract: the chosen model, checkpoint, region and one shard per thread.
    assert (flags["model_type"], flags["customized_model"]) == ("WES", str(model))
    assert (flags["regions"], flags["num_shards"]) == ("chr1", "4")
    assert [r["command"][:2] for r in stage_runs(dv)] == [["/bin/bash", "-c"]] * 3


def test_local_run_deepvariant_runs_without_docker_our_python_or_stdin(
    dv, monkeypatch, open_stdin
):
    monkeypatch.setenv("PATH", f"{dv.deepvariant.bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("PYTHONPATH", str(dv.tmp / "our-site-packages"))
    clear_tool_caches()
    call(dv)
    assert vcf_calls(dv.out / "deepvariant.vcf.gz") == dv.sample.calls
    assert dv.docker.runs() == []
    assert dv.deepvariant.stages() == STAGES
    assert "regions" not in dv_flags(dv)
    assert dv_flags(dv)["model_type"] == "WGS"


def test_detected_ont_runs_the_ont_model_with_its_own_temporary_files(dv):
    reads = other_sample(dv, ONT)
    call(dv, reads=reads.bam)
    # contract: ONT reads get DeepVariant's ONT model.
    assert dv_flags(dv)["model_type"] == "ONT_R104"
    plan, *stages = dv.docker.runs()
    assert {r["image"] for r in [plan, *stages]} == {deepvariant.DEFAULT_IMAGE}
    work = dv.out / deepvariant.WORK_DIRECTORY
    for stage in stages:
        assert stage["env"]["TMPDIR"] == stage["env"]["HOME"] == str(work / ".tmp")
    # Planning, which a --dry-run does beside a running stage, has its own.
    assert Path(plan["env"]["TMPDIR"]).parent == work
    assert plan["env"]["TMPDIR"] != str(work / ".tmp")


@pytest.mark.parametrize(
    ("selection", "model"),
    [
        (["--pacbio"], "PACBIO"),
        (["--wes"], "WES"),
        (["--model-type", "HYBRID_PACBIO_ILLUMINA"], "HYBRID_PACBIO_ILLUMINA"),
        (["--wes", "--model-type", "PACBIO"], "PACBIO"),
        (["--pacbio", "--model-type", "WES"], "PACBIO"),
    ],
)
def test_model_options_choose_the_model(dv, selection, model):
    call(dv, *selection)
    # contract: the model DeepVariant calls with.
    assert dv_flags(dv)["model_type"] == model


def test_detected_ultima_reads_run_the_ultima_image(dv):
    dv.docker.add_image(deepvariant.DEFAULT_ULTIMA_IMAGE)
    sample = other_sample(dv, ULTIMA[0], tags=ULTIMA[1])
    call(dv, reads=sample.bam)
    # contract: Ultima reads get the Ultima model, in its own image.
    assert dv_flags(dv)["model_type"] == "ULTIMA"
    assert {r["image"] for r in dv.docker.runs()} == {deepvariant.DEFAULT_ULTIMA_IMAGE}


def test_ont_conflicts_with_other_model_options(dv):
    with pytest.raises(WGSExtractError, match="cannot be combined"):
        call(dv, "--ont", "--wes")
    assert dv.docker.runs() == []


@pytest.mark.parametrize("selection", [[], ["--ont"], ["--model-type", "ONT_R104"]])
def test_apple_arm_refuses_ont_including_automatic_selection(
    dv, monkeypatch, selection
):
    reads = other_sample(dv, ONT)
    monkeypatch.setattr(deepvariant.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(sys, "platform", "darwin")
    with pytest.raises(WGSExtractError, match="ONT_R104.*not qualified on ARM64"):
        call(dv, *selection, reads=reads.bam)
    assert dv.docker.runs() == []


# CRAM 3.1 staging -----------------------------------------------------------


@pytest.mark.parametrize("reads", ["bam", "3.0", "3.1", "3.1 named .bam"])
def test_only_cram_31_is_staged_as_indexed_cram_30(dv, caplog, reads):
    """The images' htslib can't decode CRAM 3.1 (the fake fails on it), so
    those reads, whatever their name, are copied to CRAM 3.0 first."""
    sample = dv.sample
    if reads == "bam":
        alignment = sample.bam
    else:
        name = "reads.bam" if reads.endswith(".bam") else None
        alignment = as_cram(sample, reads.split()[0], name)
    before = Path(alignment).read_bytes()
    with caplog.at_level("INFO"):
        call(dv, reads=alignment)

    (examples,) = dv.deepvariant.runs("make_examples")
    staged = reads.startswith("3.1")
    assert examples["cram_version"] == (None if reads == "bam" else [3, 0])
    assert (Path(examples["reads"]).name == "reads.cram") == staged
    assert examples["names"] == read_names(sample.bam, sample.ref)
    assert caplog.text.count("Transcoding CRAM") == staged
    assert vcf_calls(dv.out / "deepvariant.vcf.gz") == sample.calls
    assert not (dv.out / deepvariant.WORK_DIRECTORY).exists()
    assert Path(alignment).read_bytes() == before


@pytest.mark.parametrize(
    ("region", "expected"),
    [
        (None, []),
        ("chr1:900-1000", ["chr1:900-1000"]),
        ("chr1", []),
        ("chr1:600-700 chr1:2000-2100", ["chr1:600-700", "chr1:2000-2100"]),
        ("chr1:900-1000 chr1:900-950", ["chr1:900-1000"]),
        ("bed", ["chr1:900-1000"]),
        ("bed.gz", ["chr1:900-1000"]),
        ("bed-mixed", ["chr1:900-1000", "chr1:2000-2100"]),
        ("bedpe", ["chr1:600-2100"]),  # One span from both ends.
    ],
)
def test_staging_keeps_the_reads_of_the_selected_regions(dv, region, expected):
    cram = as_cram(dv.sample, "3.1")
    if region and region.startswith("bed"):
        selection = dv.tmp / ("selection." + region.split("-")[0])
        rows = (
            "chr1\t599\t700\tchr1\t1999\t2100\n"
            if region == "bedpe"
            else "chr1\t899\t1000\nchr1\t899\t950\n"
        )
        if region == "bed.gz":
            with gzip.open(selection, "wt") as stream:
                stream.write(rows)
        else:
            selection.write_text(rows)
        region = str(selection) + (" chr1:2000-2100" if region == "bed-mixed" else "")
    call(dv, *(["-r", region] if region else []), reads=cram)
    (examples,) = dv.deepvariant.runs("make_examples")
    assert examples["cram_version"] == [3, 0]
    assert examples["names"] == read_names(cram, dv.sample.ref, *expected)


@pytest.mark.parametrize(
    ("selection", "reads"),
    [
        ("HLA-A*01:01:01:01", range(49)),
        ("HLA-A*01:01:01:01:1,001-2,000", range(10, 20)),
        ("HLA-A*01:01:01:01:1,001", [10]),
    ],
)
def test_colon_contigs_select_like_deepvariant_does(dv, selection, reads):
    """A contig name with colons is a whole contig; a range after it is
    read like DeepVariant (Nucleus) reads it."""
    contig = "HLA-A*01:01:01:01"
    (dv.tmp / "hla").mkdir()
    ref = write_fasta(dv.tmp / "hla" / "hla.fa", {contig: "ACGT" * 1250})
    lines = ["@HD\tVN:1.6\tSO:unsorted", f"@SQ\tSN:{contig}\tLN:5000"]
    for i in range(49):  # r{i} covers 100i+1 to 100i+50.
        lines.append(
            f"r{i}\t0\t{contig}\t{100 * i + 1}\t60\t50M\t*\t0\t0\t"
            f"{'ACGT' * 12}AC\t{'I' * 50}"
        )
    hla = SimpleNamespace(
        ref=str(ref), bam=sam_to_bam(lines, dv.tmp / "hla" / "hla.bam")
    )
    cram = as_cram(hla, "3.1")
    run_wgsextract(
        "vcf", "deepvariant", "--input", cram, "--ref", str(ref),
        "--outdir", str(dv.out), "-r", selection,
    )  # fmt: skip
    (examples,) = dv.deepvariant.runs("make_examples")
    assert examples["names"] == sorted(f"r{i}" for i in reads)


def test_unknown_contigs_are_dropped_from_a_selection_but_not_alone(dv):
    cram = as_cram(dv.sample, "3.1")
    bed = dv.tmp / "capture.bed"
    bed.write_text("chrUn_stray\t0\t50\nchr1\t899\t1000\nchr1\t949\t1100\n")
    call(dv, "-r", f"{bed} chrUn_stray:1-10", reads=cram)
    (examples,) = dv.deepvariant.runs("make_examples")
    assert examples["names"] == read_names(cram, dv.sample.ref, "chr1:900-1100")

    bed.write_text("chrUn_stray\t0\t50\n")
    dv.docker.clear()
    for region, message in [
        (str(bed), "covers no reference bases"),
        ("chrUn_stray:1-10", "covers no reference bases"),
        ("chr1 chrUn_stray", "Invalid interval"),
    ]:
        with pytest.raises(WGSExtractError, match=message):
            call(dv, "-r", region, reads=cram, out=dv.tmp / "refused")
    assert dv.docker.runs() == []


def test_a_relative_region_bed_in_its_own_folder_reaches_the_image(dv, monkeypatch):
    """Regression: a BED beside neither the reads, the reference nor the
    output wasn't mounted, so DeepVariant's image couldn't open it."""
    (dv.tmp / "targets").mkdir()
    (dv.tmp / "targets" / "panel.bed").write_text("chr1\t899\t1100\n")
    monkeypatch.chdir(dv.tmp / "targets")
    call(dv, "-r", "panel.bed")
    assert vcf_calls(dv.out / "deepvariant.vcf.gz") == dv.sample.calls
    assert dv_flags(dv)["regions"] == str(dv.tmp / "targets" / "panel.bed")


def test_missing_region_bed_is_reported_before_docker_runs(dv):
    with pytest.raises(WGSExtractError, match="Invalid DeepVariant region selection"):
        call(dv, "-r", str(dv.tmp / "missing.bed"))
    assert dv.docker.runs() == []


@pytest.fixture
def big_cram(dv, monkeypatch):
    """A CRAM 3.1 that looks 1 GiB big (staging the whole of it needs ~2
    GiB) beside an output folder with 20 GiB free."""
    cram = as_cram(dv.sample, "3.1")
    dv.out.mkdir()
    volumes = Volumes(monkeypatch)
    volumes.sizes[Path(cram)] = GIB
    volumes.free[dv.out] = 20
    return SimpleNamespace(path=cram, volumes=volumes)


def test_staging_needs_disk_for_the_whole_input_or_the_selected_slices(dv, big_cram):
    big_cram.volumes.sizes[Path(big_cram.path)] = 16 * GIB
    with pytest.raises(WGSExtractError, match=r"Insufficient free disk.*~32\.00 GiB"):
        call(dv, reads=big_cram.path)
    assert not list(dv.out.rglob("reads.cram"))
    assert dv.deepvariant.runs("make_examples") == []

    # A region needs only its CRAM slices, and at least 64 MiB.
    call(dv, "-r", "chr1:900-1000", reads=big_cram.path)
    assert dv.deepvariant.runs("make_examples")
    big_cram.volumes.free[dv.out] = 63 / 1024
    with pytest.raises(WGSExtractError, match=r"~0\.06 GiB"):
        call(dv, "-r", "chr1:900-1100", reads=big_cram.path, out=dv.out / "again")


@pytest.mark.parametrize("compressed", [False, True])
def test_capture_staging_counts_the_selected_slices_not_the_genome_fraction(
    dv, big_cram, compressed
):
    data = f"0\t1\t100\t100\t0\t{16 * GIB}\n".encode()
    Path(f"{big_cram.path}.crai").write_bytes(
        gzip.compress(data) if compressed else data
    )
    with pytest.raises(WGSExtractError, match=r"~32\.00 GiB"):
        call(dv, "-r", "chr1:10-20", reads=big_cram.path)


@pytest.mark.parametrize(
    "index_data",
    [
        None,
        b"",
        b"malformed\n",
        b"0 1 20 100 0 50\nbad trailing row\n",
        b"9 1 20 100 0 50\n",
        gzip.compress(b"0 1 20 100 0 50\n", mtime=0)[:-4],
        b"\x1f\x8b\x08\x00" + bytes(6) + b"\x07",
    ],
)
def test_an_unreadable_crai_bounds_staging_by_the_whole_input(dv, big_cram, index_data):
    """Each of these CRAIs would say 50 bytes (so 64 MiB) were selected."""
    index = Path(f"{big_cram.path}.crai")
    index.unlink()
    if index_data is not None:
        index.write_bytes(index_data)
    big_cram.volumes.free[dv.out] = 1.5
    with pytest.raises(WGSExtractError, match=r"~2\.00 GiB"):
        call(dv, "-r", "chr1:10", reads=big_cram.path)


def test_crai_slices_follow_the_header_order_and_shared_slices_count_once(dv):
    """Slices of 1 GiB units: chrM and chr20 share the first one."""
    work = dv.tmp / "two"
    work.mkdir()
    seqs = {"chrM": "ACGT" * 250, "chr20": "TGCA" * 250}
    ref = write_fasta(work / "ref.fa", seqs)
    lines = ["@HD\tVN:1.6\tSO:coordinate", "@SQ\tSN:chrM\tLN:1000"]
    lines += ["@SQ\tSN:chr20\tLN:1000", "r1\t0\tchrM\t1\t60\t4M\t*\t0\t0\tACGT\tIIII"]
    sample = SimpleNamespace(ref=str(ref), bam=sam_to_bam(lines, work / "two.bam"))
    cram = as_cram(sample, "3.1")
    Path(f"{cram}.crai").write_text(
        f"0\t1\t100\t100\t10\t{200 * GIB}\n"
        f"1\t1\t100\t100\t10\t{200 * GIB}\n"
        f"1\t101\t20\t500\t10\t{300 * GIB}\n"
        f"1\t121\t20\t900\t10\t{400 * GIB}\n"
    )
    for region, needs in [
        ("chr20:11-20 chrM:11-20", "400.00"),
        ("chr20:101-120", "600.00"),
    ]:
        with pytest.raises(WGSExtractError, match=rf"needs ~{needs} GiB"):
            run_wgsextract(
                "vcf", "deepvariant", "--input", cram, "--ref", str(ref),
                "--outdir", str(dv.out), "-r", region,
            )  # fmt: skip


@pytest.mark.parametrize("failure", [None, "view", "index", "caller", "cancel"])
def test_staged_reads_are_kept_for_a_rerun_and_removed_after_success(
    dv, caplog, failure
):
    cram = as_cram(dv.sample, "3.1")
    before = Path(cram).read_bytes()
    staged = dv.out / deepvariant.WORK_DIRECTORY / "reads" / "reads.cram"
    if failure in ("view", "index"):
        # A folder where samtools writes makes it fail.
        blocker = staged if failure == "view" else Path(f"{staged}.crai")
        blocker.mkdir(parents=True)
    elif failure == "caller":
        dv.deepvariant.fail("make_examples")
    elif failure == "cancel":
        dv.deepvariant.fail("call_variants", interrupt=os.getpid())
    if failure:
        with pytest.raises(
            KeyboardInterrupt if failure == "cancel" else WGSExtractError
        ):
            call(dv, reads=cram)
        assert visible(dv.out) == []
        assert staged.parent.is_dir()  # Kept for the rerun.
        if failure in ("view", "index"):
            blocker.rmdir()
    caplog.clear()
    with caplog.at_level("INFO"):
        rerun(dv, reads=cram)
    # A rerun stages the reads again only if staging didn't finish.
    assert ("Transcoding CRAM" in caplog.text) == (failure in (None, "view", "index"))
    if failure == "cancel":
        assert dv.deepvariant.stages() == ["call_variants", "postprocess_variants"]
    else:
        (examples,) = dv.deepvariant.runs("make_examples")
        assert examples["reads"] == str(staged)
    assert not staged.parent.parent.exists()
    assert Path(cram).read_bytes() == before
    assert visible(dv.out) == ["deepvariant.vcf.gz", "deepvariant.vcf.gz.tbi"]


# Linux ARM64 ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("engine", "image", "message"),
    [
        ("x86_64", "linux/arm64", "native ARM64 Docker engine"),
        ("aarch64", "linux/amd64", "must be linux/arm64"),
        ("aarch64", "", "must be linux/arm64"),
    ],
)
def test_arm_refuses_emulation_before_reading_input(arm64, engine, image, message):
    arm64.docker.set(arch=engine)
    arm64.docker.add_image(deepvariant.DEFAULT_ARM64_IMAGE, platform=image)
    with pytest.raises(WGSExtractError, match=message):
        call(arm64, reads=arm64.tmp / "missing.bam")
    assert arm64.docker.runs() == []


def test_arm_runs_the_native_image_past_its_entrypoint_and_local_launchers(
    arm64, monkeypatch
):
    """Community ARM images' entrypoints auto-select INT8/BF16 models, and a
    run_deepvariant on PATH isn't audited: each stage runs its own binary in
    the local image, staged reads included."""
    monkeypatch.setenv(
        "PATH", f"{arm64.deepvariant.bin}{os.pathsep}{os.environ['PATH']}"
    )
    monkeypatch.setitem(
        deepvariant.settings, "deepvariant_image", "local/native@sha256:abc"
    )
    arm64.docker.add_image("local/native@sha256:abc", platform="linux/arm64")
    clear_tool_caches()
    call(arm64, "-r", "chr1:600-700 chr1:2000-2100", reads=as_cram(arm64.sample, "3.1"))

    plan, *stages = arm64.docker.runs()
    assert {r["image"] for r in [plan, *stages]} == {"local/native@sha256:abc"}
    # contract: never pull, and bypass the image's entrypoint.
    assert {r["pull"] for r in [plan, *stages]} == {"--pull=never"}
    assert plan["entrypoint"] == "/opt/deepvariant/bin/run_deepvariant"
    assert [s["entrypoint"] for s in stages] == ["/bin/bash"] * 3
    (examples,) = arm64.deepvariant.runs("make_examples")
    assert Path(examples["reads"]).name == "reads.cram"
    assert examples["tmpdir"] == str(arm64.out / deepvariant.WORK_DIRECTORY / ".tmp")
    assert visible(arm64.out) == ["deepvariant.vcf.gz", "deepvariant.vcf.gz.tbi"]


@pytest.mark.parametrize(
    "selection",
    [
        *(["--model-type", m] for m in
          ("ULTIMA", "WES", "PACBIO", "ONT_R104", "HYBRID_PACBIO_ILLUMINA")),
        ["--pacbio"],
        ["--wes"],
        ["--ont"],
        ["--checkpoint", "custom/model.ckpt"],
    ],
)  # fmt: skip
def test_arm_unqualified_models_fail_before_docker_or_input_work(arm64, selection):
    with pytest.raises(WGSExtractError, match="not qualified.*only the default WGS"):
        call(arm64, *selection, reads=arm64.tmp / "missing.bam")
    assert arm64.docker.runs(None) == []


@pytest.mark.parametrize(("model", "reads"), [("ONT_R104", (ONT,)), ("ULTIMA", ULTIMA)])
def test_arm_detected_non_wgs_reads_are_refused(arm64, model, reads):
    sample = other_sample(arm64, *reads[:1], tags="".join(reads[1:]))
    with pytest.raises(WGSExtractError, match=f"{model}.*not qualified"):
        call(arm64, reads=sample.bam)
    assert arm64.docker.runs() == []


def test_arm_missing_native_image_gives_build_instructions(arm64):
    arm64.docker.set(images={})
    with pytest.raises(
        WGSExtractError, match="packaging/deepvariant-arm64/build.sh"
    ) as error:
        call(arm64)
    assert "docker pull" not in str(error.value)


def test_arm_run_reports_the_deepvariant_preflight(arm64, caplog):
    with caplog.at_level("INFO"):
        call(arm64)
    assert "Preflight deepvariant" in caplog.text
    assert "DeepVariant WGS uses sequential stages" in caplog.text
    assert "postprocess_variants: 4 workers" in caplog.text


# Resources ------------------------------------------------------------------


@pytest.mark.parametrize("batch_size", ["0", "-1"])
def test_invalid_batch_size_is_refused(batch_size):
    with pytest.raises(WGSExtractError, match="must be positive"):
        run_wgsextract("vcf", "deepvariant", "-i", "s.bam", "--batch-size", batch_size)


def test_batch_size_is_not_silently_ignored_by_parabricks(dv):
    with pytest.raises(UsageError, match="--batch-size doesn't apply to Parabricks"):
        call(dv, "--batch-size", "256", "--gpu")


def test_batch_size_reaches_call_variants_without_changing_shards(dv):
    call(dv, "--batch-size", "256")
    (inference,) = dv.deepvariant.runs("call_variants")
    assert inference["opts"]["batch_size"] == "256"
    (examples,) = dv.deepvariant.runs("parallel")
    assert examples["tasks"] == ["0", "1", "2", "3"]


@pytest.fixture
def genome_fai(dv):
    """The reference's index says chr1 is 3 Gbp: a whole genome's worth of
    postprocess_variants partitions."""
    fai = Path(f"{dv.sample.ref}.fai")
    fai.write_text(fai.read_text().replace("\t3000\t", "\t3000000000\t", 1))


def postprocess(dv):
    """postprocess_variants' last --cpus and --num_partitions."""
    opts = dv.deepvariant.runs("postprocess_variants")[-1]["opts"]
    return opts["cpus"], opts.get("num_partitions")


def test_postprocess_is_bounded_by_available_memory(
    dv, genome_fai, monkeypatch, caplog
):
    def call_with(gib, *options):
        monkeypatch.setattr(alignment_memory, "available_memory", lambda: gib * GIB)
        call(dv, *options, out=dv.tmp / f"{gib}{''.join(options)}")

    caplog.set_level("INFO")
    call_with(64)
    assert postprocess(dv) == ("4", None)  # 4 workers fit in 64 GiB.

    call_with(8)
    cpus, partitions = postprocess(dv)
    assert cpus == "4" and int(partitions) > 4
    assert "Splitting DeepVariant postprocess_variants" in caplog.text

    call_with(3)
    assert postprocess(dv)[0] == "2"
    assert "Limiting DeepVariant postprocess_variants to 2 of 4" in caplog.text

    call_with(3, "--wes")
    assert postprocess(dv) == ("4", None)  # run_deepvariant runs WES serially.

    dv.docker.clear()
    with pytest.raises(WGSExtractError, match="Not enough memory.*smaller --region"):
        call_with(1)
    assert dv.docker.runs() == []


@pytest.mark.parametrize(
    ("image", "helpfull", "partitions"),
    [
        ("google/deepvariant:1.8.0", None, False),
        ("google/deepvariant:1.9.0-gpu", None, True),
        ("local/dv@sha256:abc", "  --num_partitions: ...", True),
        ("local/dv@sha256:abc", "  --cpus: ...", False),
    ],
)
def test_num_partitions_is_used_only_when_deepvariant_has_it(
    dv, genome_fai, monkeypatch, caplog, image, helpfull, partitions
):
    monkeypatch.setitem(deepvariant.settings, "deepvariant_image", image)
    dv.docker.add_image(image)
    dv.deepvariant.set_helpfull(helpfull or "")
    # Without num_partitions, fewer workers sort bigger partitions: 2 fit here.
    monkeypatch.setattr(alignment_memory, "available_memory", lambda: 30 * GIB)
    call(dv)
    probes = [r for r in dv.docker.runs() if r["command"] == ["--helpfull"]]
    assert len(probes) == (helpfull is not None)
    if partitions:
        assert postprocess(dv)[1] is not None
        return
    assert postprocess(dv) == ("2", None)
    assert "has no --num_partitions" in caplog.text
    monkeypatch.setattr(alignment_memory, "available_memory", lambda: 8 * GIB)
    with pytest.raises(WGSExtractError, match="use DeepVariant 1.9"):
        call(dv, out=dv.tmp / "small")


# Resuming -------------------------------------------------------------------


def test_oom_in_call_variants_fails_fast_and_a_rerun_resumes_there(dv):
    dv.deepvariant.fail("call_variants", oom=True)
    with pytest.raises(WGSExtractError, match="ran out of memory.*lower --threads"):
        call(dv)
    assert dv.deepvariant.stages() == ["make_examples", "call_variants"]
    killed = [r["args"][1] for r in dv.docker.runs("kill")]
    assert killed == [stage_runs(dv)[-1]["name"]]
    assert visible(dv.out) == []

    rerun(dv)
    assert dv.deepvariant.stages() == ["call_variants", "postprocess_variants"]
    assert visible(dv.out) == ["deepvariant.vcf.gz", "deepvariant.vcf.gz.tbi"]
    assert not (dv.out / deepvariant.WORK_DIRECTORY).exists()

    # A finished run is kept; only resource settings changed.
    rerun(dv, "--batch-size", "256")
    assert dv.deepvariant.stages() == []


def test_a_relative_outdir_keeps_the_examples_of_a_skipped_make_examples(
    dv, monkeypatch
):
    (dv.tmp / "cwd" / "sub").mkdir(parents=True)
    monkeypatch.chdir(dv.tmp / "cwd")
    dv.deepvariant.fail("call_variants")
    with pytest.raises(WGSExtractError):
        call(dv, out="sub/../../out")
    # call_variants fails if make_examples' shards are gone.
    rerun(dv, out="sub/../../out")
    assert dv.deepvariant.stages() == ["call_variants", "postprocess_variants"]
    assert visible(dv.out) == ["deepvariant.vcf.gz", "deepvariant.vcf.gz.tbi"]
    steps = json.loads((dv.out / "deepvariant.vcf.gz.steps.json").read_text())["steps"]
    called = [Path(o["path"]).name for o in steps["call_variants"]["outputs"]]
    assert called and not [name for name in called if "make_examples" in name]


def test_a_rerun_waits_for_a_killed_runs_container(dv):
    dv.deepvariant.fail("make_examples")
    with pytest.raises(WGSExtractError):
        call(dv)
    # As if wgsextract had been SIGKILLed while its container ran on.
    stage = stage_runs(dv)[-1]
    dv.docker.keep_running(stage["name"], stage["label"])
    with pytest.raises(WGSExtractError, match=f"container {stage['name']}"):
        rerun(dv)
    assert dv.deepvariant.stages() == []

    # When no engine answers, the rerun resumes.
    dv.docker.set(ps_error="Cannot connect to the Docker daemon")
    rerun(dv)
    assert dv.deepvariant.stages() == STAGES
    assert visible(dv.out) == ["deepvariant.vcf.gz", "deepvariant.vcf.gz.tbi"]


def test_interrupting_a_stage_stops_its_container(dv):
    dv.deepvariant.fail("call_variants", interrupt=os.getpid())
    with pytest.raises(KeyboardInterrupt):
        call(dv)
    killed = [r["args"][1] for r in dv.docker.runs("kill")]
    assert killed == [stage_runs(dv)[-1]["name"]]


def test_killed_make_examples_keeps_finished_shards_only_for_the_same_run(dv):
    work = dv.out / deepvariant.WORK_DIRECTORY
    dv.deepvariant.fail("make_examples", task=2, write=False)
    with pytest.raises(WGSExtractError, match="rerun the same command to resume"):
        call(dv)
    assert len(list(work.glob("make_examples*"))) == 2

    rerun(dv)
    (examples,) = dv.deepvariant.runs("parallel")
    assert examples["tasks"] == ["2", "3"]
    assert vcf_calls(dv.out / "deepvariant.vcf.gz") == dv.sample.calls

    # Different settings start make_examples afresh, without the old shards.
    dv.deepvariant.fail("make_examples", task=2, write=False)
    with pytest.raises(WGSExtractError):
        call(dv, "--force")
    dv.deepvariant.clear()
    dv.deepvariant.fail("make_examples", task=0, write=False)
    with pytest.raises(WGSExtractError):
        call(dv, "--wes")
    (examples,) = dv.deepvariant.runs("parallel")
    assert examples["tasks"] == ["0"]
    assert not list(work.glob("make_examples*"))


def test_truncated_or_changed_stage_outputs_and_inputs_are_redone(dv):
    dv.deepvariant.fail("postprocess_variants", write=False)
    with pytest.raises(WGSExtractError):
        call(dv)
    outputs = dv.out / deepvariant.WORK_DIRECTORY / "call_variants_output.tfrecord.gz"
    outputs.write_text(outputs.read_text()[:-2])  # Truncated.
    dv.deepvariant.clear()
    with pytest.raises(WGSExtractError):
        call(dv)
    assert dv.deepvariant.stages() == ["call_variants", "postprocess_variants"]

    # The same reads, written again.
    run_command(
        ["samtools", "view", "-b", "-l", "1", "-o", f"{dv.tmp}/r.bam", dv.sample.bam]
    )
    os.replace(f"{dv.tmp}/r.bam", dv.sample.bam)
    run_command(["samtools", "index", dv.sample.bam])
    rerun(dv)
    assert dv.deepvariant.stages() == STAGES

    (dv.out / "deepvariant.vcf.gz").write_text("edited")
    rerun(dv)
    # The intermediates were removed after use, so every stage reruns.
    assert dv.deepvariant.stages() == STAGES


def test_a_resume_keeps_shards_and_reruns_for_changed_regions_or_images(dv):
    bed = dv.tmp / "targets.bed"
    bed.write_text("chr1\t0\t2000\n")
    dv.deepvariant.fail("call_variants")
    with pytest.raises(WGSExtractError):
        call(dv, "-r", str(bed))

    # Fewer --threads: the 4 finished shards are kept.
    dv.deepvariant.clear()
    with pytest.raises(WGSExtractError):
        call(dv, "-r", str(bed), threads="2")
    assert dv.deepvariant.stages() == ["call_variants"]
    assert dv_flags(dv)["num_shards"] == "4"

    bed.write_text("chr1\t0\t2500\n")
    dv.deepvariant.clear()
    with pytest.raises(WGSExtractError):
        call(dv, "-r", str(bed))
    assert dv.deepvariant.stages() == ["make_examples", "call_variants"]

    # The same tag now names another build.
    dv.docker.add_image(deepvariant.DEFAULT_IMAGE, image_id="sha256:rebuilt")
    rerun(dv, "-r", str(bed))
    assert dv.deepvariant.stages() == STAGES


def test_dry_run_leaves_an_interrupted_runs_files_alone(dv):
    dv.deepvariant.fail("call_variants")
    with pytest.raises(WGSExtractError):
        call(dv)
    work = dv.out / deepvariant.WORK_DIRECTORY
    before = sorted(p.relative_to(work) for p in work.rglob("*"))
    rerun(dv, "--dry-run")
    assert sorted(p.relative_to(work) for p in work.rglob("*")) == before
    assert dv.deepvariant.stages() == []


def test_dry_run_and_forced_steps(dv, capsys):
    dv.deepvariant.fail("postprocess_variants")
    with pytest.raises(WGSExtractError):
        call(dv)
    capsys.readouterr()

    rerun(dv, "--dry-run")
    assert dv.deepvariant.stages() == []
    plan = capsys.readouterr().out
    assert re.search(r"make_examples +skip +complete", plan)
    assert re.search(r"postprocess_variants +run +an earlier run was interrupted", plan)
    assert visible(dv.out) == []

    rerun(dv, "--force-step", "call_variants")
    assert dv.deepvariant.stages() == ["call_variants", "postprocess_variants"]

    rerun(dv, "--force")
    assert dv.deepvariant.stages() == STAGES


# Ultima on the GPU (#358) --------------------------------------------------


@pytest.fixture
def ultima_gpu(dv):
    """Ultima reads on a GPU host: an RTX 3090 with nothing else on it, Docker's
    NVIDIA runtime and Google's ULTIMA GPU image, but no Parabricks."""
    dv.docker.set(gpus=True, runtimes='{"nvidia":{},"runc":{}}')
    dv.docker.add_image(deepvariant.DEFAULT_ULTIMA_GPU_IMAGE)
    dv.gpus = dv.tmp / "gpus.json"
    set_gpus(dv.gpus, (24_576, 0))
    fake_nvidia_smi(dv.docker.root.parent / "bin", dv.gpus)
    clear_tool_caches()
    dv.ultima = other_sample(dv, ULTIMA[0], tags=ULTIMA[1])
    return dv


def without_gpu_driver(dv, monkeypatch):
    """Only the fake Docker and the core tools on PATH: no nvidia-smi."""
    bindir = dv.docker.root.parent / "bin"
    for name in ("nvidia-smi", "nvidia-smi.py", "nvidia-smi.cmd"):
        (bindir / name).unlink(missing_ok=True)
    tools = {str(Path(path).parent) for path in CORE_TOOLS.values()}
    monkeypatch.setenv("PATH", os.pathsep.join([str(bindir), *tools]))
    clear_tool_caches()


def gpu_call(dv, *options):
    call(dv, "--gpu", *options, reads=dv.ultima.bam)


def gpu_stages(dv):
    """The stages that ran, each with whether its container had the GPUs."""
    return [
        (re.search(r"/(\w+)\s", r["command"][2]).group(1), r["gpus"])
        for r in stage_runs(dv)
    ]


@pytest.mark.parametrize("selection", [[], ["--model-type", "ULTIMA"]])
def test_ultima_reads_call_on_the_gpu_in_googles_ultima_gpu_image(
    ultima_gpu, selection
):
    gpu_call(ultima_gpu, *selection, "-r", "chr1:1-1500,chr1:1501-3000")

    calls = vcf_calls(ultima_gpu.out / "deepvariant.vcf.gz")
    assert calls == ultima_gpu.ultima.calls
    # contract: Parabricks has no ULTIMA model, so DeepVariant's own ULTIMA
    # model runs, in Google's GPU image, with call_variants on the GPU.
    assert dv_flags(ultima_gpu)["model_type"] == "ULTIMA"
    # contract: --gpu's comma-separated regions reach DeepVariant spaced.
    assert dv_flags(ultima_gpu)["regions"] == "chr1:1-1500 chr1:1501-3000"
    images = {r["image"] for r in ultima_gpu.docker.runs()}
    assert images == {deepvariant.DEFAULT_ULTIMA_GPU_IMAGE}
    assert gpu_stages(ultima_gpu) == [
        ("make_examples", None), ("call_variants", "all"),
        ("postprocess_variants", None),
    ]  # fmt: skip


@pytest.mark.parametrize(
    ("problem", "message"),
    [
        ("no driver", "No NVIDIA GPU driver"),
        ("no nvidia runtime", "NVIDIA runtime"),
        ("no image", "docker pull google/deepvariant:gh1100-ultima-gpu@sha256:"),
    ],
)
def test_ultima_gpu_setup_problems_are_named_before_any_work(
    ultima_gpu, monkeypatch, problem, message
):
    if problem == "no driver":
        without_gpu_driver(ultima_gpu, monkeypatch)
    elif problem == "no nvidia runtime":
        ultima_gpu.docker.set(runtimes='{"runc":{}}')
    else:
        ultima_gpu.docker.set(images={})
    with pytest.raises(MissingDependencyError, match=message):
        gpu_call(ultima_gpu)
    assert ultima_gpu.docker.runs() == []


@pytest.mark.parametrize(
    ("system", "machine"),
    [("darwin", "arm64"), ("linux", "aarch64"), ("win32", "AMD64")],
)
def test_ultima_gpu_needs_x86_64_linux(ultima_gpu, monkeypatch, system, machine):
    monkeypatch.setattr(deepvariant.platform, "machine", lambda: machine)
    monkeypatch.setattr(sys, "platform", system)
    with pytest.raises(UsageError, match="--gpu is not supported"):
        gpu_call(ultima_gpu, "--model-type", "ULTIMA")
    assert ultima_gpu.docker.runs() == []


def test_a_busy_gpu_stops_before_call_variants_and_a_rerun_resumes(ultima_gpu):
    set_gpus(ultima_gpu.gpus, (24_576, 23_500))
    with pytest.raises(WGSExtractError, match="free GPU memory"):
        gpu_call(ultima_gpu)
    assert ultima_gpu.deepvariant.stages() == ["make_examples"]

    set_gpus(ultima_gpu.gpus, (24_576, 0))
    rerun(ultima_gpu, "--gpu", reads=ultima_gpu.ultima.bam)
    assert ultima_gpu.deepvariant.stages() == ["call_variants", "postprocess_variants"]

    # A finished run needs no GPU to be kept, nor does a plan.
    set_gpus(ultima_gpu.gpus, (24_576, 23_500))
    rerun(ultima_gpu, "--gpu", reads=ultima_gpu.ultima.bam)
    rerun(ultima_gpu, "--gpu", "--dry-run", reads=ultima_gpu.ultima.bam)
    assert ultima_gpu.deepvariant.stages() == []


def test_no_gpu_memory_check_runs_on_a_busy_gpu_with_a_warning(ultima_gpu):
    set_gpus(ultima_gpu.gpus, (24_576, 23_500))
    result = api.vcf.deepvariant(
        api.VcfDeepvariantParams(
            input=Path(ultima_gpu.ultima.bam), ref=Path(ultima_gpu.sample.ref),
            outdir=ultima_gpu.out, gpu=True, no_gpu_memory_check=True,
        )
    )  # fmt: skip
    assert "deepvariant_gpu_memory" in [w.code for w in result.warnings]
    assert (ultima_gpu.out / "deepvariant.vcf.gz").is_file()


def test_a_gpu_plan_needs_no_gpu_driver(ultima_gpu, monkeypatch, capsys):
    without_gpu_driver(ultima_gpu, monkeypatch)
    gpu_call(ultima_gpu, "--dry-run")
    assert re.search(r"call_variants +run", capsys.readouterr().out)
    assert ultima_gpu.deepvariant.stages() == []
