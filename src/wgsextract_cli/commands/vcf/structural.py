import argparse
import gzip
import logging
import os
import shutil
import subprocess
from typing import IO

from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.utils import (
    WGSExtractError,
    run_command,
)
from wgsextract_cli.core.variant_files import (
    ensure_vcf_indexed,
    popen,
    verify_paths_exist,
)

from .basic import (
    get_base_args,
)


def _region_input_bam(
    args: argparse.Namespace, outdir: str, ref: str, failure_label: str
) -> tuple[str, str | None]:
    temp_bam = None
    if not getattr(args, "region", None):
        return args.input, temp_bam

    import tempfile

    fd, temp_bam = tempfile.mkstemp(suffix=".bam", dir=outdir)
    os.close(fd)
    logging.info(f"Extracting region {args.region} to temporary BAM...")
    try:
        _extract_region_bam_with_reference_header(args, ref, temp_bam)
        samtools = get_tool_path("samtools")
        run_command([samtools, "index", temp_bam])
        return temp_bam, temp_bam
    except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
        logging.error(f"Failed to extract region: {e}")
        if temp_bam and os.path.exists(temp_bam):
            os.remove(temp_bam)
        raise WGSExtractError(f"{failure_label} region extraction failed.") from e


def _reference_sq_lines(ref: str) -> list[str]:
    fai_path = ref + ".fai"
    if not os.path.isfile(fai_path):
        run_command([get_tool_path("samtools"), "faidx", ref])

    sq_lines = []
    with open(fai_path, encoding="utf-8") as fai:
        for line in fai:
            fields = line.rstrip("\n").split("\t")
            if len(fields) >= 2:
                sq_lines.append(f"@SQ\tSN:{fields[0]}\tLN:{fields[1]}\n")
    if not sq_lines:
        raise WGSExtractError(f"Reference index has no contigs: {fai_path}")
    return sq_lines


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


def _terminate_processes(processes: tuple[subprocess.Popen, ...]) -> None:
    for process in processes:
        try:
            if process.poll() is None:
                process.terminate()
        except OSError as e:
            logging.warning(f"Failed to terminate process {process.pid}: {e}")
    for process in processes:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.wait()
            except OSError as e:
                logging.warning(f"Failed to kill process {process.pid}: {e}")
        except OSError as e:
            logging.warning(f"Failed to wait for process {process.pid}: {e}")


