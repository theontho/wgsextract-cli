import argparse
import gzip
import io
import logging
import os
import signal
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from typing import IO

from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.utils import (
    WGSExtractError,
    popen,
    run_command,
)
from wgsextract_cli.core.variant_files import (
    ensure_vcf_indexed,
    verify_paths_exist,
)

from .basic import get_base_args, plain_reference

DELLY_MACOS_HINT = (
    "Hint: If using macOS, ensure 'delly' and 'boost' are correctly installed "
    "via Homebrew."
)


def _cram_args(input_path: str, ref: str) -> list[str]:
    return ["-T", ref] if input_path.lower().endswith(".cram") else []


@contextmanager
def _region_input_bam(
    args: argparse.Namespace, outdir: str, ref: str, failure_label: str
) -> Iterator[str]:
    """Yield args.input, or a temporary indexed BAM of --region removed on exit."""
    if not getattr(args, "region", None):
        yield args.input
        return

    with tempfile.TemporaryDirectory(dir=outdir, prefix=".wgse_region_") as tmp:
        temp_bam = os.path.join(tmp, "region.bam")
        logging.info(f"Extracting region {args.region} to temporary BAM...")
        try:
            _extract_region_bam_with_reference_header(args, ref, temp_bam)
            run_command(["samtools", "index", temp_bam], capture_output=True)
        except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
            raise WGSExtractError(
                f"{failure_label} region extraction failed: {e}"
            ) from e
        yield temp_bam


def _reference_contigs(ref: str) -> list[tuple[str, int]]:
    fai_path = ref + ".fai"
    if not os.path.isfile(fai_path):
        run_command(["samtools", "faidx", ref], capture_output=True)

    contigs = []
    with open(fai_path, encoding="utf-8") as fai:
        for line in fai:
            fields = line.rstrip("\n").split("\t")
            if len(fields) >= 2:
                contigs.append((fields[0], int(fields[1])))
    if not contigs:
        raise WGSExtractError(f"Reference index has no contigs: {fai_path}")
    return contigs


def _reference_sq_lines(ref: str) -> list[str]:
    return [
        f"@SQ\tSN:{name}\tLN:{length}\n" for name, length in _reference_contigs(ref)
    ]


def _write_reference_header(
    sink: IO[str], non_sq_header_lines: list[str], reference_sq_lines: list[str]
) -> None:
    hd_lines = [line for line in non_sq_header_lines if line.startswith("@HD")]
    other_header_lines = [
        line for line in non_sq_header_lines if not line.startswith("@HD")
    ]
    for line in hd_lines:
        sink.write(line)
    for line in reference_sq_lines:
        sink.write(line)
    for line in other_header_lines:
        sink.write(line)


def _stderr_tail(stream: IO[bytes], max_lines: int = 20) -> str:
    stream.seek(0)
    lines = stream.read().decode(errors="replace").strip().splitlines()
    return "\n".join(lines[-max_lines:])


def _extract_region_bam_with_reference_header(
    args: argparse.Namespace, ref: str, temp_bam: str
) -> None:
    """
    Write args.region as BAM whose @SQ lines are exactly the reference's contigs.

    Delly rejects BAMs listing contigs the reference lacks (e.g. an hs38DH CRAM
    called against hg38). Records are passed as SAM text so contigs are remapped
    by name; Python only writes the header and never touches the records.
    """
    cram_args = _cram_args(args.input, ref)
    header = run_command(
        ["samtools", "view", "-H", "--no-PG", *cram_args, args.input],
        capture_output=True,
    ).stdout
    non_sq = [
        line + "\n"
        for line in header.splitlines()
        if line and not line.startswith("@SQ")
    ]
    new_header = io.StringIO()
    _write_reference_header(new_header, non_sq, _reference_sq_lines(ref))

    with tempfile.TemporaryFile() as source_err, tempfile.TemporaryFile() as sink_err:
        sink = popen(
            ["samtools", "view", "-b", "-o", temp_bam, "-"],
            stdin=subprocess.PIPE,
            stderr=sink_err,
        )
        source = None
        assert sink.stdin is not None
        try:
            try:
                sink.stdin.write(new_header.getvalue().encode())
                sink.stdin.flush()
                # Records only (no -h): they follow our header in the same pipe.
                source = popen(
                    ["samtools", "view", *cram_args, args.input, args.region],
                    stdout=sink.stdin,
                    stderr=source_err,
                )
            finally:
                sink.stdin.close()
            assert source is not None
            source_rc = source.wait()
            sink_rc = sink.wait()
        finally:
            for proc in (source, sink):
                if proc is not None and proc.poll() is None:
                    proc.kill()
                    proc.wait()

        if source_rc != 0:
            raise WGSExtractError(
                f"samtools view failed (exit {source_rc}): {_stderr_tail(source_err)}"
            )
        if sink_rc != 0:
            raise WGSExtractError(
                f"samtools BAM write failed (exit {sink_rc}): {_stderr_tail(sink_err)}"
            )


