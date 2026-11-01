"""CPU DeepVariant: Google's image through Docker, or run_deepvariant on PATH.

The image is too big for CI; these check the setup checks and what Docker is
asked to run.
"""

import gzip
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from tests.htslib_helpers import needs_tools, sam_to_bam, write_fasta
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
    Path(str(ref) + ".fai").write_text("chr20\t1000\t0\t1000\t1001\n")
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


@pytest.mark.skipif(sys.platform == "win32", reason="runs as the POSIX user")
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


@pytest.mark.parametrize(
    ("definition", "expected"),
    [
        (b"CRAM\x03\x00" + bytes(20), (3, 0)),
        (b"CRAM\x03\x01" + bytes(20), (3, 1)),
        (b"CRAM\x04\x00" + bytes(20), (4, 0)),
        (b"BAM\x01" + bytes(20), None),
        (b"\x1f\x8b" + bytes(20), None),
        (b"CRAM\x03", None),
        (b"", None),
        (b"garbage", None),
    ],
)
def test_cram_version_sniffs_definition_not_extension(tmp_path, definition, expected):
    alignment = tmp_path / "reads.bam"
    alignment.write_bytes(definition)
    assert deepvariant._cram_version(str(alignment)) == expected


@pytest.mark.parametrize("version", [None, (3, 0), (3, 1), (4, 0)])
def test_only_new_cram_is_transcoded_and_indexed(files, monkeypatch, version, caplog):
    ref, alignment, work = files
    if version:
        alignment.write_bytes(b"CRAM" + bytes(version) + bytes(20))
    calls = []
    monkeypatch.setattr(deepvariant, "run_command", lambda cmd: calls.append(cmd))
    monkeypatch.setattr(deepvariant, "check_free_space", lambda *_: True)
    with caplog.at_level("INFO"):
        result = deepvariant._compatible_reads(
            str(alignment), str(ref), str(work), "4", "chr20:1-100"
        )
    if version is None or version < (3, 1):
        assert result == str(alignment)
        assert calls == []
        assert "Transcoding CRAM" not in caplog.text
        return
    assert result == str(work / "reads.cram")
    view, index = calls
    assert view[:2] == ["samtools", "view"]
    assert view[view.index("-@") + 1] == "4"
    assert view[view.index("-T") + 1] == str(ref)
    assert view[view.index("--output-fmt-option") + 1] == "version=3.0"
    assert view[-1] == str(alignment)
    assert "-M" in view
    bed = Path(view[view.index("-L") + 1])
    assert bed.read_text() == "chr20\t0\t100\n"
    assert index == ["samtools", "index", result]
    assert caplog.text.count("Transcoding CRAM") == 1
    assert "htslib 1.18 SSE4 rANS decoder" in caplog.text


def test_transcode_fails_before_writing_when_disk_is_low(files, monkeypatch):
    ref, alignment, work = files
    alignment.write_bytes(b"CRAM\x03\x01" + bytes(20))
    monkeypatch.setattr(deepvariant, "check_free_space", lambda *_: False)
    monkeypatch.setattr(
        deepvariant, "run_command", lambda *_: pytest.fail("must check disk first")
    )
    with pytest.raises(WGSExtractError, match="Insufficient free disk"):
        deepvariant._compatible_reads(str(alignment), str(ref), str(work), "1", None)
    assert not (work / "reads.cram").exists()


@pytest.mark.parametrize(
    "failure",
    [None, "view", "index", "caller", "cancel", "cancel-view", "cancel-index"],
)
def test_staged_cram_is_used_and_cleaned_on_all_exit_paths(files, monkeypatch, failure):
    _ref, alignment, out = files
    alignment.write_bytes(b"CRAM\x03\x01" + bytes(20))
    monkeypatch.setattr(
        deepvariant,
        "get_tool_path",
        lambda tool: "/opt/dv/run_deepvariant" if tool == "run_deepvariant" else None,
    )
    monkeypatch.setattr(deepvariant, "check_free_space", lambda *_: True)
    staged = []

    def fake(cmd, **kwargs):
        if cmd[0] == "samtools":
            if cmd[1] == "view":
                path = Path(cmd[cmd.index("-o") + 1])
                staged.append(path)
                path.write_bytes(b"partial")
                assert cmd[-1] == str(alignment)  # Whole input, without a region.
            else:
                Path(cmd[-1] + ".crai").touch()
            if cmd[1] == failure:
                raise subprocess.CalledProcessError(1, cmd)
            if failure == f"cancel-{cmd[1]}":
                raise KeyboardInterrupt
        else:
            reads = Path(cmd[cmd.index("--reads") + 1])
            assert reads == staged[0]
            assert reads.is_file()
            assert Path(str(reads) + ".crai").exists()
            if failure == "caller":
                raise subprocess.CalledProcessError(1, cmd)
            if failure == "cancel":
                raise KeyboardInterrupt
            Path(cmd[cmd.index("--output_vcf") + 1]).write_text("vcf")

    monkeypatch.setattr(deepvariant, "run_command", fake)
    if failure:
        error = KeyboardInterrupt if failure.startswith("cancel") else WGSExtractError
        with pytest.raises(error):
            deepvariant.cmd_deepvariant(_args(alignment, region=None))
    else:
        deepvariant.cmd_deepvariant(_args(alignment, region=None))
    assert len(staged) == 1
    assert not staged[0].parent.exists()
    assert alignment.read_bytes() == b"CRAM\x03\x01" + bytes(20)
    assert sorted(p.name for p in out.iterdir()) == (
        ["deepvariant.vcf.gz"] if failure is None else []
    )


