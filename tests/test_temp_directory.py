import json
import os
import random
import shutil
import threading
from argparse import Namespace
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import psutil
import pytest

from tests.disk_volumes import GIB, Volumes
from tests.htslib_helpers import needs_tools, sam_to_bam, write_fasta
from wgsextract_cli.core.temp_directory import (
    configured_temp_directory,
    run_temporary_directory,
)
from wgsextract_cli.core.utils import WGSExtractError, run_command
from wgsextract_cli.main import main


@pytest.fixture
def volumes(monkeypatch) -> Volumes:
    return Volumes(monkeypatch)


def _wgsextract(capsys, *argv: str) -> dict:
    """Run the CLI in-process and return its --json result manifest."""
    try:
        main([*argv, "--json"])
    except SystemExit:
        pass
    return json.loads(capsys.readouterr().out)


@contextmanager
def _files_seen_under(*folders: Path) -> Iterator[dict[Path, set[str]]]:
    """Record every file that appears under each folder while the block runs.
    Sort and collate spills live until their tool finishes, so a few-ms poll
    sees them."""
    seen: dict[Path, set[str]] = {folder: set() for folder in folders}
    done = threading.Event()

    def poll() -> None:
        while not done.wait(0.01):
            for folder, names in seen.items():
                for root, _dirs, files in os.walk(folder):
                    names.update(
                        os.path.relpath(os.path.join(root, f), folder) for f in files
                    )

    watcher = threading.Thread(target=poll)
    watcher.start()
    try:
        yield seen
    finally:
        done.set()
        watcher.join()


@pytest.fixture
def paired_bam(tmp_path) -> Callable[[int], tuple[str, str]]:
    """Make a reference and an indexed BAM of FR pairs mapped to it. The
    default 15k pairs are enough records that a 1 MB sort spills to disk."""

    def make(pairs: int = 15_000) -> tuple[str, str]:
        rng = random.Random(7)
        genome = "".join(rng.choices("ACGT", k=20_000))
        ref = write_fasta(tmp_path / "ref.fa", {"chr1": genome})
        lines = [f"@SQ\tSN:chr1\tLN:{len(genome)}", "@RG\tID:rg\tSM:sample"]
        for i in range(pairs):
            start = rng.randrange(len(genome) - 300)
            mate = start + 200
            lines += [
                f"p{i}\t99\tchr1\t{start + 1}\t60\t100M\t=\t{mate + 1}\t300\t"
                f"{genome[start : start + 100]}\t{'I' * 100}\tRG:Z:rg",
                f"p{i}\t147\tchr1\t{mate + 1}\t60\t100M\t=\t{start + 1}\t-300\t"
                f"{genome[mate : mate + 100]}\t{'I' * 100}\tRG:Z:rg",
            ]
        (tmp_path / "in").mkdir()
        return str(ref), sam_to_bam(lines, tmp_path / "in" / "sample.bam")

    return make


@pytest.fixture
def folders(tmp_path) -> tuple[Path, Path]:
    output, scratch = tmp_path / "out", tmp_path / "scratch"
    output.mkdir()
    scratch.mkdir()
    return output, scratch


def _failure(result: dict) -> str:
    assert result["status"] == "failed", result["error"]
    return result["error"]["message"]


@needs_tools("samtools")
@pytest.mark.parametrize(
    ("command", "spill_gb", "final_gb"),
    # A 4 GiB input: a coordinate sort spills 2x and writes 1x; a name sort
    # (collate) spills 2.45x, and its FASTQs are about the input's size.
    [
        (["sort", "--memory", "1M"], 8, 4),
        (["unalign", "--r1", "a.fq.gz", "--r2", "b.fq.gz", "--memory", "1G"], 9, 4),
    ],
    ids=["coordinate", "name"],
)
@pytest.mark.parametrize("short", [None, "scratch", "output"])
def test_bam_sort_budgets_spills_on_the_tmp_dir_volume_separately(
    capsys, volumes, paired_bam, folders, command, spill_gb, final_gb, short
):
    _, bam = paired_bam()
    output, scratch = folders
    volumes.sizes[Path(bam)] = 4 * GIB
    # Room for exactly one budget per volume; a shared one would need the sum.
    volumes.free[scratch] = spill_gb - (short == "scratch")
    volumes.free[output] = final_gb - (short == "output")
    argv = ["bam", *command, "--input", bam, "--outdir", str(output)]
    with _files_seen_under(scratch, output) as seen:
        result = _wgsextract(capsys, *argv, "--tmp-dir", str(scratch),
                             "--threads", "1")  # fmt: skip
    if short is None:
        assert result["status"] == "ok", result["error"]
        assert {Path(o["path"]).parent for o in result["outputs"]} == {output}
        # The sorter's or collate's spills went to --tmp-dir, not the output.
        assert any(Path(name).parent.name for name in seen[scratch])
        assert all(len(Path(name).parts) == 1 for name in seen[output])
    else:
        message = _failure(result)
        directory, needed = (
            (scratch, spill_gb) if short == "scratch" else (output, final_gb)
        )
        assert str(directory) in message and f"~{needed} GB" in message
        assert not seen[scratch] and not seen[output]
    assert list(scratch.iterdir()) == []


