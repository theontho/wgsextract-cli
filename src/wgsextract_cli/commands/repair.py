import gzip
import logging
import os
import re
import sys
import tempfile

from wgsextract_cli.core.messages import CLI_HELP, LOG_MESSAGES
from wgsextract_cli.core.utils import (
    WGSExtractError,
    run_command,
)


def register(subparsers, base_parser):
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


def get_script_path(script_name):
    prog_dir = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "../../../../program")
    )
    return os.path.join(prog_dir, script_name)


def repair_bam(args):
    if _input_was_explicit(args) and getattr(args, "input", None):
        repair_bam_file(args)
        return

    script = get_script_path("fixFTDNAbam.py")
    logging.info(LOG_MESSAGES["repair_bam"])
    if not os.path.exists(script):
        repair_bam_stream(sys.stdin, sys.stdout)
        return

    try:
        # Note: This is designed to be part of a pipe: samtools view -h in.bam | wgsextract-cli repair ftdna-bam | samtools view -b > out.bam
        run_command([sys.executable, script])
    except Exception as e:
        raise WGSExtractError(f"Repair failed: {e}") from e


def repair_vcf(args):
    if _input_was_explicit(args) and getattr(args, "input", None):
        repair_vcf_file(args)
        return

    script = get_script_path("fixFTDNAvcf.py")
    logging.info(LOG_MESSAGES["repair_vcf"])
    if not os.path.exists(script):
        repair_vcf_stream(sys.stdin, sys.stdout)
        return

    try:
        # Note: Designed to be part of a pipe: bcftools view in.vcf | wgsextract-cli repair ftdna-vcf > out.vcf
        run_command([sys.executable, script])
    except Exception as e:
        raise WGSExtractError(f"Repair failed: {e}") from e


def repair_bam_file(args) -> None:
    input_path = args.input
    if not os.path.isfile(input_path):
        raise WGSExtractError(f"Input BAM/CRAM does not exist: {input_path}")
    output_path = _repair_output_path(args, ".bam")
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    stem = _input_stem(input_path)
    with tempfile.TemporaryDirectory(prefix=f"{stem}.repair.") as tmp_dir:
        tmp_sam = os.path.join(tmp_dir, f"{stem}.sam")
        tmp_repaired_sam = os.path.join(tmp_dir, f"{stem}.repaired.sam")
        with open(tmp_sam, "wb") as sam_output:
            run_command(["samtools", "view", "-h", input_path], stdout=sam_output)
        with (
            open(tmp_sam, encoding="utf-8", errors="replace") as sam_input,
            open(tmp_repaired_sam, "w", encoding="utf-8") as repaired_output,
        ):
            repair_bam_stream(sam_input, repaired_output)
        with open(tmp_repaired_sam, encoding="utf-8") as repaired_input:
            run_command(
                ["samtools", "view", "-b", "-o", output_path, "-"],
                stdin=repaired_input,
            )
    logging.info("Repaired FTDNA BAM written to %s", output_path)


def repair_vcf_file(args) -> None:
    input_path = args.input
    if not os.path.isfile(input_path):
        raise WGSExtractError(f"Input VCF/BCF does not exist: {input_path}")
    output_path = _repair_output_path(args, ".vcf")
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
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
                os.remove(tmp_path)
            except OSError:
                pass
    else:
        with (
            _open_text_variant_input(input_path) as vcf_input,
            open(output_path, "w", encoding="utf-8") as repaired_output,
        ):
            repair_vcf_stream(vcf_input, repaired_output)
    logging.info("Repaired FTDNA VCF written to %s", output_path)


def repair_bam_stream(input_stream, output_stream) -> None:
    for line in input_stream:
        if line.startswith("@"):
            output_stream.write(line)
            continue
        fields = line.rstrip("\n").split("\t")
        if fields:
            fields[0] = fields[0].replace(" ", ":")
        output_stream.write("\t".join(fields) + "\n")


def repair_vcf_stream(input_stream, output_stream) -> None:
    for line in input_stream:
        if line.startswith("#"):
            output_stream.write(line)
            continue
        fields = line.rstrip("\n").split("\t")
        if len(fields) > 6:
            fields[6] = re.sub(r"[^A-Za-z0-9_.;-]", "", fields[6])
        output_stream.write("\t".join(fields) + "\n")


def _repair_output_path(args, extension: str) -> str:
    output = getattr(args, "output", None)
    if output:
        return os.path.abspath(str(output))
    outdir = getattr(args, "outdir", None) or os.path.dirname(
        os.path.abspath(args.input)
    )
    return os.path.abspath(
        os.path.join(outdir, f"{_input_stem(args.input)}_repaired{extension}")
    )


def _input_was_explicit(args) -> bool:
    explicit_dests = getattr(args, "_explicit_dests", None)
    if explicit_dests is None:
        return bool(getattr(args, "input", None))
    return "input" in explicit_dests


def _input_stem(path: str) -> str:
    name = os.path.basename(path)
    for extension in (".vcf.gz", ".bam", ".cram", ".vcf", ".bcf"):
        if name.lower().endswith(extension):
            return name[: -len(extension)]
    return os.path.splitext(name)[0]


def _open_text_variant_input(path: str):
    if path.lower().endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return open(path, encoding="utf-8", errors="replace")
