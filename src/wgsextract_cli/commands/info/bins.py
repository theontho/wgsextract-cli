"""Exact depth-bucket breadth, without materializing per-base depth on disk."""

import csv
import logging
import subprocess
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path

from wgsextract_cli.core.utils import WGSExtractError, popen

from .metrics import IdxStat
from .render import _cram_reference_opts, _resolve_sample_targets


def count_depth_bins(
    lines: Iterable[str], lengths: Mapping[str, int]
) -> dict[str, list[int]]:
    """Infer unreported zeros from lengths; samtools depth emits positive sites."""
    counts = {name: [0, 0, 0, 0] for name in lengths}
    for line in lines:
        fields = line.rstrip("\n").split("\t")
        if len(fields) != 3 or fields[0] not in counts:
            raise WGSExtractError(f"Unexpected samtools depth row: {line[:120]!r}")
        depth = int(fields[2])
        if depth < 0:
            raise WGSExtractError("samtools depth returned a negative depth.")
        if depth:
            bucket = 1 if depth <= 3 else 2 if depth <= 7 else 3
            counts[fields[0]][bucket] += 1
    for name, bins in counts.items():
        bins[0] = lengths[name] - sum(bins[1:])
        if bins[0] < 0:
            raise WGSExtractError(f"Depth sites exceed the length of {name}.")
    return counts


def run_depth_bins(
    input_path: str,
    reference: str | None,
    lengths: Mapping[str, int],
    outdir: str,
    region: str | None = None,
) -> None:
    """One samtools depth pass, default filters, both overlapping mates counted."""
    Path(outdir).mkdir(parents=True, exist_ok=True)
    selected = dict(lengths)
    command = ["samtools", "depth", *_cram_reference_opts(input_path, reference)]
    if region:
        stats: list[IdxStat] = [
            {"name": name, "length": length, "mapped": 0, "unmapped": 0}
            for name, length in lengths.items()
        ]
        name, start, end = _resolve_sample_targets(stats, region)[0]
        selected = {name: end - start + 1}
        command += ["-r", f"{name}:{start}-{end}"]
    command.append(input_path)
    logging.info("Calculating exact depth bins (one streaming pass)...")
    with tempfile.TemporaryFile(dir=outdir) as stderr:
        process = popen(command, stdout=subprocess.PIPE, stderr=stderr, text=True)
        assert process.stdout is not None
        try:
            counts = count_depth_bins(process.stdout, selected)
            if process.wait():
                stderr.seek(0)
                detail = stderr.read().decode(errors="replace")[-4000:]
                raise WGSExtractError(f"samtools depth failed: {detail}")
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()

    # No reuse of potentially stale region/reference/filter-specific results.
    from .render import region_file_tag

    output = Path(
        outdir, Path(input_path).name + region_file_tag(region) + "_coverage_bins.tsv"
    )
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", newline="", dir=outdir, delete=False
        ) as f:
            temporary = Path(f.name)
            writer = csv.writer(f, delimiter="\t", lineterminator="\n")
            writer.writerow(
                [
                    "Seq Name",
                    "Model Len",
                    "Depth 0 bases",
                    "Depth 1-3 bases",
                    "Depth 4-7 bases",
                    "Depth 8+ bases",
                    "Depth 0 fraction",
                    "Depth 1-3 fraction",
                    "Depth 4-7 fraction",
                    "Depth 8+ fraction",
                ]
            )
            for name, bins in counts.items():
                length = selected[name]
                writer.writerow(
                    [
                        name,
                        length,
                        *bins,
                        *(
                            f"{count / length:.8f}" if length else "0.00000000"
                            for count in bins
                        ),
                    ]
                )
        temporary.replace(output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print(output.read_text(), end="")
    logging.info("Depth-bin report saved to %s", output)
