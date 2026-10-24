"""Parabricks (GPU) alignment and calling: setup checks and the commands built.

A real GPU run can't happen in CI; these check what the CLI asks Docker to do.
"""

import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

from tests.htslib_helpers import needs_tools, sam_to_bam
from wgsextract_cli.commands import align
from wgsextract_cli.commands.vcf import callers, deepvariant
from wgsextract_cli.core import containers, parabricks
from wgsextract_cli.core.utils import WGSExtractError, run_command


def _completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(parabricks.sys, "platform", "linux")


def _tools(monkeypatch, *present: str) -> None:
    for module in (parabricks, containers):
        monkeypatch.setattr(
            module, "get_tool_path", lambda tool: tool if tool in present else None
        )


def _docker_answers(monkeypatch, info: subprocess.CompletedProcess, image_rc: int):
    def fake(cmd, **_kwargs):
        return info if cmd[1] == "info" else _completed(image_rc)

    monkeypatch.setattr(containers, "run_command", fake)


@pytest.mark.parametrize(
    ("tools", "info", "image_rc", "message"),
    [
        ((), _completed(), 0, "Docker not found"),
        (("docker",), _completed(), 0, "No NVIDIA GPU driver"),
        (
            ("docker", "nvidia-smi"),
            _completed(1, stderr="permission denied"),
            0,
            "docker group",
        ),
        (("docker", "nvidia-smi"), _completed(0, '{"runc":{}}'), 0, "NVIDIA runtime"),
        (("docker", "nvidia-smi"), _completed(0, '{"nvidia":{}}'), 1, "docker pull"),
    ],
)
def test_setup_problems_are_named(linux, monkeypatch, tools, info, image_rc, message):
    _tools(monkeypatch, *tools)
    _docker_answers(monkeypatch, info, image_rc)
    with pytest.raises(WGSExtractError, match=message):
        parabricks.verify_parabricks()


def test_a_complete_setup_passes(linux, monkeypatch):
    _tools(monkeypatch, "docker", "nvidia-smi")
    _docker_answers(monkeypatch, _completed(0, '{"nvidia":{},"runc":{}}'), 0)
    parabricks.verify_parabricks()


def test_parabricks_is_linux_only(monkeypatch):
    monkeypatch.setattr(parabricks.sys, "platform", "darwin")
    with pytest.raises(WGSExtractError, match="Linux only"):
        parabricks.verify_parabricks()


def test_the_container_sees_each_directory_at_its_own_path(tmp_path, monkeypatch):
    _tools(monkeypatch, "docker")
    monkeypatch.setattr(parabricks, "parabricks_image", lambda: "pb:test")
    calls = []
    monkeypatch.setattr(containers, "run_command", calls.append)
    (tmp_path / "ref").mkdir()
    (tmp_path / "out").mkdir()
    ref = str(tmp_path / "ref" / "hs38DH.fa")
    parabricks.run_pbrun("fq2bam", ["--ref", ref], [ref, str(tmp_path / "out")])

    cmd = calls[0]
    assert cmd[:5] == ["docker", "run", "--rm", "--gpus", "all"]
    assert cmd[cmd.index("-u") + 1].count(":") == 1
    mounts = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-v"]
    assert mounts == [f"{tmp_path / 'out'}:{tmp_path / 'out'}", f"{tmp_path / 'ref'}:{tmp_path / 'ref'}"]  # fmt: skip
    assert cmd[cmd.index("pb:test") :] == ["pb:test", "pbrun", "fq2bam", "--ref", ref]


def _align_args(tmp_path: Path, r2: bool = True) -> Namespace:
    ref = tmp_path / "ref.fa"
    ref.write_text(">chr1\nACGT\n")
    (tmp_path / "ref.fa.fai").write_text(
        "chr1\t4\t6\t4\t5\nchr1_KI1_alt\t4\t16\t4\t5\n"
    )
    for name in ("r1.fq.gz", "r2.fq.gz", "ref.fa.bwt"):
        (tmp_path / name).write_text("x")
    return Namespace(
        r1=str(tmp_path / "r1.fq.gz"),
        r2=str(tmp_path / "r2.fq.gz") if r2 else None,
        ref=str(ref), input=None, outdir=str(tmp_path), format="CRAM",
        threads="4", memory="1G", sample="ladan", platform="illumina",
        aligner="auto", gpu=True, preset=None, long_read=False,
    )  # fmt: skip


@pytest.mark.parametrize(
    ("paired", "fq_arg"), [(True, "--in-fq"), (False, "--in-se-fq")]
)
def test_fq2bam_gets_the_reads_read_group_and_alt_file(
    tmp_path, monkeypatch, paired, fq_arg
):
    monkeypatch.setattr(align, "verify_dependencies", lambda *_a: None)
    monkeypatch.setattr(align, "log_dependency_info", lambda *_a: None)
    monkeypatch.setattr(parabricks, "verify_parabricks", lambda: None)
    runs = []
    monkeypatch.setattr(
        parabricks, "run_pbrun", lambda tool, args, paths: runs.append((tool, args))
    )
    args = _align_args(tmp_path, r2=paired)

    align.run(args)

    tool, pb_args = runs[0]
    assert tool == "fq2bam"
    reads = pb_args[pb_args.index(fq_arg) + 1 :]
    assert reads[: 2 if paired else 1] == [args.r1] + ([args.r2] if paired else [])
    read_group = reads[2 if paired else 1]
    assert read_group.startswith("@RG\\tID:") and "\\tSM:ladan" in read_group
    assert read_group.endswith("\\tPU:r1")
    assert pb_args[pb_args.index("--out-bam") + 1].endswith("_aligned.cram")
    # BWA's ALT list goes next to the reference, as for align --aligner bwa.
    assert (tmp_path / "ref.fa.alt").read_text() == "chr1_KI1_alt\n"


