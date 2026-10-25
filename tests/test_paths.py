import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from wgsextract_cli.commands._extract_helpers import cleanup_files, derive_output_path
from wgsextract_cli.core import (
    config,
    samtools_commands,
    tool_commands,
)
from wgsextract_cli.core.paths import absolute_path, append_suffix, expand_path
from wgsextract_cli.core.reference_assets import input_stem, reference_dict_path
from wgsextract_cli.core.reference_resolver import ReferenceLibrary
from wgsextract_cli.core.utils import (
    WGSExtractError,
    _normalize_subprocess_cmd,
    popen,
    run_command,
)
from wgsextract_cli.core.variant_files import ensure_vcf_prepared, verify_paths_exist


class FilePath(os.PathLike[str]):
    def __init__(self, path: Path) -> None:
        self.path = path

    def __fspath__(self) -> str:
        return str(self.path)


@pytest.mark.parametrize(
    "extension", [".vcf.gz", ".vcf.bgz", ".fa.gz", ".fasta.gz", ".tsv.gz", ".bam"]
)
def test_compound_output_stems_keep_sample_dots(tmp_path: Path, extension: str) -> None:
    source = FilePath(tmp_path / f"HG002.chr20{extension.upper()}")
    assert derive_output_path(source, tmp_path, "_result.txt") == str(
        tmp_path / "HG002.chr20_result.txt"
    )
    assert input_stem(source) == "HG002.chr20"


def test_pathlike_reference_and_sidecars(tmp_path: Path) -> None:
    fasta = tmp_path / "hg38.fa.gz"
    fasta.touch()
    append_suffix(fasta, ".fai").write_text("chr1\t248956422\t0\t60\t61\n")
    dictionary = tmp_path / "hg38.dict"
    dictionary.touch()

    library = ReferenceLibrary(FilePath(fasta), skip_full_search=True)
    assert library.fasta == str(fasta)
    assert library.fai == str(fasta) + ".fai"
    assert library.dict_file == str(dictionary)
    assert reference_dict_path(FilePath(fasta)) == str(dictionary)
    verify_paths_exist({"--ref": FilePath(fasta)})
    with pytest.raises(WGSExtractError, match="directory"):
        verify_paths_exist({"--input": FilePath(tmp_path)})


def test_pathlike_vcf_reuses_appended_csi(tmp_path: Path) -> None:
    vcf = tmp_path / "sample.chr20.vcf.bgz"
    vcf.touch()
    append_suffix(vcf, ".csi").touch()
    assert ensure_vcf_prepared(FilePath(vcf)) == str(vcf)
    assert not (tmp_path / "sample.chr20.vcf.csi").exists()


def test_samtools_pathlike_commands_and_alternate_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(samtools_commands, "_use_sambamba", lambda: False)
    bam = FilePath(tmp_path / "sample.bam")
    ref = FilePath(tmp_path / "hg38.fa.gz")
    temp = FilePath(tmp_path / "sort temp")
    index = tmp_path / "sample.bai"
    index.touch()
    assert samtools_commands.alignment_index(bam) == str(index)
    cmd = samtools_commands.get_sam_sort_cmd(
        bam, "2", "1G", fmt="CRAM", reference=ref, temp_dir=temp
    )
    assert cmd == [
        "samtools",
        "sort",
        "-@",
        "2",
        "-m",
        "1G",
        "-o",
        os.fspath(bam),
        "-T",
        os.fspath(temp),
        "-O",
        "CRAM",
        "--reference",
        os.fspath(ref),
    ]
    assert samtools_commands.get_sam_index_cmd(bam) == [
        "samtools",
        "index",
        os.fspath(bam),
    ]
    assert samtools_commands.get_sam_view_cmd(fmt="CRAM", reference=ref)[-2:] == [
        "-T",
        os.fspath(ref),
    ]


