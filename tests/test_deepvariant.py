"""CPU DeepVariant: Google's image through Docker, or run_deepvariant on PATH.

The image is too big for CI; these check the setup checks and what Docker is
asked to run.
"""

import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

from wgsextract_cli.commands.vcf import deepvariant
from wgsextract_cli.core import containers
from wgsextract_cli.core.utils import WGSExtractError


def _completed(returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, "", stderr)


@pytest.fixture
def files(tmp_path, monkeypatch):
    """A reference, a BAM and an output directory in separate folders."""
    for d in ("ref", "bam", "out"):
        (tmp_path / d).mkdir()
    ref = tmp_path / "ref" / "hs38DH.fa.gz"
    bam = tmp_path / "bam" / "s.bam"
    ref.write_text("x")
    bam.write_text("x")
    out = tmp_path / "out"
    monkeypatch.setattr(
        deepvariant, "get_base_args", lambda _a: ("4", str(out), str(ref), None)
    )
    monkeypatch.setattr(deepvariant, "verify_dependencies", lambda _t: None)
    monkeypatch.setattr(deepvariant, "ensure_vcf_indexed", lambda _v: None)
    monkeypatch.setattr(deepvariant, "warn_if_no_variants", lambda *_a: None)
    monkeypatch.setattr(deepvariant.sys, "platform", "linux")
    return ref, bam, out


def _args(bam, **overrides):
    values = {"input": str(bam), "region": "chr20", "checkpoint": None, "gpu": False}
    return Namespace(**{**values, **overrides})


def _docker(monkeypatch, *, present=True, info_rc=0, image_rc=0):
    """Fake the docker CLI; returns the `docker run` commands."""
    runs = []

    def fake(cmd, **_kwargs):
        if cmd[1] == "info":
            return _completed(info_rc, "permission denied")
        if cmd[1] == "image":
            return _completed(image_rc)
        runs.append(cmd)
        out_vcf = cmd[cmd.index("--output_vcf") + 1]
        Path(out_vcf).write_text("vcf")
        return _completed()

    def tool_path(tool):
        return "docker" if tool == "docker" and present else None

    monkeypatch.setattr(containers, "get_tool_path", tool_path)
    monkeypatch.setattr(deepvariant, "get_tool_path", tool_path)
    monkeypatch.setattr(containers, "run_command", fake)
    return runs


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        ({"present": False}, "Docker not found.*docker pull google/deepvariant"),
        ({"info_rc": 1}, "docker group"),
        ({"image_rc": 1}, "image isn't downloaded: docker pull google/deepvariant"),
    ],
)
def test_missing_docker_setup_is_named(files, monkeypatch, setup, message):
    _ref, bam, _out = files
    _docker(monkeypatch, **setup)
    with pytest.raises(WGSExtractError, match=message):
        deepvariant.cmd_deepvariant(_args(bam))


def test_windows_is_refused(files, monkeypatch):
    _ref, bam, _out = files
    _docker(monkeypatch)
    monkeypatch.setattr(deepvariant.sys, "platform", "win32")
    with pytest.raises(WGSExtractError, match="Windows"):
        deepvariant.cmd_deepvariant(_args(bam))


def test_the_image_runs_run_deepvariant_on_mounted_paths(files, monkeypatch, tmp_path):
    ref, bam, out = files
    runs = _docker(monkeypatch)
    model = tmp_path / "model" / "model.ckpt"
    model.parent.mkdir()

    deepvariant.cmd_deepvariant(
        _args(bam, checkpoint=str(model), model_type="WES", pacbio=False, wes=False)
    )

    (cmd,) = runs
    image_at = cmd.index(deepvariant.DEFAULT_IMAGE)
    dv = cmd[image_at + 1 :]
    assert dv[0] == "/opt/deepvariant/bin/run_deepvariant"
    flag = {dv[i]: dv[i + 1] for i in range(1, len(dv) - 1, 2)}
    assert flag["--model_type"] == "WES"
    assert flag["--ref"] == str(ref)
    assert flag["--reads"] == str(bam)
    assert flag["--output_vcf"] == str(out / "deepvariant.vcf.gz")
    assert flag["--num_shards"] == "4"
    assert flag["--regions"] == "chr20"
    assert flag["--customized_model"] == str(model)
    mounts = {cmd[i + 1].split(":")[0] for i, a in enumerate(cmd) if a == "-v"}
    work = Path(flag["--intermediate_results_dir"])
    assert mounts == {str(p) for p in (ref.parent, bam.parent, out, work, model.parent)}
    assert "--gpus" not in cmd
    # Only the VCF is left; the intermediate directory is removed.
    assert [p.name for p in out.iterdir()] == ["deepvariant.vcf.gz"]


def test_run_deepvariant_on_path_is_used_without_docker(files, monkeypatch):
    _ref, bam, out = files
    monkeypatch.setattr(
        deepvariant,
        "get_tool_path",
        lambda tool: "/opt/dv/run_deepvariant" if tool == "run_deepvariant" else None,
    )
    monkeypatch.setenv("PYTHONPATH", "/ours")
    calls = []

    def fake(cmd, **kwargs):
        calls.append((cmd, kwargs))
        Path(cmd[cmd.index("--output_vcf") + 1]).write_text("vcf")

    monkeypatch.setattr(deepvariant, "run_command", fake)
    deepvariant.cmd_deepvariant(_args(bam, region=None))

    ((cmd, kwargs),) = calls
    assert cmd[0] == "/opt/dv/run_deepvariant"
    assert "--regions" not in cmd
    assert cmd[cmd.index("--model_type") + 1] == "WGS"
    assert "PYTHONPATH" not in kwargs["env"]
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert (out / "deepvariant.vcf.gz").exists()