@pytest.mark.parametrize(
    ("overrides", "preset"),
    [
        ({"platform": "hifi", "aligner": "minimap2", "r2": None}, "map-hifi"),
        ({"platform": "ont", "aligner": "minimap2", "r2": None}, "map-ont"),
        ({"platform": "hifi", "aligner": "pbmm2", "r2": None}, "map-pbmm2"),
    ],
)
def test_gpu_long_reads_use_parabricks_minimap2(
    tmp_path, monkeypatch, overrides, preset
):
    monkeypatch.setattr(align, "_ensure_read_group_sample", lambda *_a: None)
    monkeypatch.setattr(align, "verify_dependencies", lambda *_a: None)
    monkeypatch.setattr(align, "log_dependency_info", lambda *_a: None)
    monkeypatch.setattr(parabricks, "verify_parabricks", lambda: None)
    runs = []
    monkeypatch.setattr(
        parabricks, "run_pbrun", lambda tool, args, paths: runs.append((tool, args))
    )
    args = _align_args(tmp_path)
    for key, value in overrides.items():
        setattr(args, key, value)

    align.run(args)

    tool, pb_args = runs[0]
    assert tool == "minimap2"
    assert pb_args[pb_args.index("--preset") + 1] == preset
    assert pb_args[pb_args.index("--in-fq") + 1] == args.r1


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {"platform": "clr", "aligner": "minimap2", "r2": None},
            "no minimap2 preset for map-pb",
        ),
        ({"platform": "clr", "aligner": "pbmm2", "r2": None}, "not CLR"),
        ({"aligner": "minimap2"}, "no minimap2 preset for sr"),
    ],
)
def test_gpu_alignment_without_a_parabricks_equivalent_is_refused(
    tmp_path, overrides, message
):
    args = _align_args(tmp_path)
    for key, value in overrides.items():
        setattr(args, key, value)
    with pytest.raises(WGSExtractError, match=message):
        align.run(args)


def _call_args(tmp_path: Path, **extra) -> Namespace:
    values = {"input": str(tmp_path / "x.cram"), "region": "chr20,chr21:1-500",
              "gpu": True, "checkpoint": None, "wes": False, "pacbio": False,
              "model_type": None}  # fmt: skip
    values.update(extra)
    return Namespace(**values)


@pytest.mark.parametrize(
    ("module", "command", "tool", "extra", "expected_extra"),
    [
        (deepvariant, "cmd_deepvariant", "deepvariant", {}, []),
        (deepvariant, "cmd_deepvariant", "deepvariant", {"wes": True}, ["--use-wes-model"]),
        (deepvariant, "cmd_deepvariant", "deepvariant", {"pacbio": True}, ["--mode", "pacbio"]),
        (callers, "cmd_gatk", "haplotypecaller", {}, []),
    ],
)  # fmt: skip
def test_gpu_calling_runs_the_parabricks_caller(
    tmp_path, monkeypatch, module, command, tool, extra, expected_extra
):
    monkeypatch.setattr(module, "verify_dependencies", lambda *_a: None)
    monkeypatch.setattr(parabricks, "verify_parabricks", lambda: None)
    monkeypatch.setattr(
        module, "get_base_args", lambda _a: ("4", str(tmp_path), "ref.fa", None)
    )
    monkeypatch.setattr(module, "ensure_vcf_indexed", lambda _v: None)
    monkeypatch.setattr(module, "warn_if_no_variants", lambda *_a: None)
    runs = []
    monkeypatch.setattr(parabricks, "run_pbrun", lambda t, a, p: runs.append((t, a)))

    getattr(module, command)(_call_args(tmp_path, **extra))

    ran, pb_args = runs[0]
    assert ran == tool
    assert pb_args[pb_args.index("-L") :] == ["-L", "chr20", "-L", "chr21:1-500", *expected_extra]  # fmt: skip


def test_gpu_deepvariant_refuses_a_custom_checkpoint(tmp_path):
    with pytest.raises(WGSExtractError, match="checkpoint"):
        deepvariant.cmd_deepvariant(_call_args(tmp_path, checkpoint="model.ckpt"))


@needs_tools("samtools")
def test_read_groups_without_a_sample_get_one(tmp_path):
    """PacBio's unaligned BAMs carry read groups without SM, which Parabricks keeps."""
    bam = sam_to_bam(
        ["@SQ\tSN:chr1\tLN:1000", "@RG\tID:movie\tPL:PACBIO",
         "r1\t0\tchr1\t1\t60\t4M\t*\t0\t0\tACGT\tIIII\tRG:Z:movie"],
        tmp_path / "hifi.bam",
    )  # fmt: skip
    align._ensure_read_group_sample(bam, "HG00732", "unused.fa")
    header = run_command(["samtools", "view", "-H", bam], capture_output=True).stdout
    assert "@RG\tID:movie\tPL:PACBIO\tSM:HG00732" in header
    assert (
        run_command(["samtools", "view", "-c", bam], capture_output=True).stdout.strip()
        == "1"
    )
