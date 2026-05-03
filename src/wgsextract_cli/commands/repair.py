import logging
import os
import re
import sys

from wgsextract_cli.core.messages import CLI_HELP, LOG_MESSAGES
from wgsextract_cli.core.utils import WGSExtractError, run_command


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
    bam_parser.set_defaults(func=repair_bam)

    vcf_parser = repair_subs.add_parser(
        "ftdna-vcf",
        parents=[base_parser],
        help=CLI_HELP["cmd_repair-ftdna-vcf"],
    )
    vcf_parser.set_defaults(func=repair_vcf)


def get_script_path(script_name):
    prog_dir = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "../../../../program")
    )
    return os.path.join(prog_dir, script_name)


def repair_bam(args):
    script = get_script_path("fixFTDNAbam.py")
    logging.info(LOG_MESSAGES["repair_bam"])
    if not os.path.exists(script):
        for line in sys.stdin:
            if line.startswith("@"):
                sys.stdout.write(line)
                continue
            fields = line.rstrip("\n").split("\t")
            if fields:
                fields[0] = fields[0].replace(" ", ":")
            sys.stdout.write("\t".join(fields) + "\n")
        return

    try:
        # Note: This is designed to be part of a pipe: samtools view -h in.bam | wgsextract-cli repair ftdna-bam | samtools view -b > out.bam
        run_command([sys.executable, script])
    except Exception as e:
        raise WGSExtractError(f"Repair failed: {e}") from e


def repair_vcf(args):
    script = get_script_path("fixFTDNAvcf.py")
    logging.info(LOG_MESSAGES["repair_vcf"])
    if not os.path.exists(script):
        for line in sys.stdin:
            if line.startswith("#"):
                sys.stdout.write(line)
                continue
            fields = line.rstrip("\n").split("\t")
            if len(fields) > 6:
                fields[6] = re.sub(r"[^A-Za-z0-9_.;-]", "", fields[6])
            sys.stdout.write("\t".join(fields) + "\n")
        return

    try:
        # Note: Designed to be part of a pipe: bcftools view in.vcf | wgsextract-cli repair ftdna-vcf > out.vcf
        run_command([sys.executable, script])
    except Exception as e:
        raise WGSExtractError(f"Repair failed: {e}") from e
