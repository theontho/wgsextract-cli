import logging
import os

from wgsextract_cli.core.builds import (
    BUILD_CHOICES,
    fake_data_library_code,
)
from wgsextract_cli.core.config import settings
from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import CLI_HELP

from ._qc_commands import (
    cmd_fastp,
    cmd_fastqc,
    cmd_vcf_qc,
)
from ._qc_fake_data import (
    generate_fake_genomics_data,
)


def cmd_fake_data(args):
    verify_dependencies(["samtools", "bcftools", "bgzip", "tabix"])
    log_dependency_info(["samtools", "bcftools"])

    outdir = args.outdir if args.outdir else os.getcwd()
    os.makedirs(outdir, exist_ok=True)

    from wgsextract_cli.core.ref_library import get_available_genomes
    from wgsextract_cli.core.variant_files import resolve_reference

    # Try to find reference in library if a known build is specified
    lib_ref = None
    target_md5 = None

    target_code = fake_data_library_code(args.build)
    if target_code:
        all_genomes = get_available_genomes()
        genome_info = next(
            (
                g
                for g in all_genomes
                if g["code"] == target_code or g["final"].startswith(target_code)
            ),
            None,
        )
        if genome_info:
            target_md5 = genome_info.get("md5") if genome_info.get("md5") else None
            if target_md5:
                logging.debug(f"Found target MD5 for {args.build}: {target_md5}")
            # See if it's installed
            from wgsextract_cli.core.config import settings

            reflib_dir = settings.get("reference_library")
            if reflib_dir:
                candidate = os.path.join(reflib_dir, "genomes", genome_info["final"])
                if os.path.exists(candidate):
                    lib_ref = candidate

    ref_path = resolve_reference(args.ref, None) if args.ref else lib_ref

    # If the resolved path is still a directory, it means it didn't find a fasta file there.
    if ref_path and os.path.isdir(ref_path):
        ref_path = None

    # Parse types
    types = [t.strip().lower() for t in args.type.split(",")]
    if "all" in types:
        types = ["vcf", "cram", "bam", "fastq"]

    generate_fake_genomics_data(
        outdir,
        ref_path,
        coverage=args.coverage,
        seed=args.seed,
        build=args.build,
        full_size=args.full_size,
        types=types,
        target_md5=target_md5,
        legacy_bam=args.legacy_bam,
    )


def register(subparsers, base_parser):
    parser = subparsers.add_parser(
        "qc", help="Runs quality control or calculates coverage."
    )
    qc_subs = parser.add_subparsers(dest="qc_cmd", required=True)

    fastp_parser = qc_subs.add_parser(
        "fastp", parents=[base_parser], help=CLI_HELP["cmd_fastp"]
    )
    fastp_parser.add_argument("--r1", help=CLI_HELP["arg_r1"])
    fastp_parser.add_argument("--r2", help=CLI_HELP["arg_r2"])
    fastp_parser.set_defaults(func=cmd_fastp)

    fastqc_parser = qc_subs.add_parser(
        "fastqc", parents=[base_parser], help=CLI_HELP["cmd_fastqc"]
    )
    fastqc_parser.set_defaults(func=cmd_fastqc)

    vcf_parser = qc_subs.add_parser(
        "vcf", parents=[base_parser], help=CLI_HELP["cmd_vcf-qc"]
    )
    vcf_parser.add_argument(
        "--vcf-input",
        default=settings.get("default_input_vcf"),
        help=CLI_HELP["arg_vcf_input"],
    )
    vcf_parser.set_defaults(func=cmd_vcf_qc)

    fake_parser = qc_subs.add_parser(
        "fake-data", parents=[base_parser], help=CLI_HELP["cmd_fake-data"]
    )
    fake_parser.add_argument(
        "--coverage", type=float, default=1.0, help="Coverage depth (e.g. 30.0)"
    )
    fake_parser.add_argument(
        "--build",
        choices=BUILD_CHOICES,
        default="hg38",
        help="Human genome build naming convention.",
    )
    fake_parser.add_argument(
        "--type",
        default="cram",
        help="Comma-separated list of types to generate (vcf, cram, bam, fastq, all). Default: cram",
    )
    fake_parser.add_argument(
        "--full-size",
        action="store_true",
        help="Use real human chromosome lengths. The default scaled mode uses shorter chromosomes.",
    )
    fake_parser.add_argument(
        "--legacy-bam",
        action="store_true",
        help=(
            "Use the older scaled fake BAM generator. Slower and unavailable with --full-size, "
            "but includes randomized placement and indel CIGARs."
        ),
    )
    fake_parser.add_argument(
        "--seed", type=int, default=42, help="Random seed for reproducibility"
    )
    fake_parser.set_defaults(func=cmd_fake_data)