def _validate_delly_map(map_file: str) -> None:
    if not map_file.endswith(".gz"):
        return
    try:
        with gzip.open(map_file, "rb") as handle:
            handle.read(1)
    except (OSError, EOFError) as e:
        raise WGSExtractError(
            f"Mappability map is not a valid gzip-compressed file: {map_file}"
        ) from e


def _write_indexed_vcf_from_bcf(out_bcf: str, out_vcf: str) -> None:
    run_command(["bcftools", "view", "-Oz", "-o", out_vcf, out_bcf])
    ensure_vcf_indexed(out_vcf)
    for path in (out_bcf, out_bcf + ".csi"):
        if os.path.exists(path):
            os.remove(path)


def cmd_cnv(args: argparse.Namespace) -> None:
    verify_dependencies(["delly", "bcftools", "tabix", "samtools"])
    _threads, outdir, ref, lib = get_base_args(args)

    out_bcf = os.path.join(outdir, "cnv.bcf")
    out_vcf = os.path.join(outdir, "cnv.vcf.gz")
    logging.info(LOG_MESSAGES["vcf_calling_cnv"].format(output=out_vcf))

    map_file = getattr(args, "map", None) or getattr(lib, "mappability_map", None)
    if not map_file or not os.path.exists(map_file):
        raise WGSExtractError(
            "Mappability map (-M/--map) is required for delly cnv.\n"
            "You can download standard maps (e.g., hg38.map.gz) from:\n"
            "https://github.com/dellytools/delly/tree/master/exclude\n"
            "Or provide a custom .map file."
        )
    _validate_delly_map(map_file)

    map_args = ["-m", map_file]
    if getattr(args, "ploidy", None) is not None:
        map_args.extend(["-y", str(args.ploidy)])

    with _region_input_bam(args, outdir, ref, "CNV") as input_file:
        try:
            run_command(
                ["delly", "cnv", "-g", ref, "-o", out_bcf, *map_args, input_file]
            )
        except subprocess.CalledProcessError as e:
            if e.returncode == -signal.SIGSEGV:
                logging.error(
                    "Delly segfaulted. This is common on macOS due to incompatible "
                    "shared libraries (boost/htslib) in some environments.\n"
                    "Hint: Try 'brew install delly' or run in a Linux container."
                )
            else:
                logging.error(DELLY_MACOS_HINT)
            raise WGSExtractError(
                f"CNV calling failed with exit code {e.returncode}"
            ) from e
    _write_indexed_vcf_from_bcf(out_bcf, out_vcf)


def cmd_sv_pbsv(args: argparse.Namespace) -> None:
    verify_dependencies(["pbsv", "bcftools", "tabix", "samtools"])
    log_dependency_info(["pbsv", "bcftools", "tabix", "samtools"])
    threads, outdir, ref, _lib = get_base_args(args)

    out_vcf = os.path.join(outdir, "pbsv.vcf.gz")
    region = getattr(args, "region", None)
    tandem_repeats = getattr(args, "tandem_repeats", None)
    if tandem_repeats:
        verify_paths_exist({"--tandem-repeats": tandem_repeats})

    logging.info(LOG_MESSAGES["vcf_calling_sv"].format(output=out_vcf))
    with (
        tempfile.TemporaryDirectory(dir=outdir, prefix=".wgse_pbsv_") as work,
        # pbsv call does not accept .fa.gz input.
        plain_reference(ref, outdir) as pbsv_ref,
    ):
        svsig = os.path.join(work, "pbsv.svsig.gz")
        raw_vcf = os.path.join(work, "pbsv.vcf")
        discover_cmd = ["pbsv", "discover"]
        if region:
            discover_cmd.extend(["--region", region])
        if tandem_repeats:
            discover_cmd.extend(["--tandem-repeats", tandem_repeats])
        if getattr(args, "ccs", False):
            discover_cmd.append("--hifi")
        discover_cmd.extend([args.input, svsig])

        call_cmd = ["pbsv", "call", "-j", threads]
        if region:
            call_cmd.extend(["--region", region])
        if getattr(args, "ccs", False):
            call_cmd.append("--ccs")
        call_cmd.extend([pbsv_ref, svsig, raw_vcf])
        try:
            run_command(discover_cmd)
            run_command(call_cmd)
        except subprocess.CalledProcessError as e:
            raise WGSExtractError(
                f"PacBio SV calling failed with exit code {e.returncode}"
            ) from e
        run_command(["bcftools", "view", "-Oz", "-o", out_vcf, raw_vcf])
    ensure_vcf_indexed(out_vcf)


