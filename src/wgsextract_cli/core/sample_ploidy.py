"""The sample's sex for bcftools call's sex-aware ploidy.

`bcftools call --ploidy GRCh38` (or a ploidy file) applies its M lines only to
samples listed as male in --samples-file; every other sample is called as F,
so a man's chrY gets ploidy 0 (no calls at all) and his chrX is diploid.
"""

import contextlib
import logging
import re
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


def _region_contigs(region: str | None) -> set[str] | None:
    """Contigs in a plain bcftools region list; unknown syntax stays sex-aware."""
    if not region:
        return None
    contigs = set()
    for part in region.split(","):
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)(?::[0-9]+(?:-[0-9]*)?)?", part.strip())
        if match is None:
            return None
        contigs.add(match[1])
    return contigs


def _sex_aware(ploidy_args: list[str], region: str | None = None) -> bool:
    """Whether the ploidy distinguishes the sexes: a predefined assembly or a
    file with M lines. bcftools rejects a samples file declaring a sex the
    ploidy doesn't (a numeric --ploidy 1 or 2)."""
    contigs = _region_contigs(region)
    if "--ploidy" in ploidy_args:
        preset = ploidy_args[ploidy_args.index("--ploidy") + 1]
        if preset.isdigit():
            return False
        if contigs is not None and preset in {"GRCh37", "GRCh38"}:
            autosomes = {str(i) for i in range(1, 23)} | {"m", "mt"}
            return any(c.lower().removeprefix("chr") not in autosomes for c in contigs)
        return True
    if "--ploidy-file" in ploidy_args:
        path = ploidy_args[ploidy_args.index("--ploidy-file") + 1]
        try:
            lines = Path(path).read_text().splitlines()
        except OSError:
            return False
        rows = [
            line.split()
            for line in lines
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if not any(row[3:4] == ["M"] for row in rows):
            return False
        if contigs is None:
            return True
        for contig in contigs:
            relevant = [row for row in rows if row[0] in {"*", contig}]
            # Be conservative about malformed/custom tables and overlapping
            # rules: only skip a scan when all applicable rules agree, and
            # every sex-specific interval has an identical opposite-sex rule.
            if any(len(row) != 5 for row in relevant):
                return True
            if len({row[4] for row in relevant}) > 1:
                return True
            rules = {tuple(row) for row in relevant}
            for chrom, start, end, sex, ploidy in rules:
                if sex == "*":
                    continue
                opposite = {"M": "F", "F": "M"}.get(sex)
                if (
                    opposite is None
                    or (chrom, start, end, opposite, ploidy) not in rules
                ):
                    return True
        return False
    return False


@contextlib.contextmanager
def sex_samples_args(
    alignment: str, directory: str, ploidy_args: list[str], *, region: str | None = None
) -> Iterator[list[str]]:
    """`--samples-file` listing a male alignment's samples as M (none for a
    female or unknown one, which bcftools already calls as F). Regions whose
    ploidy is the same for both sexes need no whole-alignment sex scan."""
    from wgsextract_cli.commands.info.metrics import sample_sex

    if not alignment.lower().endswith((".bam", ".cram")) or not _sex_aware(
        ploidy_args, region
    ):
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
