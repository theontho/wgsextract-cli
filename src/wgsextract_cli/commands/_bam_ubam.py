"""``bam to-ubam``: build a queryname-sorted unaligned BAM for GATK pipelines."""

import argparse
import gzip
import logging
import os
import re
import subprocess
import tempfile

from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.utils import (
    WGSExtractError,
    get_resource_defaults,
    run_command,
    run_pipeline,
)
from wgsextract_cli.core.variant_files import (
    calculate_bam_md5,
    get_bam_header,
    resolve_reference,
    verify_paths_exist,
)
from wgsextract_cli.core.warnings import (
    check_free_space,
    get_free_space_needed,
    indexed_read_count,
)

from ._extract_helpers import cleanup_files
from .info.metrics import determine_sequencer

# SAM-spec @RG PL values.
UBAM_PLATFORMS = (
    "ILLUMINA",
    "DNBSEQ",
    "ELEMENT",
    "ULTIMA",
    "PACBIO",
    "ONT",
    "IONTORRENT",
    "LS454",
    "SOLID",
    "CAPILLARY",
)

# Tags that describe the raw read rather than an alignment (GATK RevertSam keeps
# these too); everything else (AS, MD, NM, XS, SA, ...) is dropped.
KEEP_TAGS = "RG,BC,QT,RX,QX,OQ,BI,BD,MI"

_ILLUMINA_NAME = re.compile(r"^[^:\s]+:\d+:(?P<flowcell>[^:\s]+):(?P<lane>\d+):")
_MGI_NAME = re.compile(r"(?:^|_)(?P<flowcell>[A-Z0-9]+?)L(?P<lane>\d)C\d{3}R\d{3}")


def read_group_from_name(qname: str) -> tuple[str | None, str | None]:
    """(platform unit "flowcell.lane", SAM PL) parsed from a read name."""
    sequencer = determine_sequencer(qname)
    platform = None
    if sequencer.startswith(("Illumina", "Solexa")):
        platform = "ILLUMINA"
    elif sequencer.startswith("MGI"):
        platform = "DNBSEQ"
    elif sequencer.startswith("PacBio"):
        platform = "PACBIO"
    elif sequencer.startswith("Oxford"):
        platform = "ONT"

    match = _ILLUMINA_NAME.match(qname) or _MGI_NAME.search(qname)
    unit = f"{match['flowcell']}.{match['lane']}" if match else None
    return unit, platform


def _first_fastq_name(path: str) -> str:
    opener = gzip.open if path.lower().endswith((".gz", ".bgz")) else open
    try:
        with opener(path, "rt") as handle:
            line = handle.readline()
    except (OSError, UnicodeDecodeError) as e:
        raise WGSExtractError(f"Could not read FASTQ {path}: {e}") from e
    if not line.startswith("@"):
        raise WGSExtractError(f"{path} does not look like a FASTQ file.")
    return line[1:].split()[0].removesuffix("/1").removesuffix("/2")


def _input_stem(path: str) -> str:
    name = os.path.basename(path)
    for ext in (".gz", ".bgz", ".fastq", ".fq", ".bam", ".cram", ".sam"):
        name = name.removesuffix(ext) if name.lower().endswith(ext) else name
    return re.sub(r"[._-](R?1|read1)$", "", name, flags=re.IGNORECASE) or "sample"


def _new_read_group(
    args: argparse.Namespace, qname: str | None, stem: str
) -> list[str]:
    """``@RG`` fields (``KEY:value``) for input that has no read group."""
    unit, platform = read_group_from_name(qname) if qname else (None, None)
    platform = args.platform or platform
    sample = args.sample or stem
    fields = [f"ID:{unit or sample}", f"SM:{sample}", f"LB:{args.library}"]
    if unit:
        fields.append(f"PU:{unit}")
    if platform:
        fields.append(f"PL:{platform}")
    else:
        logging.warning(
            "Could not detect the sequencing platform from the read names; "
            "pass --platform so GATK tools can use the read group."
        )
    return fields


def _sort_cmd(out_file: str, threads: str, memory: str, temp_dir: str) -> list[str]:
    # -N is Picard/GATK queryname order (strcmp), unlike -n's natural order.
    tmp_prefix = os.path.join(temp_dir, "sort")
    cmd = ["samtools", "sort", "-N", "-@", threads, "-m", memory]
    return cmd + ["-T", tmp_prefix, "-o", out_file, "-"]


