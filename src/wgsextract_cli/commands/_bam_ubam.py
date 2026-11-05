"""``bam to-ubam``: build a queryname-sorted unaligned BAM for GATK pipelines."""

import argparse
import gzip
import logging
import re
import subprocess
import tempfile
from os import fspath
from pathlib import Path

from wgsextract_cli.api._cli import cli_handler
from wgsextract_cli.api.bam import BamToUbamParams
from wgsextract_cli.core import ultima
from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.paths import PathInput, absolute_path
from wgsextract_cli.core.results import current_result
from wgsextract_cli.core.utils import (
    UsageError,
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

# Tags that describe the raw read rather than an alignment (GATK RevertSam keeps
# these too), and Ultima's (see core.ultima); everything else (AS, MD, NM, XS,
# SA, ...) is dropped.
KEEP_TAGS = ",".join(
    ["RG", "BC", "QT", "RX", "QX", "OQ", "BI", "BD", "MI", *ultima.READ_TAGS]
)

# Casava 1.8+: instrument:run:flowcell:lane:tile:x:y; earlier Illumina names
# are instrument:lane:tile:x:y and carry no flowcell.
_ILLUMINA_NAME = re.compile(
    r"^[^:\s]+:\d+:(?P<flowcell>[^:\s]+):(?P<lane>\d+):\d+:\d+:\d+(?=$|[\s#/])"
)
_OLD_ILLUMINA_NAME = re.compile(
    r"^(?P<flowcell>[^:\s]+):(?P<lane>\d+):\d+:\d+:\d+(?=$|[\s#/])"
)
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
    elif sequencer.startswith("Ultima"):
        platform = ultima.PLATFORM

    match = (
        _ILLUMINA_NAME.match(qname)
        or _OLD_ILLUMINA_NAME.match(qname)
        or _MGI_NAME.search(qname)
    )
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


def _input_stem(path: PathInput) -> str:
    name = Path(path).name
    for ext in (".gz", ".bgz", ".fastq", ".fq", ".bam", ".cram", ".sam"):
        name = name[: -len(ext)] if name.lower().endswith(ext) else name
    # Read-1 markers: _R1, .1, _read1 and bcl2fastq's _R1_001.
    pattern = r"[._-](R?1|read1)(_001)?$"
    return re.sub(pattern, "", name, flags=re.IGNORECASE) or "sample"


def _new_read_group(params: BamToUbamParams, qname: str | None, stem: str) -> list[str]:
    """``@RG`` fields (``KEY:value``) for input that has no read group."""
    unit, platform = read_group_from_name(qname) if qname else (None, None)
    platform = params.platform or platform
    sample = params.sample or stem
    fields = [f"ID:{unit or sample}", f"SM:{sample}", f"LB:{params.library}"]
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


def _sort_cmd(
    out_file: PathInput, threads: str, memory: str, temp_dir: PathInput
) -> list[str]:
    # -N is Picard/GATK queryname order (strcmp), unlike -n's natural order.
    tmp_prefix = str(Path(temp_dir, "sort"))
    cmd = ["samtools", "sort", "-N", "-@", threads, "-m", memory]
    return cmd + ["-T", tmp_prefix, "-o", fspath(out_file), "-"]


def _bam_stages(
    params: BamToUbamParams, input_path: str, threads: str
) -> list[list[str]]:
    header = get_bam_header(input_path)
    if not header:
        raise WGSExtractError(f"Could not read the header of {input_path}.")
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
    if input_path.lower().endswith(".cram"):
        ref = resolve_reference(
            fspath(params.ref) if params.ref else None,
            calculate_bam_md5(input_path, header=header),
            input_path,
        )
        if not (ref and Path(ref).is_file()):
            raise UsageError("--ref is required to read a CRAM file.")
        ref_opt = ["--reference", ref]
    if ultima.is_ultima_header(header):
        # reset turns reverse-strand reads around but not their flow tags.
        view = ["samtools", "view", "-h", "--no-PG", *ref_opt, input_path]
        stages = [view, ultima.flow_tag_filter()]
        stages.append(reset + ref_opt + ["-O", "BAM,level=0", "-o", "-", "-"])
    else:
        stages = [reset + ref_opt + ["-O", "BAM,level=0", "-o", "-", input_path]]

    if any(line.startswith("@RG") for line in header.splitlines()):
        if params.sample or params.platform:
            logging.warning(
                "Input already has read groups; --sample/--platform are ignored."
            )
        return stages

    first = run_command(
        ["samtools", "head", "-h", "0", "-n", "1", *ref_opt, input_path],
        capture_output=True,
        check=False,
    )
    qname = first.stdout.split("\t", 1)[0] if first.stdout else None
    rg = _new_read_group(params, qname, _input_stem(input_path))
    logging.info(f"Input has no @RG line; adding @RG {' '.join(rg)}")
    stages.append(
        ["samtools", "addreplacerg", "-m", "overwrite_all", "-O", "BAM,level=0"]
        + [arg for field in rg for arg in ("-r", field)]
        + ["-o", "-", "-"]
    )
    return stages


def _fastq_stages(
    params: BamToUbamParams, r1: str, r2: str | None, threads: str
) -> list[list[str]]:
    rg = _new_read_group(params, _first_fastq_name(r1), _input_stem(r1))
    logging.info(f"Read group: @RG {' '.join(rg)}")
    cmd = ["samtools", "import", "-i", "-@", threads, "-O", "BAM,level=0"]
    cmd += [arg for field in rg for arg in ("-r", field)]
    cmd += ["-1", r1, "-2", r2] if r2 else ["-0", r1]
    stages = [cmd + ["-o", "-"]]
    if not r2:
        # samtools import reads a /1 name suffix as read 1 of a pair, even
        # from -0; single-end reads have no mate.
        unpair = ["--remove-flags", "PAIRED,MUNMAP,READ1,READ2"]
        stages.append(
            ["samtools", "view", *unpair, "-O", "BAM,level=0", "-o", "-", "-"]
        )
    return stages


def run_to_ubam(params: BamToUbamParams) -> None:
    input_path = fspath(params.input) if params.input else None
    r1 = fspath(params.r1) if params.r1 else None
    r2 = fspath(params.r2) if params.r2 else None
    source = input_path or r1
    assert source  # BamToUbamParams requires one
    verify_paths_exist({"input": source, **({"--r2": r2} if r2 else {})})
    result = current_result()
    if input_path:
        result.add_input(input_path, "input")
    else:
        result.add_input(r1, "fastq_r1")
        result.add_input(r2, "fastq_r2")
    if input_path and not input_path.lower().endswith((".bam", ".cram")):
        raise WGSExtractError("--input must be a BAM or CRAM file.")
    verify_dependencies(["samtools"])
    log_dependency_info(["samtools"])

    threads, memory = get_resource_defaults(params.threads, params.memory)
    outdir = Path(params.outdir) if params.outdir else absolute_path(source).parent
    outdir.mkdir(parents=True, exist_ok=True)
    out_file = outdir / f"{_input_stem(source)}_unaligned.bam"
    if out_file.resolve() == Path(source).resolve():
        raise WGSExtractError(f"Output {out_file} would overwrite the input.")

    size = sum(Path(p).stat().st_size for p in (source, r2) if p)
    temp_gb, final_gb = get_free_space_needed(
        size,
        "Name",
        is_cram=source.lower().endswith(".cram"),
        reads=indexed_read_count(source, threads) if input_path else None,
    )
    if not check_free_space(outdir, temp_gb + final_gb):
        raise WGSExtractError(
            f"Insufficient free disk space at {outdir} "
            f"(needs ~{temp_gb + final_gb} GB)."
        )

    with tempfile.TemporaryDirectory(prefix=".wgse_ubam_", dir=outdir) as temp_dir:
        stages = (
            _bam_stages(params, input_path, threads)
            if input_path
            else _fastq_stages(params, source, r2, threads)
        )
        stages.append(_sort_cmd(out_file, threads, memory, temp_dir))
        logging.info(f"Writing unaligned BAM {out_file}...")
        try:
            with cleanup_files(on_failure=[out_file]):
                run_pipeline(stages, description="Unaligned BAM creation")
        except (OSError, subprocess.SubprocessError) as e:
            raise WGSExtractError(f"Unaligned BAM creation failed: {e}") from e
    current_result().add_output(out_file, "unaligned_bam")
    logging.info(f"Unaligned BAM written to {out_file}")


_cli_to_ubam = cli_handler(BamToUbamParams, run_to_ubam)


def cmd_to_ubam(args: argparse.Namespace) -> None:
    explicit = getattr(args, "_explicit_dests", None)
    if args.r1 and explicit is not None and "input" not in explicit:
        args.input = None  # a configured default input yields to --r1
    _cli_to_ubam(args)