def _write_region_bed(region: str, ref: str, bed_path: str) -> None:
    """Convert a samtools-style region (chr, chr:start, chr:start-end) to BED."""
    chrom, _, span = region.partition(":")
    lengths = dict(_reference_contigs(ref))
    if chrom not in lengths:
        raise WGSExtractError(f"Region contig {chrom!r} is not in reference {ref}")
    start_text, _, end_text = span.replace(",", "").partition("-")
    try:
        start = int(start_text) if start_text else 1
        end = int(end_text) if end_text else lengths[chrom]
    except ValueError as e:
        raise WGSExtractError(f"Invalid region: {region}") from e
    if start < 1 or end < start:
        raise WGSExtractError(f"Invalid region: {region}")
    with open(bed_path, "w") as f:
        f.write(f"{chrom}\t{start - 1}\t{end}\n")


def cmd_sv_sniffles(args: argparse.Namespace) -> None:
    verify_dependencies(["sniffles", "bcftools", "tabix", "samtools"])
    log_dependency_info(["sniffles", "bcftools", "tabix", "samtools"])
    threads, outdir, ref, _lib = get_base_args(args)

    out_vcf = os.path.join(outdir, "sniffles.vcf.gz")
    region = getattr(args, "region", None)

    logging.info(LOG_MESSAGES["vcf_calling_sv"].format(output=out_vcf))
    with tempfile.TemporaryDirectory(dir=outdir, prefix=".wgse_sniffles_") as work:
        raw_vcf = os.path.join(work, "sniffles.vcf")
        # --reference is needed to decode CRAM and to emit deletion sequences.
        cmd = ["sniffles", "--input", args.input, "--vcf", raw_vcf]
        cmd += ["--reference", ref, "--threads", threads]
        if region:
            bed_path = os.path.join(work, "region.bed")
            _write_region_bed(region, ref, bed_path)
            cmd.extend(["--regions", bed_path])
        try:
            run_command(cmd)
        except subprocess.CalledProcessError as e:
            raise WGSExtractError(
                f"Sniffles SV calling failed with exit code {e.returncode}"
            ) from e
        run_command(["bcftools", "view", "-Oz", "-o", out_vcf, raw_vcf])
    ensure_vcf_indexed(out_vcf)


def _select_sv_caller(args: argparse.Namespace) -> str:
    caller = getattr(args, "caller", None)
    if not getattr(args, "pacbio", False):
        return caller or "delly"
    if caller == "delly":
        raise WGSExtractError(
            "--pacbio selects a long-read SV caller (pbsv or sniffles) and cannot "
            "be combined with --caller delly."
        )
    args.ccs = True
    return caller or ("pbsv" if get_tool_path("pbsv") is not None else "sniffles")


def cmd_sv(args: argparse.Namespace) -> None:
    args.caller = _select_sv_caller(args)
    if args.caller == "pbsv":
        cmd_sv_pbsv(args)
        return
    if args.caller == "sniffles":
        cmd_sv_sniffles(args)
        return

    verify_dependencies(["delly", "bcftools", "tabix", "samtools"])
    _threads, outdir, ref, _lib = get_base_args(args)

    out_bcf = os.path.join(outdir, "sv.bcf")
    out_vcf = os.path.join(outdir, "sv.vcf.gz")
    logging.info(LOG_MESSAGES["vcf_calling_sv"].format(output=out_vcf))

    with _region_input_bam(args, outdir, ref, "SV") as input_file:
        try:
            run_command(["delly", "call", "-g", ref, "-o", out_bcf, input_file])
        except subprocess.CalledProcessError as e:
            logging.error(DELLY_MACOS_HINT)
            raise WGSExtractError(
                f"SV calling failed with exit code {e.returncode}"
            ) from e
    _write_indexed_vcf_from_bcf(out_bcf, out_vcf)