def _bam_stages(
    args: argparse.Namespace, threads: str, temp_dir: str
) -> list[list[str]]:
    header = get_bam_header(args.input)
    if not header:
        raise WGSExtractError(f"Could not read the header of {args.input}.")
    reset = ["samtools", "reset", "-@", threads, "--keep-tag", KEEP_TAGS]
    pg_ids = [
        field[3:]
        for line in header.splitlines()
        if line.startswith("@PG")
        for field in line.split("\t")
        if field.startswith("ID:")
    ]
    if pg_ids:
        # Drops the aligner's @PG chain: the reads are no longer its output.
        reset += ["--reject-PG", pg_ids[0]]
    ref_opt: list[str] = []
    if args.input.lower().endswith(".cram"):
        ref = resolve_reference(
            args.ref, calculate_bam_md5(args.input, header=header), args.input
        )
        if not (ref and os.path.isfile(ref)):
            raise WGSExtractError("--ref is required to read a CRAM file.")
        ref_opt = ["--reference", ref]
    stages = [reset + ref_opt + ["-O", "BAM,level=0", "-o", "-", args.input]]

    if any(line.startswith("@RG") for line in header.splitlines()):
        if args.sample or args.platform:
            logging.warning(
                "Input already has read groups; --sample/--platform are ignored."
            )
        return stages

    first = run_command(
        ["samtools", "head", "-h", "0", "-n", "1", *ref_opt, args.input],
        capture_output=True,
        check=False,
    )
    qname = first.stdout.split("\t", 1)[0] if first.stdout else None
    rg = _new_read_group(args, qname, _input_stem(args.input))
    logging.info(f"Input has no @RG line; adding @RG {' '.join(rg)}")
    stages.append(
        ["samtools", "addreplacerg", "-m", "overwrite_all", "-O", "BAM,level=0"]
        + [arg for field in rg for arg in ("-r", field)]
        + ["-o", "-", "-"]
    )
    return stages


def _fastq_stages(args: argparse.Namespace, threads: str) -> list[list[str]]:
    rg = _new_read_group(args, _first_fastq_name(args.r1), _input_stem(args.r1))
    logging.info(f"Read group: @RG {' '.join(rg)}")
    cmd = ["samtools", "import", "-i", "-@", threads, "-O", "BAM,level=0"]
    cmd += [arg for field in rg for arg in ("-r", field)]
    cmd += ["-1", args.r1, "-2", args.r2] if args.r2 else ["-0", args.r1]
    return [cmd + ["-o", "-"]]


def cmd_to_ubam(args: argparse.Namespace) -> None:
    explicit = getattr(args, "_explicit_dests", None)
    if args.r1 and explicit is not None and "input" not in explicit:
        args.input = None  # a configured default input yields to --r1
    if bool(args.input) == bool(args.r1):
        raise WGSExtractError(
            "Pass either --input (aligned BAM/CRAM) or --r1 [--r2] (FASTQ)."
        )
    if args.r2 and not args.r1:
        raise WGSExtractError("--r2 needs --r1.")
    source = args.input or args.r1
    verify_paths_exist({"input": source, **({"--r2": args.r2} if args.r2 else {})})
    if args.input and not args.input.lower().endswith((".bam", ".cram")):
        raise WGSExtractError("--input must be a BAM or CRAM file.")
    verify_dependencies(["samtools"])
    log_dependency_info(["samtools"])

    threads, memory = get_resource_defaults(args.threads, args.memory)
    outdir = args.outdir or os.path.dirname(os.path.abspath(source))
    os.makedirs(outdir, exist_ok=True)
    out_file = os.path.join(outdir, f"{_input_stem(source)}_unaligned.bam")
    if os.path.realpath(out_file) == os.path.realpath(source):
        raise WGSExtractError(f"Output {out_file} would overwrite the input.")

    size = sum(os.path.getsize(p) for p in (source, args.r2) if p)
    temp_gb, final_gb = get_free_space_needed(
        size,
        "Name",
        is_cram=source.lower().endswith(".cram"),
        reads=indexed_read_count(source, threads) if args.input else None,
    )
    if not check_free_space(outdir, temp_gb + final_gb):
        raise WGSExtractError(
            f"Insufficient free disk space at {outdir} "
            f"(needs ~{temp_gb + final_gb} GB)."
        )

    with tempfile.TemporaryDirectory(prefix=".wgse_ubam_", dir=outdir) as temp_dir:
        stages = (
            _bam_stages(args, threads, temp_dir)
            if args.input
            else _fastq_stages(args, threads)
        )
        stages.append(_sort_cmd(out_file, threads, memory, temp_dir))
        logging.info(f"Writing unaligned BAM {out_file}...")
        try:
            with cleanup_files(on_failure=[out_file]):
                run_pipeline(stages, description="Unaligned BAM creation")
        except (OSError, subprocess.SubprocessError) as e:
            raise WGSExtractError(f"Unaligned BAM creation failed: {e}") from e
    logging.info(f"Unaligned BAM written to {out_file}")