@pytest.mark.skipif(sys.platform == "win32", reason="runs as the POSIX user")
@pytest.mark.parametrize("region", ["chr20", "chr20:1-50 chr20:101-150"])
def test_container_reads_staged_cram_and_preserves_existing_mounts(
    files, monkeypatch, region
):
    _ref, alignment, out = files
    alignment.write_bytes(b"CRAM\x03\x01" + bytes(20))
    runs = _docker(monkeypatch)
    monkeypatch.setattr(deepvariant, "check_free_space", lambda *_: True)
    monkeypatch.setattr(deepvariant, "run_command", lambda *a, **kw: None)
    deepvariant.cmd_deepvariant(_args(alignment, region=region))
    (cmd,) = runs
    reads = Path(cmd[cmd.index("--reads") + 1])
    assert reads.name == "reads.cram"
    assert cmd[cmd.index("--regions") + 1] == region
    assert reads.parent.parent == out
    mounts = {cmd[i + 1].split(":")[0] for i, a in enumerate(cmd) if a == "-v"}
    assert str(reads.parent) in mounts
    assert str(alignment.parent) in mounts
    assert not reads.parent.exists()


@needs_tools("samtools")
@pytest.mark.parametrize(
    ("region", "expected", "source_region"),
    [
        (None, "2", None),
        ("chr20:1-50", "1", "chr20:1-50"),
        ("chr20", "2", None),
        ("chr20:1-50 chr20:101-150", "2", None),
        ("chr20:1-50 chr20:1-30", "1", "chr20:1-50"),
        ("bed", "1", "chr20:1-50"),
        ("bed.gz", "1", "chr20:1-50"),
        ("bed-mixed", "2", None),
        ("bedpe", "2", None),
    ],
)
def test_real_cram31_roundtrip_has_cram30_header_and_read_parity(
    tmp_path, region, expected, source_region
):
    from wgsextract_cli.core.utils import run_command

    ref = write_fasta(tmp_path / "ref.fa", {"chr20": "ACGT" * 100})
    bam = sam_to_bam(
        [
            "@HD\tVN:1.6\tSO:coordinate",
            "@SQ\tSN:chr20\tLN:400",
            "r1\t0\tchr20\t1\t60\t20M\t*\t0\t0\tACGTACGTACGTACGTACGT\tIIIIIIIIIIIIIIIIIIII",
            "r2\t0\tchr20\t101\t60\t20M\t*\t0\t0\tACGTACGTACGTACGTACGT\tIIIIIIIIIIIIIIIIIIII",
        ],
        tmp_path / "reads.bam",
    )
    cram = tmp_path / "source.cram"
    run_command(
        [
            "samtools", "view", "-C", "--output-fmt-option", "version=3.1",
            "-T", str(ref), "-o", str(cram), bam,
        ]
    )  # fmt: skip
    run_command(["samtools", "index", str(cram)])
    assert deepvariant._cram_version(str(cram)) == (3, 1)
    work = tmp_path / "dv"
    work.mkdir()
    if region and region.startswith("bed"):
        suffix = ".bed" if region == "bed-mixed" else "." + region
        regions_file = tmp_path / ("selection" + suffix)
        if region == "bedpe":
            regions_file.write_text("chr20\t0\t20\tchr20\t100\t120\n")
        elif region == "bed.gz":
            with gzip.open(regions_file, "wt") as stream:
                stream.write("chr20\t0\t50\nchr20\t0\t30\n")
        else:
            regions_file.write_text("chr20\t0\t50\nchr20\t0\t30\n")
        region = str(regions_file) + (" chr20:101-150" if region == "bed-mixed" else "")
    converted = deepvariant._compatible_reads(
        str(cram), str(ref), str(work), "1", region
    )
    assert deepvariant._cram_version(converted) == (3, 0)
    assert Path(converted + ".crai").is_file()
    original_count = run_command(
        [
            "samtools",
            "view",
            "-c",
            "-T",
            str(ref),
            str(cram),
            *([source_region] if source_region else []),
        ],
        capture_output=True,
    ).stdout.strip()
    converted_count = run_command(
        ["samtools", "view", "-c", "-T", str(ref), converted], capture_output=True
    ).stdout.strip()
    assert converted_count == original_count == expected
