"""Exact depth-bucket breadth, without materializing per-base depth on disk."""

import csv
import logging
import re
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import IO

from wgsextract_cli.core.results import human_output
from wgsextract_cli.core.utils import WGSExtractError, popen

from .metrics import IdxStat
from .render import _cram_reference_opts, _resolve_sample_targets

_DEPTH_CHUNK_BYTES = 8 << 20
_DEPTH_DIGITS = b"0123456789"
_DEPTH_BUCKETS = bytes.maketrans(b"123456789", b"111444488")


def _count_depth_block(data: bytes, counts: Mapping[bytes, list[int]]) -> None:
    if b"\r" in data:
        data = data.replace(b"\r\n", b"\n")
    start = 0
    mixed = False
    rows = data.count(b"\n")
    while start < len(data):
        first_end = data.index(b"\n", start)
        separator = data.find(b"\t", start, first_end)
        name = data[start:separator] if separator >= 0 else b""
        if separator < 0 or name not in counts:
            raise WGSExtractError(
                f"Unexpected samtools depth row: {data[start : start + 120]!r}"
            )
        prefix = name + b"\t"
        end = len(data)
        if start == 0:
            mixed = data.count(b"\n" + prefix) != rows - 1
        if mixed:
            # Most chunks contain one contig. Only boundary chunks need a
            # line-start search, performed in C rather than iterating rows.
            transition = re.compile(rb"(?m)^(?!" + re.escape(prefix) + rb")")
            match = transition.search(data, start)
            assert match is not None
            end = match.start()
        block = data[start:end]
        if mixed:
            rows = block.count(b"\n")
        zero = block.count(b"\t0\n")
        # Removing digits leaves the name's non-digits and two tabs per row.
        # This validates fields without allocating a name-stripped copy.
        shape = name.translate(None, _DEPTH_DIGITS) + b"\t\t\n"
        if (
            block.translate(None, _DEPTH_DIGITS) != shape * rows
            or b"\t\t" in block
            or b"\t\n" in block
            or block.count(b"\t0") != zero
        ):
            if b"\t-" in block:
                raise WGSExtractError("samtools depth returned a negative depth.")
            raise WGSExtractError(
                f"Unexpected samtools depth row: {data[start : start + 120]!r}"
            )
        bucketed = block.translate(_DEPTH_BUCKETS)
        low = bucketed.count(b"\t1\n")
        medium = bucketed.count(b"\t4\n")
        bins = counts[name]
        bins[1] += low
        bins[2] += medium
        bins[3] += rows - zero - low - medium
        start = end


def count_depth_bins(
    stream: IO[bytes], lengths: Mapping[str, int]
) -> dict[str, list[int]]:
    """Count binary samtools output in chunks; infer unreported zero depths."""
    counts = {name: [0, 0, 0, 0] for name in lengths}
    encoded = {name.encode(): bins for name, bins in counts.items()}
    pending = b""
    while chunk := stream.read(_DEPTH_CHUNK_BYTES):
        data = pending + chunk
        end = data.rfind(b"\n") + 1
        if end:
            _count_depth_block(data[:end], encoded)
        pending = data[end:]
        if len(pending) > _DEPTH_CHUNK_BYTES:
            raise WGSExtractError("Unexpected samtools depth row: line too long.")
    if pending:
        _count_depth_block(pending + b"\n", encoded)
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
) -> Path:
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
        process = popen(command, stdout=subprocess.PIPE, stderr=stderr)
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
    human_output(output.read_text(), end="")
    logging.info("Depth-bin report saved to %s", output)
    return output
