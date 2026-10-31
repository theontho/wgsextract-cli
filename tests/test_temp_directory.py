import os
import shutil
from argparse import Namespace
from pathlib import Path

import pytest

from wgsextract_cli.cli import parser
from wgsextract_cli.core import config, samtools_commands
from wgsextract_cli.core.temp_directory import (
    configured_temp_directory,
    run_temporary_directory,
)
from wgsextract_cli.core.utils import WGSExtractError


@pytest.mark.parametrize("keep, expected", [(False, (6, 9)), (True, (10, 5))])
def test_realign_preflight_budgets_output_and_staging_volumes_separately(
    tmp_path, monkeypatch, keep, expected
):
    from wgsextract_cli.commands import realign

    source = tmp_path / "input.bam"
    source.write_bytes(b"input")
    output, scratch = tmp_path / "out", tmp_path / "scratch"
    output.mkdir()
    scratch.mkdir()
    stat = Path.stat

    def separate_volume(path, **kwargs):
        result = stat(path, **kwargs)
        if path == scratch:
            values = list(result)
            values[2] += 1
            return os.stat_result(values)
        return result

    monkeypatch.setattr(Path, "stat", separate_volume)
    monkeypatch.setattr(realign, "configured_temp_directory", lambda: str(scratch))
    needs = iter([(3, 4), (5, 6)])
    monkeypatch.setattr(
        realign, "get_free_space_needed", lambda *args, **kwargs: next(needs)
    )
    checked = []
    monkeypatch.setattr(
        realign,
        "check_free_space",
        lambda path, size: checked.append((path, size)) or True,
    )
    realign._preflight_disk_space(str(source), str(output), True, 100, keep_fastq=keep)
    assert checked == [(str(output), expected[0]), (str(scratch), expected[1])]


def test_cli_override_routes_sort_spills_and_is_cleaned_on_failure(
    tmp_path, monkeypatch
):
    configured = tmp_path / "configured"
    chosen = tmp_path / "chosen"
    configured.mkdir()
    chosen.mkdir()
    monkeypatch.setitem(config.settings, "temp_directory", str(configured))
    command_parser, base = parser.build_parser()
    args, _ = parser.parse_args(
        command_parser, base, ["align", "--r1", "reads.fq", "--tmp-dir", str(chosen)]
    )
    monkeypatch.setattr(samtools_commands, "_use_sambamba", lambda: False)
    with pytest.raises(RuntimeError), run_temporary_directory(args.tmp_dir):
        directory = configured_temp_directory()
        assert directory is not None and Path(directory).is_relative_to(chosen)
        command = samtools_commands.get_sam_sort_cmd(
            "final.bam", "1", "1M", sam_input=True
        )
        assert command[command.index("-T") + 1] == directory
        Path(directory, "spilled.bam").write_bytes(b"spill")
        assert config.config_args()[-2:] == ["--tmp-dir", directory]
        raise RuntimeError("interrupted run")
    assert list(chosen.iterdir()) == []
    assert config.settings["temp_directory"] == str(configured)


def test_sambamba_spills_follow_the_selected_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(samtools_commands, "_use_sambamba", lambda: True)
    with run_temporary_directory(tmp_path):
        command = samtools_commands.get_sam_sort_cmd("final.bam", "1", "1M")
        assert command[command.index("--tmpdir") + 1] == configured_temp_directory()


def test_invalid_temp_directory_fails_before_starting_tools(tmp_path):
    with (
        pytest.raises(WGSExtractError, match="must exist and be writable"),
        run_temporary_directory(tmp_path / "missing"),
    ):
        pytest.fail("the command started")


def test_gatk_workspace_uses_the_selected_volume(tmp_path):
    from wgsextract_cli.commands.vcf.callers import _gatk_tmp_dir

    with run_temporary_directory(tmp_path):
        directory = _gatk_tmp_dir()
        assert Path(directory).resolve().is_relative_to(tmp_path.resolve())
        shutil.rmtree(directory)
    assert list(tmp_path.iterdir()) == []


def test_native_pbmm2_rejects_an_unsupported_internal_sort_override(tmp_path):
    from wgsextract_cli.commands.align import align_pbmm2

    with (
        run_temporary_directory(tmp_path),
        pytest.raises(WGSExtractError, match="internal sorter"),
    ):
        align_pbmm2(Namespace(gpu=False))
