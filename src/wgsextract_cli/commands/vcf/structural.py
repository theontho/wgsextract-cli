import dataclasses
import io
import logging
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

from wgsextract_cli.api._cli import cli_handler
from wgsextract_cli.api.vcf import VcfCnvParams, VcfSvParams
from wgsextract_cli.core.alignment_metadata import get_bam_header
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.output_checks import vcf_record_count, warn_if_no_variants
from wgsextract_cli.core.reference_resolver import ReferenceLibrary
from wgsextract_cli.core.results import current_result
from wgsextract_cli.core.sequencing import input_platform
from wgsextract_cli.core.ultima import read_groups
from wgsextract_cli.core.utils import (
    UsageError,
    WGSExtractError,
    popen,
    run_command,
)
from wgsextract_cli.core.variant_files import (
    calculate_bam_md5,
    ensure_vcf_indexed,
    verify_paths_exist,
)

from .basic import plain_reference, record_variants, setup_caller


def _cram_args(input_path: str, ref: str) -> list[str]:
    return ["-T", ref] if input_path.lower().endswith(".cram") else []


@contextmanager
def _region_input_bam(
    input_file: str, region: str | None, outdir: str, ref: str, failure_label: str
) -> Iterator[str]:
    """Yield input_file, or a temporary indexed BAM of region removed on exit."""
    if not region:
        yield input_file
        return

    with tempfile.TemporaryDirectory(dir=outdir, prefix=".wgse_region_") as tmp:
        temp_bam = str(Path(tmp, "region.bam"))
        logging.info(f"Extracting region {region} to temporary BAM...")
        try:
            _extract_region_bam_with_reference_header(input_file, region, ref, temp_bam)
            run_command(["samtools", "index", temp_bam], capture_output=True)
        except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
            raise WGSExtractError(
                f"{failure_label} region extraction failed: {e}"
            ) from e
        yield temp_bam


