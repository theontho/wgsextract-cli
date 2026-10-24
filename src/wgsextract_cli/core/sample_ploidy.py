"""The sample's sex for bcftools call's sex-aware ploidy.

`bcftools call --ploidy GRCh38` (or a ploidy file) applies its M lines only to
samples listed as male in --samples-file; every other sample is called as F,
so a man's chrY gets ploidy 0 (no calls at all) and his chrX is diploid.
"""

import contextlib
import logging
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

from wgsextract_cli.core.utils import run_command


def alignment_samples(alignment: str) -> list[str]:
    """The sample names bcftools mpileup gives an alignment: its read groups'
    SM, or the file name as given when it has none."""
    try:
        header = run_command(["samtools", "view", "-H", alignment], capture_output=True)
    except (OSError, subprocess.SubprocessError):
        return [alignment]
    names = [
        field[3:]
        for line in header.stdout.splitlines()
        if line.startswith("@RG")
        for field in line.split("\t")
        if field.startswith("SM:")
    ]
    return list(dict.fromkeys(names)) or [alignment]


def _sex_aware(ploidy_args: list[str]) -> bool:
    """Whether the ploidy distinguishes the sexes: a predefined assembly or a
    file with M lines. bcftools rejects a samples file declaring a sex the
    ploidy doesn't (a numeric --ploidy 1 or 2)."""
    if "--ploidy" in ploidy_args:
        return not ploidy_args[ploidy_args.index("--ploidy") + 1].isdigit()
    if "--ploidy-file" in ploidy_args:
        path = ploidy_args[ploidy_args.index("--ploidy-file") + 1]
        try:
            lines = Path(path).read_text().splitlines()
        except OSError:
            return False
        return any(line.split()[3:4] == ["M"] for line in lines if line.strip())
    return False


@contextlib.contextmanager
def sex_samples_args(
    alignment: str, directory: str, ploidy_args: list[str]
) -> Iterator[list[str]]:
    """`--samples-file` listing a male alignment's samples as M (none for a
    female or unknown one, which bcftools already calls as F)."""
    from wgsextract_cli.commands.info.metrics import sample_sex

    if not alignment.lower().endswith((".bam", ".cram")) or not _sex_aware(ploidy_args):
        yield []
        return
    sex = sample_sex(alignment, directory)
    if sex != "Male":
        if sex != "Female":
            logging.warning(
                f"Couldn't tell the sex of {alignment}; chrX/chrY are called as for "
                "a woman."
            )
        yield []
        return
    with tempfile.NamedTemporaryFile(
        "w", suffix=".samples.txt", dir=directory, delete=False
    ) as f:
        f.writelines(f"{name}\tM\n" for name in alignment_samples(alignment))
    try:
        yield ["--samples-file", f.name]
    finally:
        Path(f.name).unlink(missing_ok=True)