def _extract_region_bam_with_reference_header(
    args: argparse.Namespace, ref: str, temp_bam: str
) -> None:
    samtools = get_tool_path("samtools")
    if samtools is None:
        raise WGSExtractError("samtools dependency is required for region extraction.")
    source_cmd = [samtools, "view", "-h"]
    if args.input.lower().endswith(".cram"):
        source_cmd.extend(["-T", ref])
    source_cmd.extend([args.input, args.region])
    sink_cmd = [samtools, "view", "-bh", "-o", temp_bam, "-"]

    reference_sq = _reference_sq_lines(ref)
    source = popen(
        source_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    sink = popen(sink_cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if source.stdout is None or sink.stdin is None:
        _terminate_processes((source, sink))
        raise WGSExtractError("Failed to open samtools region extraction pipeline.")

    try:
        non_sq_header_lines = []
        first_alignment = None
        for line in source.stdout:
            if line.startswith("@"):
                if not line.startswith("@SQ"):
                    non_sq_header_lines.append(line)
                continue
            first_alignment = line
            break

        _write_reference_header(sink.stdin, non_sq_header_lines, reference_sq)
        if first_alignment is not None:
            sink.stdin.write(first_alignment)
        for line in source.stdout:
            sink.stdin.write(line)

        source_stderr = source.stderr.read() if source.stderr is not None else ""
        sink.stdin.close()
        sink_stderr = sink.stderr.read() if sink.stderr is not None else ""
        source_rc = source.wait()
        sink_rc = sink.wait()
    except (OSError, subprocess.SubprocessError, WGSExtractError):
        if sink.stdin and not sink.stdin.closed:
            try:
                sink.stdin.close()
            except OSError as e:
                logging.warning(f"Failed to close samtools sink stdin: {e}")
        _terminate_processes((source, sink))
        raise

    if source_rc != 0:
        raise WGSExtractError(f"samtools view failed: {source_stderr.strip()}")
    if sink_rc != 0:
        raise WGSExtractError(f"samtools BAM write failed: {sink_stderr.strip()}")


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
    bcftools = get_tool_path("bcftools")
    run_command([bcftools, "view", "-Oz", "-o", out_vcf, out_bcf])
    ensure_vcf_indexed(out_vcf)
    if os.path.exists(out_bcf):
        os.remove(out_bcf)


def cmd_cnv(args: argparse.Namespace) -> None:
    verify_dependencies(["delly", "bcftools", "tabix", "samtools"])
    base = get_base_args(args)
    if not base:
        return
    threads, outdir, ref, lib = base

    out_bcf = os.path.join(outdir, "cnv.bcf")
    out_vcf = os.path.join(outdir, "cnv.vcf.gz")

    logging.info(LOG_MESSAGES["vcf_calling_cnv"].format(output=out_vcf))

    # Auto-resolve mappability map if possible
    map_file = (
        args.map
        if getattr(args, "map", None)
        else getattr(lib, "mappability_map", None)
    )

    if not map_file or not os.path.exists(map_file):
        msg = "Mappability map (-M/--map) is required for delly cnv.\nYou can download standard maps (e.g., hg38.map.gz) from:\nhttps://github.com/dellytools/delly/tree/master/exclude\nOr provide a custom .map file."
        logging.error(f"❌: {msg}")
        raise WGSExtractError(msg) from None
    _validate_delly_map(map_file)

    map_args = ["-m", map_file]
    if getattr(args, "ploidy", None):
        map_args.extend(["-y", args.ploidy])

    input_file, temp_bam = _region_input_bam(args, outdir, ref, "CNV")

    try:
        # delly cnv -g ref.fa -o cnv.bcf input.bam
        delly = get_tool_path("delly")
        cmd = [delly, "cnv", "-g", ref, "-o", out_bcf] + map_args + [input_file]
        run_command(cmd)
        _write_indexed_vcf_from_bcf(out_bcf, out_vcf)
    except subprocess.CalledProcessError as e:
        logging.error(f"CNV calling failed: {e}")
        if e.returncode < 0:
            import signal

            if abs(e.returncode) == signal.SIGSEGV:
                logging.error("❌: Delly segfaulted (Segmentation Fault).")
                logging.error(
                    "     This is common on macOS due to incompatible shared libraries (boost/htslib) in some environments."
                )
                logging.error(
                    "     Hint: Try 'brew install delly' or run in a Linux container."
                )
        else:
            logging.error(
                "Hint: If using macOS, ensure 'delly' and 'boost' are correctly installed via Homebrew."
            )
        raise WGSExtractError(
            f"CNV calling failed with exit code {e.returncode}"
        ) from e
    finally:
        if temp_bam and os.path.exists(temp_bam):
            os.remove(temp_bam)
            if os.path.exists(temp_bam + ".bai"):
                os.remove(temp_bam + ".bai")


def _ensure_fasta_index(ref: str) -> None:
    if os.path.isfile(ref + ".fai"):
        return
    samtools = get_tool_path("samtools")
    run_command([samtools, "faidx", ref])


def _prepare_pbsv_reference(ref: str, outdir: str) -> str:
    """Return a plain FASTA path because pbsv call does not accept .fa.gz input."""
    if not ref.lower().endswith(".gz"):
        return ref

    sibling = ref[:-3]
    if os.path.isfile(sibling):
        _ensure_fasta_index(sibling)
        return sibling

    os.makedirs(outdir, exist_ok=True)
    target = os.path.join(outdir, os.path.basename(sibling))
    if not os.path.isfile(target):
        logging.info(f"Creating uncompressed reference for pbsv: {target}")
        with gzip.open(ref, "rb") as source, open(target, "wb") as destination:
            shutil.copyfileobj(source, destination)
    _ensure_fasta_index(target)
    return target


def cmd_sv_pbsv(args: argparse.Namespace) -> None:
    verify_dependencies(["pbsv", "bcftools", "tabix", "samtools"])
    log_dependency_info(["pbsv", "bcftools", "tabix", "samtools"])
    base = get_base_args(args)
    if not base:
        return
    threads, outdir, ref, lib = base
    pbsv_ref = _prepare_pbsv_reference(ref, outdir)

    out_vcf = os.path.join(outdir, "pbsv.vcf.gz")
    svsig = os.path.join(outdir, "pbsv.svsig.gz")
    raw_vcf = os.path.join(outdir, "pbsv.vcf")
    region = getattr(args, "region", None)
    tandem_repeats = getattr(args, "tandem_repeats", None)

    logging.info(LOG_MESSAGES["vcf_calling_sv"].format(output=out_vcf))
    try:
        pbsv = get_tool_path("pbsv")
        discover_cmd = [pbsv, "discover"]
        if region:
            discover_cmd.extend(["--region", region])
        if tandem_repeats:
            if not verify_paths_exist({"--tandem-repeats": tandem_repeats}):
                return
            discover_cmd.extend(["--tandem-repeats", tandem_repeats])
        if getattr(args, "ccs", False):
            discover_cmd.append("--hifi")
        discover_cmd.extend([args.input, svsig])
        run_command(discover_cmd)

        call_cmd = [pbsv, "call", "-j", threads]
        if region:
            call_cmd.extend(["--region", region])
        if getattr(args, "ccs", False):
            call_cmd.append("--ccs")
        call_cmd.extend([pbsv_ref, svsig, raw_vcf])
        run_command(call_cmd)

        bcftools = get_tool_path("bcftools")
        run_command([bcftools, "view", "-Oz", "-o", out_vcf, raw_vcf], check=True)
        ensure_vcf_indexed(out_vcf)
    except subprocess.CalledProcessError as e:
        logging.error(f"PacBio SV calling failed: {e}")
        raise WGSExtractError(
            f"PacBio SV calling failed with exit code {e.returncode}"
        ) from e


def cmd_sv_sniffles(args: argparse.Namespace) -> None:
    verify_dependencies(["sniffles", "bcftools", "tabix", "samtools"])
    log_dependency_info(["sniffles", "bcftools", "tabix", "samtools"])
    base = get_base_args(args)
    if not base:
        return
    threads, outdir, _ref, _lib = base

    raw_vcf = os.path.join(outdir, "sniffles.vcf")
    out_vcf = os.path.join(outdir, "sniffles.vcf.gz")
    region = getattr(args, "region", None)

    logging.info(LOG_MESSAGES["vcf_calling_sv"].format(output=out_vcf))
    try:
        sniffles = get_tool_path("sniffles")
        cmd = [
            sniffles,
            "--input",
            args.input,
            "--vcf",
            raw_vcf,
            "--threads",
            threads,
        ]
        if region:
            cmd.extend(["--regions", region])
        run_command(cmd)

        bcftools = get_tool_path("bcftools")
        run_command([bcftools, "view", "-Oz", "-o", out_vcf, raw_vcf], check=True)
        ensure_vcf_indexed(out_vcf)
    except subprocess.CalledProcessError as e:
        logging.error(f"Sniffles SV calling failed: {e}")
        raise WGSExtractError(
            f"Sniffles SV calling failed with exit code {e.returncode}"
        ) from e


def cmd_sv(args: argparse.Namespace) -> None:
    if getattr(args, "pacbio", False):
        args.caller = "pbsv" if get_tool_path("pbsv") is not None else "sniffles"
        args.ccs = True
    if getattr(args, "caller", "delly") == "pbsv":
        cmd_sv_pbsv(args)
        return
    if getattr(args, "caller", "delly") == "sniffles":
        cmd_sv_sniffles(args)
        return

    verify_dependencies(["delly", "bcftools", "tabix", "samtools"])
    base = get_base_args(args)
    if not base:
        return
    threads, outdir, ref, lib = base

    out_bcf = os.path.join(outdir, "sv.bcf")
    out_vcf = os.path.join(outdir, "sv.vcf.gz")

    logging.info(LOG_MESSAGES["vcf_calling_sv"].format(output=out_vcf))

    input_file, temp_bam = _region_input_bam(args, outdir, ref, "SV")

    try:
        # delly call -g ref.fa -o sv.bcf input.bam
        delly = get_tool_path("delly")
        cmd = [delly, "call", "-g", ref, "-o", out_bcf, input_file]
        run_command(cmd)
        _write_indexed_vcf_from_bcf(out_bcf, out_vcf)
    except subprocess.CalledProcessError as e:
        logging.error(f"SV calling failed: {e}")
        logging.error(
            "Hint: If using macOS, ensure 'delly' and 'boost' are correctly installed via Homebrew."
        )
        raise WGSExtractError(f"SV calling failed with exit code {e.returncode}") from e
    finally:
        if temp_bam and os.path.exists(temp_bam):
            os.remove(temp_bam)
            if os.path.exists(temp_bam + ".bai"):
                os.remove(temp_bam + ".bai")


def _exit_if_missing(
    file_path: str | None, message_key: str, ann_name: str | None = None
) -> None:
    if not file_path:
        msg = LOG_MESSAGES.get(message_key, f"Missing data file for {ann_name}")
        logging.error(f"REQUIRED DATA MISSING: {msg}")
        logging.info(
            f"To fix this, run: wgsextract ref {ann_name or message_key.split('_')[1]}"
        )
        raise WGSExtractError(msg)