def _reference_contigs(ref: str) -> list[tuple[str, int]]:
    fai_path = ref + ".fai"
    if not Path(fai_path).is_file():
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
    input_file: str, region: str, ref: str, temp_bam: str
) -> None:
    """
    Write region of input_file as BAM whose @SQ lines are exactly the reference's contigs.

    Delly rejects BAMs listing contigs the reference lacks (e.g. an hs38DH CRAM
    called against hg38). Records are passed as SAM text so contigs are remapped
    by name; Python only writes the header and never touches the records.
    """
    cram_args = _cram_args(input_file, ref)
    header = run_command(
        ["samtools", "view", "-H", "--no-PG", *cram_args, input_file],
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
                    ["samtools", "view", *cram_args, input_file, region],
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


def _write_indexed_vcf_from_bcf(out_bcf: str, out_vcf: str) -> None:
    run_command(["bcftools", "view", "-Oz", "-o", out_vcf, out_bcf])
    ensure_vcf_indexed(out_vcf)
    for path in (out_bcf, out_bcf + ".csi"):
        if Path(path).exists():
            Path(path).unlink()


def _record_calls(out_vcf: str, role: str, ref: str, region: str | None) -> None:
    # A small region can have none; a whole genome can't.
    count = vcf_record_count(out_vcf) if region else warn_if_no_variants(out_vcf, ref)
    record_variants(out_vcf, role, count)


def run_cnv(params: VcfCnvParams) -> None:
    """``vcf cnv``: delly cnv into ``cnv.vcf.gz``."""
    verify_dependencies(["delly", "bcftools", "tabix", "samtools"])
    setup = setup_caller(params)
    outdir, ref = setup.outdir, setup.ref

    out_bcf = str(Path(outdir, "cnv.bcf"))
    out_vcf = str(Path(outdir, "cnv.vcf.gz"))
    logging.info(LOG_MESSAGES["vcf_calling_cnv"].format(output=out_vcf))

    if params.map:
        logging.warning(
            "Ignoring --map: Delly 2 estimates mappability from the reads and no "
            "longer takes a mappability map."
        )
    ploidy_args = ["-y", str(params.ploidy)] if params.ploidy is not None else []

    with _region_input_bam(
        str(params.input), params.region, outdir, ref, "CNV"
    ) as input_file:
        try:
            run_command(
                ["delly", "cnv", "-g", ref, "-o", out_bcf, *ploidy_args, input_file]
            )
        except subprocess.CalledProcessError as e:
            raise WGSExtractError(
                f"CNV calling failed with exit code {e.returncode}"
            ) from e
    _write_indexed_vcf_from_bcf(out_bcf, out_vcf)
    _record_calls(out_vcf, "copy_number_variants", ref, params.region)


cmd_cnv = cli_handler(VcfCnvParams, run_cnv)


def _tandem_repeats(params: VcfSvParams) -> str | None:
    return str(params.tandem_repeats) if params.tandem_repeats else None


def run_sv_pbsv(params: VcfSvParams) -> None:
    """``vcf sv`` with pbsv into ``pbsv.vcf.gz``."""
    verify_dependencies(["pbsv", "bcftools", "tabix", "samtools"])
    log_dependency_info(["pbsv", "bcftools", "tabix", "samtools"])
    setup = setup_caller(params)
    threads, outdir, ref = setup.threads, setup.outdir, setup.ref

    out_vcf = str(Path(outdir, "pbsv.vcf.gz"))
    region = params.region
    tandem_repeats = _tandem_repeats(params)
    if tandem_repeats:
        verify_paths_exist({"--tandem-repeats": tandem_repeats})

    logging.info(LOG_MESSAGES["vcf_calling_sv"].format(caller="pbsv", output=out_vcf))
    with (
        tempfile.TemporaryDirectory(dir=outdir, prefix=".wgse_pbsv_") as work,
        # pbsv call does not accept .fa.gz input.
        plain_reference(ref, outdir) as pbsv_ref,
    ):
        svsig = str(Path(work, "pbsv.svsig.gz"))
        raw_vcf = str(Path(work, "pbsv.vcf"))
        discover_cmd = ["pbsv", "discover"]
        if region:
            discover_cmd.extend(["--region", region])
        if tandem_repeats:
            discover_cmd.extend(["--tandem-repeats", tandem_repeats])
        if params.ccs:
            discover_cmd.append("--hifi")
        discover_cmd.extend([str(params.input), svsig])

        call_cmd = ["pbsv", "call", "-j", threads]
        if region:
            call_cmd.extend(["--region", region])
        if params.ccs:
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
    _record_calls(out_vcf, "structural_variants", ref, region)


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
        raise UsageError(f"Invalid region: {region}") from e
    if start < 1 or end < start:
        raise UsageError(f"Invalid region: {region}")
    with open(bed_path, "w", newline="\n") as f:
        f.write(f"{chrom}\t{start - 1}\t{end}\n")


def run_sv_sniffles(params: VcfSvParams) -> None:
    """``vcf sv`` with Sniffles into ``sniffles.vcf.gz``."""
    verify_dependencies(["sniffles", "bcftools", "tabix", "samtools"])
    log_dependency_info(["sniffles", "bcftools", "tabix", "samtools"])
    setup = setup_caller(params)
    threads, outdir, ref = setup.threads, setup.outdir, setup.ref

    out_vcf = str(Path(outdir, "sniffles.vcf.gz"))
    region = params.region
    tandem_repeats = _tandem_repeats(params)
    if tandem_repeats:
        verify_paths_exist({"--tandem-repeats": tandem_repeats})

    logging.info(
        LOG_MESSAGES["vcf_calling_sv"].format(caller="sniffles", output=out_vcf)
    )
    with tempfile.TemporaryDirectory(dir=outdir, prefix=".wgse_sniffles_") as work:
        raw_vcf = str(Path(work, "sniffles.vcf"))
        # --reference is needed to decode CRAM and to emit deletion sequences.
        input_file = str(params.input)
        cmd = ["sniffles", "--input", input_file, "--vcf", raw_vcf]
        cmd += ["--reference", ref, "--threads", threads]
        if tandem_repeats:
            cmd += ["--tandem-repeats", tandem_repeats]
        # Sniffles names the sample "SAMPLE" unless told; match the other callers.
        header = get_bam_header(input_file, ref)
        samples = {g["SM"] for g in read_groups(header) if g.get("SM")}
        if len(samples) == 1:
            cmd += ["--sample-id", samples.pop()]
        if region:
            bed_path = str(Path(work, "region.bed"))
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
    _record_calls(out_vcf, "structural_variants", ref, region)


def _detected_platform(params: VcfSvParams) -> str | None:
    """Platform of the input alignment, decoding CRAM with the resolved --ref."""
    input_file = str(params.input)
    if not Path(input_file).is_file():
        return None
    lib = ReferenceLibrary(
        str(params.ref) if params.ref else None,
        calculate_bam_md5(input_file, None),
        input_path=input_file,
    )
    return input_platform(input_file, lib.fasta)


def _select_sv_caller(params: VcfSvParams) -> VcfSvParams:
    """``params`` with the caller chosen (and ``ccs`` set for ``pacbio``)."""
    return dataclasses.replace(
        params, caller=_sv_caller(params), ccs=params.ccs or params.pacbio
    )


def _sv_caller(params: VcfSvParams) -> str:
    """Explicit flags win; only detected ONT changes the Delly default."""
    caller = params.caller
    pacbio, ont, long_read = params.pacbio, params.ont, params.long_read
    if pacbio and ont:
        raise UsageError("--pacbio and --ont cannot be combined.")
    if (pacbio or ont or long_read) and caller == "delly":
        flag = "--pacbio" if pacbio else "--ont" if ont else "--long-read"
        raise UsageError(
            f"{flag} selects a long-read SV caller (pbsv or sniffles) and cannot "
            "be combined with --caller delly."
        )
    pbsv_ont_error = WGSExtractError(
        "pbsv is PacBio-only and does not support Nanopore (ONT) reads; "
        "use --caller sniffles (the ONT default)."
    )
    if pacbio:
        caller = caller or ("pbsv" if get_tool_path("pbsv") is not None else "sniffles")
        if caller == "pbsv" and _detected_platform(params) == "ont":
            raise pbsv_ont_error
        return caller
    detected = None
    if not ont and (caller in ("pbsv", "delly") or (not caller and not long_read)):
        detected = _detected_platform(params)
    if caller == "pbsv" and (ont or detected == "ont"):
        raise pbsv_ont_error
    if ont or long_read:
        return caller or "sniffles"
    if caller:
        if caller == "delly" and detected == "ont":
            logging.warning(
                "Delly is a short-read SV caller and is not validated for ONT; "
                "Sniffles is the ONT default."
            )
        return caller
    if detected == "ont":
        logging.info("Detected ONT reads: calling structural variants with Sniffles.")
        return "sniffles"
    if detected == "pacbio":
        logging.info(
            "Detected PacBio reads; keeping the Delly default. Pass --pacbio for "
            "pbsv/Sniffles."
        )
    return "delly"


def run_sv(params: VcfSvParams) -> None:
    """``vcf sv``: delly, pbsv or Sniffles, as ``_select_sv_caller`` picks."""
    params = _select_sv_caller(params)
    current_result().add_metrics(caller=params.caller)
    if params.caller == "pbsv":
        run_sv_pbsv(params)
        return
    if params.caller == "sniffles":
        run_sv_sniffles(params)
        return

    verify_dependencies(["delly", "bcftools", "tabix", "samtools"])
    setup = setup_caller(params)
    threads, outdir, ref = setup.threads, setup.outdir, setup.ref

    out_bcf = str(Path(outdir, "sv.bcf"))
    out_vcf = str(Path(outdir, "sv.vcf.gz"))
    logging.info(LOG_MESSAGES["vcf_calling_sv"].format(caller="delly", output=out_vcf))

    with _region_input_bam(
        str(params.input), params.region, outdir, ref, "SV"
    ) as input_file:
        try:
            # Delly 2 short-read SV discovery; note `-h` is its thread count.
            run_command(
                ["delly", "sr", "-g", ref, "-h", str(threads), "-o", out_bcf]
                + [input_file]
            )
        except subprocess.CalledProcessError as e:
            raise WGSExtractError(
                f"SV calling failed with exit code {e.returncode}"
            ) from e
    _write_indexed_vcf_from_bcf(out_bcf, out_vcf)
    _record_calls(out_vcf, "structural_variants", ref, params.region)


cmd_sv = cli_handler(VcfSvParams, run_sv)