@pytest.mark.parametrize("link", ["hardlink", "symlink"])
def test_output_guard_rejects_aliases(tmp_path: Path, link: str) -> None:
    source = tmp_path / "sample.bam"
    source.touch()
    alias = tmp_path / "sample_copy.bam"
    try:
        if link == "hardlink":
            alias.hardlink_to(source)
        else:
            alias.symlink_to(source)
    except OSError as exc:
        pytest.skip(f"{link} unavailable: {exc}")
    with pytest.raises(WGSExtractError, match="overwrite"):
        derive_output_path(FilePath(source), tmp_path, "_copy.bam")


@pytest.mark.parametrize("fail", [False, True])
def test_cleanup_pathlike_files(tmp_path: Path, fail: bool) -> None:
    temp = tmp_path / "temporary.bam"
    output = tmp_path / "result.bam"
    temp.touch()
    output.touch()
    try:
        with cleanup_files(
            temp=[FilePath(temp), tmp_path / "already gone"],
            on_failure=[FilePath(output)],
        ):
            if fail:
                raise ValueError("incomplete output")
    except ValueError:
        assert fail
    assert not temp.exists()
    assert output.exists() is not fail


def test_pathlike_process_arguments_and_cwd(tmp_path: Path) -> None:
    argument = FilePath(tmp_path / "output with spaces.vcf.gz")
    index_argument = f"{os.fspath(argument)}##idx##{os.fspath(argument)}.csi"
    command = [
        FilePath(Path(sys.executable)),
        "-c",
        "import os, sys; print(os.getcwd()); print(sys.argv[1]); print(sys.argv[2])",
        argument,
        index_argument,
    ]
    expected = [str(tmp_path), os.fspath(argument), index_argument]
    result = run_command(command, capture_output=True, cwd=FilePath(tmp_path))
    assert result.stdout.splitlines() == expected
    process = popen(command, stdout=subprocess.PIPE, text=True, cwd=tmp_path)
    stdout, _ = process.communicate()
    assert process.returncode == 0
    assert stdout.splitlines() == expected


def test_literal_pathlike_executable_is_not_split(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = FilePath(tmp_path / "missing tool suite" / "tool")
    assert _normalize_subprocess_cmd(executable) == [os.fspath(executable)]
    assert _normalize_subprocess_cmd([executable, "--version"]) == [
        os.fspath(executable),
        "--version",
    ]


def test_long_wrapper_command_is_not_treated_as_a_filename(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = "pixi run -e default samtools --argument=" + "a" * 300
    expected = shlex.split(command)
    assert tool_commands._tool_command_parts(command) == expected
    assert _normalize_subprocess_cmd([command]) == expected


def test_expansion_preserves_command_syntax_and_empty_settings() -> None:
    command = r'pixi run --manifest-path "C:\Tool Suite\pixi.toml" samtools'
    assert expand_path(command) == command
    assert expand_path("") == ""
    assert expand_path("~/reference") == str(Path.home() / "reference")


def test_unresolved_config_home_does_not_block_cli_startup() -> None:
    missing = "~wgsextract-no-such-user-101/reference"
    settings_file = config.get_config_path()
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text(f'reference_library = "{missing}"\n', encoding="utf-8")
    result = run_command(
        [sys.executable, "-m", "wgsextract_cli.main", "config"],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert missing in result.stdout
    assert "Could not expand home directory" in result.stderr


def test_absolute_path_normalizes_parents_without_following_links(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")
    assert absolute_path(FilePath(alias / "data")) == alias / "data"
    assert absolute_path(tmp_path / "unused" / ".." / "data") == tmp_path / "data"


def test_verify_paths_exist_wants_files_not_programs_or_folders(tmp_path):
    from wgsextract_cli.core.utils import WGSExtractError
    from wgsextract_cli.core.variant_files import verify_paths_exist

    (tmp_path / "x.bam").write_text("x")
    verify_paths_exist({"--input": str(tmp_path / "x.bam")})
    with pytest.raises(WGSExtractError, match="not found"):
        verify_paths_exist({"--ref": "sh"})  # a program on PATH isn't a file
    with pytest.raises(WGSExtractError, match="is a directory"):
        verify_paths_exist({"--ref": str(tmp_path)})
