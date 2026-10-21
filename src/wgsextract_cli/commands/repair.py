import argparse
import gzip
import io
import logging
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import IO, TextIO

from wgsextract_cli.core.dependency_checks import verify_dependencies
from wgsextract_cli.core.messages import CLI_HELP, LOG_MESSAGES
from wgsextract_cli.core.paths import absolute_path
from wgsextract_cli.core.reference_assets import input_stem
from wgsextract_cli.core.utils import WGSExtractError, popen, run_command
from wgsextract_cli.core.variant_files import calculate_bam_md5, resolve_reference

from ._extract_helpers import cleanup_files


def register(
    subparsers: argparse._SubParsersAction, base_parser: argparse.ArgumentParser
) -> None:
    parser = subparsers.add_parser(
        "repair", help="Repair formatting violations in FTDNA files."
    )
    repair_subs = parser.add_subparsers(dest="repair_cmd", required=True)

    bam_parser = repair_subs.add_parser(
        "ftdna-bam",
        parents=[base_parser],
        help=CLI_HELP["cmd_repair-ftdna-bam"],
    )
    bam_parser.add_argument(
        "--output",
        help="Output repaired BAM path. Defaults to <outdir>/<input>_repaired.bam.",
    )
    bam_parser.set_defaults(func=repair_bam)

    vcf_parser = repair_subs.add_parser(
        "ftdna-vcf",
        parents=[base_parser],
        help=CLI_HELP["cmd_repair-ftdna-vcf"],
    )
    vcf_parser.add_argument(
        "--output",
        help="Output repaired VCF path. Defaults to <outdir>/<input>_repaired.vcf.",
    )
    vcf_parser.set_defaults(func=repair_vcf)


def repair_bam(args: argparse.Namespace) -> None:
    if _input_was_explicit(args) and args.input:
        repair_bam_file(args)
        return
    # Pipe mode: samtools view -h in.bam | wgsextract repair ftdna-bam | samtools view -b
    logging.info(LOG_MESSAGES["repair_bam"])
    repair_bam_stream(sys.stdin, sys.stdout)


def repair_vcf(args: argparse.Namespace) -> None:
    if _input_was_explicit(args) and args.input:
        repair_vcf_file(args)
        return
    # Pipe mode: bcftools view in.vcf | wgsextract repair ftdna-vcf > out.vcf
    logging.info(LOG_MESSAGES["repair_vcf"])
    repair_vcf_stream(sys.stdin, sys.stdout)


def repair_bam_file(args: argparse.Namespace) -> None:
    """Stream samtools view -h | repair | samtools view -b without temp SAM files."""
    input_path = args.input
    if not Path(input_path).is_file():
        raise WGSExtractError(f"Input BAM/CRAM does not exist: {input_path}")
    verify_dependencies(["samtools"])
    output_path = _repair_output_path(args, ".bam")
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    cram_opt: list[str] = []
    if input_path.lower().endswith(".cram") and args.ref:
        ref = resolve_reference(args.ref, calculate_bam_md5(input_path, None))
        if not ref or not Path(ref).is_file():
            raise WGSExtractError(f"No reference FASTA for {input_path} at {args.ref}")
        cram_opt = ["-T", ref]

    with (
        cleanup_files(on_failure=[output_path]),
        tempfile.TemporaryFile() as read_err,
        tempfile.TemporaryFile() as write_err,
    ):
        reader = popen(
            ["samtools", "view", "-h", *cram_opt, input_path],
            stdout=subprocess.PIPE,
            stderr=read_err,
        )
        writer = popen(
            ["samtools", "view", "-b", "-o", output_path, "-"],
            stdin=subprocess.PIPE,
            stderr=write_err,
        )
        assert reader.stdout is not None and writer.stdin is not None
        try:
            try:
                with (
                    io.TextIOWrapper(
                        reader.stdout, encoding="utf-8", errors="replace"
                    ) as sam_in,
                    io.TextIOWrapper(writer.stdin, encoding="utf-8") as sam_out,
                ):
                    repair_bam_stream(sam_in, sam_out)
            except BrokenPipeError:
                pass  # The writer exited early; its exit status says why.
            read_code, write_code = reader.wait(), writer.wait()
        finally:
            for proc in (reader, writer):
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
        if write_code or read_code:
            # A failed writer also kills the reader with SIGPIPE; report the cause.
            label, code, err = (
                ("samtools view -b", write_code, write_err)
                if write_code
                else ("samtools view -h", read_code, read_err)
            )
            raise WGSExtractError(
                f"FTDNA BAM repair failed in {label} (exit {code}): {_tail(err)}"
            )
    logging.info("Repaired FTDNA BAM written to %s", output_path)


def repair_vcf_file(args: argparse.Namespace) -> None:
    input_path = args.input
    if not Path(input_path).is_file():
        raise WGSExtractError(f"Input VCF/BCF does not exist: {input_path}")
    output_path = _repair_output_path(args, ".vcf")
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    if input_path.lower().endswith(".bcf"):
        with tempfile.NamedTemporaryFile(
            mode="w+", suffix=".vcf", delete=False, encoding="utf-8"
        ) as tmp_vcf:
            tmp_path = tmp_vcf.name
        try:
            with open(tmp_path, "wb") as vcf_output:
                run_command(["bcftools", "view", input_path], stdout=vcf_output)
            with (
                open(tmp_path, encoding="utf-8", errors="replace") as vcf_input,
                open(output_path, "w", encoding="utf-8") as repaired_output,
            ):
                repair_vcf_stream(vcf_input, repaired_output)
        finally:
            try:
                Path(tmp_path).unlink()
            except OSError:
                pass
    else:
        with (
            _open_text_variant_input(input_path) as vcf_input,
            open(output_path, "w", encoding="utf-8") as repaired_output,
        ):
            repair_vcf_stream(vcf_input, repaired_output)
    logging.info("Repaired FTDNA VCF written to %s", output_path)


def repair_bam_stream(input_stream: TextIO, output_stream: TextIO) -> None:
    for line in input_stream:
        if line.startswith("@"):
            output_stream.write(line)
            continue
        fields = line.rstrip("\n").split("\t")
        if fields:
            fields[0] = fields[0].replace(" ", ":")
        output_stream.write("\t".join(fields) + "\n")


def repair_vcf_stream(input_stream: TextIO, output_stream: TextIO) -> None:
    for line in input_stream:
        if line.startswith("#"):
            output_stream.write(line)
            continue
        fields = line.rstrip("\n").split("\t")
        if len(fields) > 6:
            fields[6] = re.sub(r"[^A-Za-z0-9_.;-]", "", fields[6])
        output_stream.write("\t".join(fields) + "\n")


def _repair_output_path(args: argparse.Namespace, extension: str) -> str:
    if args.output:
        return str(absolute_path(str(args.output)))
    outdir = args.outdir or str(absolute_path(args.input).parent)
    return str(
        absolute_path(Path(outdir, f"{input_stem(args.input)}_repaired{extension}"))
    )


def _input_was_explicit(args: argparse.Namespace) -> bool:
    explicit_dests = getattr(args, "_explicit_dests", None)
    if explicit_dests is None:
        return bool(getattr(args, "input", None))
    return "input" in explicit_dests


def _tail(stream: IO[bytes], max_chars: int = 2000) -> str:
    stream.seek(0)
    return stream.read().decode(errors="replace")[-max_chars:].strip()


def _open_text_variant_input(path: str) -> TextIO:
    if path.lower().endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return open(path, encoding="utf-8", errors="replace")