@needs_tools("samtools", "bwa", "minimap2")
@pytest.mark.parametrize(
    ("options", "budgets"),
    # A 4 GiB input: extraction spills 9 GB and writes 4 GB of FASTQ; the
    # alignment writes a 4 GB BAM. (output GB, --tmp-dir GB)
    [
        # Files: the FASTQs are staged on --tmp-dir and deleted afterwards.
        (["--aligner", "minimap2"], (4, 4 + 9)),
        # Kept FASTQs are outputs; extraction spills stay on --tmp-dir.
        (["--aligner", "bwa", "--keep-fastq"], (4 + 4, 9)),
        # Streaming: no FASTQs are written at all.
        (["--aligner", "bwa"], (4, 9)),
    ],
    ids=["files", "keep-fastq", "streaming"],
)
@pytest.mark.parametrize("short", [None, "scratch", "output"])
def test_realign_budgets_output_and_staging_volumes_separately(
    capsys, monkeypatch, volumes, paired_bam, folders, options, budgets, short
):
    # Streaming needs RAM for a collate bucket beside BWA; give every runner
    # (a 7 GB macOS one too) plenty, so only disk space decides.
    real_memory = psutil.virtual_memory
    monkeypatch.setattr(
        psutil, "virtual_memory", lambda: real_memory()._replace(available=64 * GIB)
    )
    # Collate always spills, however few the reads.
    ref, bam = paired_bam(2_000)
    output, scratch = folders
    volumes.sizes[Path(bam)] = 4 * GIB
    volumes.free[output] = budgets[0] - (short == "output")
    volumes.free[scratch] = budgets[1] - (short == "scratch")
    argv = ["realign", "--input", bam, "--ref", ref, "--outdir", str(output)]
    with _files_seen_under(scratch, output) as seen:
        result = _wgsextract(capsys, *argv, *options, "--tmp-dir", str(scratch),
                             "--threads", "1", "--memory", "1G")  # fmt: skip
    if short is None:
        assert result["status"] == "ok", result["error"]
        assert (output / "sample_aligned.bam").is_file()
        # Extraction spills, and staged FASTQs, went to --tmp-dir.
        assert seen[scratch]
        assert all(len(Path(name).parts) == 1 for name in seen[output])
    else:
        message = _failure(result)
        directory, needed = (scratch, budgets[1]) if short == "scratch" else (
            output, budgets[0])  # fmt: skip
        if short == "scratch" and "--keep-fastq" not in options:
            # Streaming that doesn't fit falls back to staging the FASTQs.
            needed = 4 + 9
        assert str(directory) in message and f"~{needed} GB" in message
        assert not (output / "sample_aligned.bam").exists()
    assert list(scratch.iterdir()) == []


@needs_tools("samtools")
@pytest.mark.parametrize("failure", [False, True], ids=["ok", "failed"])
# sambamba sorts BAM where it is installed; samtools sorts CRAM.
@pytest.mark.parametrize("suffix", [".bam", ".cram"])
def test_tmp_dir_overrides_the_configured_one_for_one_run(
    tmp_path, capsys, paired_bam, folders, failure, suffix
):
    ref, source = paired_bam()
    if suffix == ".cram":
        source = str(Path(source).with_suffix(".cram"))
        run_command(["samtools", "view", "-C", "-T", ref, "-o", source,
                     source[: -len(suffix)] + ".bam"])  # fmt: skip
    output, chosen = folders
    gone = json.dumps(str(tmp_path / "gone"))
    config = tmp_path / "config.toml"
    config.write_text(f"temp_directory = {gone}\n")
    if failure:
        # The sort spills, then can't write its output.
        (output / f"sample_sorted{suffix}").mkdir()
    argv = ["bam", "sort", "--input", source, "--ref", ref, "--outdir", str(output),
            "--memory", "1M", "--threads", "1", "--config", str(config)]  # fmt: skip
    assert "gone" in _failure(_wgsextract(capsys, *argv))
    with _files_seen_under(chosen) as seen:
        result = _wgsextract(capsys, *argv, "--tmp-dir", str(chosen))
    assert seen[chosen], "the sort did not spill to --tmp-dir"
    assert result["status"] == ("failed" if failure else "ok")
    assert (output / f"sample_sorted{suffix}").is_file() is not failure
    # The run's workspace is removed, also after a failure, and the override
    # is not saved.
    assert list(chosen.iterdir()) == []
    assert config.read_text() == f"temp_directory = {gone}\n"


def test_child_commands_inherit_the_runs_workspace(tmp_path):
    from wgsextract_cli.core.config import config_args

    with run_temporary_directory(tmp_path):
        workspace = configured_temp_directory()
        assert workspace is not None
        assert Path(workspace).parent == tmp_path
        # contract: a child wgsextract run must spill to the same --tmp-dir.
        assert config_args()[-2:] == ["--tmp-dir", workspace]
    assert list(tmp_path.iterdir()) == []


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
