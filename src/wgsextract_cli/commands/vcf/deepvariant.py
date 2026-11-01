import argparse
import gzip
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from wgsextract_cli.core import ultima
from wgsextract_cli.core.config import settings
from wgsextract_cli.core.containers import (
    docker_info,
    host_path,
    require_image,
    run_container,
)
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.dependency_checks import verify_dependencies
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.output_checks import warn_if_no_variants
from wgsextract_cli.core.reference_assets import read_fai_lengths
from wgsextract_cli.core.samtools_commands import get_sam_index_cmd, get_sam_view_cmd
from wgsextract_cli.core.utils import WGSExtractError, run_command
from wgsextract_cli.core.variant_files import ensure_vcf_indexed
from wgsextract_cli.core.warnings import GB, check_free_space, get_free_space_needed

from .basic import get_base_args

# Google's image; Bioconda's deepvariant has shipped no DeepVariant since 1.8.
DEFAULT_IMAGE = "google/deepvariant:1.10.0"
# Google's experimental image with the ULTIMA model (google/deepvariant#1100),
# until a release has it.
DEFAULT_ULTIMA_IMAGE = "google/deepvariant:gh1100-ultima"
_SETUP = (
    "DeepVariant runs from Google's Docker image on Linux and macOS (Apple "
    "Silicon emulates it, slowly): install Docker, then docker pull {image}. "
    "Or put Google's run_deepvariant on PATH."
)


def deepvariant_image(model_type: str = "WGS") -> str:
    if model_type == "ULTIMA":
        return str(settings.get("deepvariant_ultima_image") or DEFAULT_ULTIMA_IMAGE)
    return str(settings.get("deepvariant_image") or DEFAULT_IMAGE)


def _model_type(args: argparse.Namespace, ref: str | None = None) -> str:
    """--pacbio, --model-type or --wes, else ULTIMA for Ultima reads with
    their flow tags, else WGS."""
    if getattr(args, "pacbio", False):
        return "PACBIO"
    if getattr(args, "model_type", None):
        return str(args.model_type)
    if getattr(args, "wes", False):
        return "WES"
    if ref and ultima.alignment_flow_data(args.input, ref).flow_aware:
        logging.info("Ultima reads: calling with DeepVariant's ULTIMA model.")
        return "ULTIMA"
    return "WGS"


def _verify_docker(model_type: str = "WGS") -> None:
    """Docker runs here (Windows can't run DeepVariant's Linux image)."""
    setup = _SETUP.format(image=deepvariant_image(model_type))
    if sys.platform == "win32":
        raise WGSExtractError(f"DeepVariant isn't available on Windows. {setup}")
    docker_info(setup)


def _verify_docker_image(model_type: str) -> str:
    image = deepvariant_image(model_type)
    setting = (
        "deepvariant_ultima_image" if model_type == "ULTIMA" else "deepvariant_image"
    )
    require_image(
        image,
        f"The DeepVariant image isn't downloaded: docker pull {image} "
        f"(set {setting} in config.toml for another version).",
    )
    return image


def _dv_env() -> dict[str, str]:
    # DeepVariant runs its own Python; don't leak ours into it.
    env = dict(os.environ)
    for var in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
        env.pop(var, None)
    return env


def _cram_version(path: str) -> tuple[int, int] | None:
    """Read the CRAM file definition, independently of the filename."""
    with open(path, "rb") as source:
        definition = source.read(6)
    if len(definition) != 6 or definition[:4] != b"CRAM":
        return None
    return definition[4], definition[5]


def _literal_region(selection: str, lengths: dict[str, int]) -> tuple[str, int, int]:
    if selection in lengths:
        return selection, 0, lengths[selection]
    match = re.match(r"^(\S+):([0-9,]+)-([0-9,]+)$", selection)
    if match:
        chrom, start_text, end_text = match.groups()
        start, end = int(start_text.replace(",", "")), int(end_text.replace(",", ""))
    else:
        match = re.match(r"^(\S+):([0-9,]+)$", selection)
        if not match:
            raise ValueError(f"Invalid interval: {selection}")
        chrom, position = match.groups()
        start = end = int(position.replace(",", ""))
    if start < 1 or end < start:
        raise ValueError(f"Invalid interval: {selection}")
    return chrom, start - 1, end


def _transcode_regions(
    region: str | None, ref: str, work: str
) -> tuple[list[str], float]:
    if not region:
        return [], 1.0
    lengths = read_fai_lengths(ref)
    if not lengths:
        run_command(["samtools", "faidx", ref])
        lengths = read_fai_lengths(ref)
    bed = Path(work, "regions.bed")
    # DV accepts whitespace-separated literals and BED/BED.gz/BEDPE files.
    # One BED + the multi-region iterator preserves their union without duplicates.
    try:
        with bed.open("w", newline="\n") as destination:
            for selection in region.split():
                lower = selection.lower()
                if lower.endswith((".bed", ".bed.gz", ".bedpe")):
                    opener = gzip.open if lower.endswith(".gz") else open
                    with opener(selection, "rt") as source:
                        for line in source:
                            if not line.strip() or line.startswith(
                                ("#", "track", "browser")
                            ):
                                continue
                            fields = line.split()
                            if lower.endswith(".bedpe"):
                                if fields[0] != fields[3]:
                                    continue
                                chrom, start, end = (
                                    fields[0],
                                    int(fields[1]),
                                    int(fields[5]),
                                )
                            else:
                                chrom, start, end = (
                                    fields[0],
                                    int(fields[1]),
                                    int(fields[2]),
                                )
                            if start < 0 or end < start:
                                raise ValueError(f"Invalid interval: {line.strip()}")
                            destination.write(f"{chrom}\t{start}\t{end}\n")
                else:
                    chrom, start, end = _literal_region(selection, lengths)
                    destination.write(f"{chrom}\t{start}\t{end}\n")
        intervals: dict[str, list[tuple[int, int]]] = {}
        with bed.open() as source:
            for line in source:
                chrom, start_text, end_text = line.split()
                length = lengths[chrom]
                start = min(int(start_text), length)
                end = min(int(end_text), length)
                intervals.setdefault(chrom, []).append((start, end))
        covered = 0
        for ranges in intervals.values():
            previous_end = 0
            for start, end in sorted(ranges):
                covered += max(0, end - max(start, previous_end))
                previous_end = max(previous_end, end)
        genome_size = sum(lengths.values())
        if genome_size <= 0:
            raise ValueError("Reference index contains no bases.")
    except (ValueError, KeyError, IndexError) as error:
        raise WGSExtractError(
            f"Invalid DeepVariant region selection: {region}"
        ) from error
    return ["-M", "-L", str(bed)], covered / genome_size


def _compatible_reads(
    alignment: str, ref: str, work: str, threads: str, region: str | None
) -> str:
    version = _cram_version(alignment)
    if version is None or version < (3, 1):
        return alignment
    # Gate/remove this workaround when supported DV images ship htslib >=1.19
    # (htscodecs >=1.5.2); a host-samtools decode cannot detect their SSE4 bug.
    selection, fraction = _transcode_regions(region, ref, work)
    _, final_gb = get_free_space_needed(Path(alignment).stat().st_size, is_cram=True)
    required_gb = final_gb + 1  # Round up the CRAM expansion estimate; allow its index.
    required_bytes = max(64 * 1024**2, math.ceil(required_gb * GB * fraction))
    enough_space = (
        shutil.disk_usage(work).free >= required_bytes
        if region
        else check_free_space(work, required_gb)
    )
    if not enough_space:
        raise WGSExtractError(
            f"Insufficient free disk space for DeepVariant CRAM 3.0 staging "
            f"at {work} (needs ~{required_bytes / GB:.2f} GiB)."
        )
    logging.info(
        "Transcoding CRAM %s.%s to temporary CRAM 3.0: DeepVariant's htslib "
        "1.18 SSE4 rANS decoder can fail on valid CRAM 3.1 blocks.",
        *version,
    )
    compatible = str(Path(work, "reads.cram"))
    run_command(
        [
            *get_sam_view_cmd(threads, fmt="CRAM", reference=ref),
            "--output-fmt-option", "version=3.0",
            "--no-PG", *selection, "-o", compatible, alignment,
        ]
    )  # fmt: skip
    run_command(get_sam_index_cmd(compatible, threads))
    return compatible


# Parabricks DeepVariant: its --mode per model type (WGS and WES are short reads).
_PARABRICKS_MODES = {
    "WGS": [],
    "WES": ["--use-wes-model"],
    "PACBIO": ["--mode", "pacbio"],
}


def _deepvariant_gpu(args: argparse.Namespace) -> None:
    from wgsextract_cli.core.parabricks import call_variants, verify_parabricks

    refusal = WGSExtractError(
        "--gpu (Parabricks) runs DeepVariant's WGS, WES and PACBIO models; "
        "use the CPU version for --checkpoint, ULTIMA or HYBRID_PACBIO_ILLUMINA."
    )
    if _model_type(args) not in _PARABRICKS_MODES or args.checkpoint:
        raise refusal
    verify_dependencies(["samtools", "tabix"])
    verify_parabricks()
    _threads, outdir, ref, _lib = get_base_args(args)
    model_type = _model_type(args, ref)
    if model_type not in _PARABRICKS_MODES:
        raise refusal
    out_vcf = str(Path(outdir, "deepvariant.vcf.gz"))
    logging.info(LOG_MESSAGES["vcf_calling_deepvariant"].format(output=out_vcf))
    call_variants(
        "deepvariant", args.input, ref, out_vcf, args.region,
        _PARABRICKS_MODES[model_type],
    )  # fmt: skip
    ensure_vcf_indexed(out_vcf)
    warn_if_no_variants(out_vcf, ref, args.region)


def cmd_deepvariant(args: argparse.Namespace) -> None:
    if getattr(args, "gpu", False):
        _deepvariant_gpu(args)
        return
    run_deepvariant = get_tool_path("run_deepvariant")
    if not run_deepvariant:
        _verify_docker()
    verify_dependencies(["samtools", "tabix"])
    threads, outdir, ref, _lib = get_base_args(args)
    model_type = _model_type(args, ref)
    if model_type == "ULTIMA" and run_deepvariant:
        # A local run_deepvariant is a release, which has no ULTIMA model yet.
        _verify_docker(model_type)
        run_deepvariant = None
    image = None if run_deepvariant else _verify_docker_image(model_type)
    out_vcf = str(Path(outdir, "deepvariant.vcf.gz"))
    logging.info(LOG_MESSAGES["vcf_calling_deepvariant"].format(output=out_vcf))
    region_args = ["--regions", args.region] if args.region else []

    try:
        with tempfile.TemporaryDirectory(dir=outdir, prefix=".wgse_dv_") as work:
            reads = _compatible_reads(args.input, ref, work, threads, args.region)
            dv_args = [
                "--model_type", model_type,
                "--ref", host_path(ref),
                "--reads", host_path(reads),
                "--output_vcf", host_path(out_vcf),
                "--num_shards", str(threads),
                "--intermediate_results_dir", host_path(work),
                *region_args,
            ]  # fmt: skip
            if args.checkpoint:
                dv_args += ["--customized_model", host_path(args.checkpoint)]
            if image:
                logging.info(f"Running DeepVariant from {image}...")
                run_container(
                    image,
                    ["/opt/deepvariant/bin/run_deepvariant", *dv_args],
                    [ref, args.input, out_vcf, work, args.checkpoint],
                )
            else:
                # stdin closed: DeepVariant's GNU parallel would read it otherwise.
                run_command(
                    [str(run_deepvariant), *dv_args],
                    env=_dv_env(),
                    stdin=subprocess.DEVNULL,
                )
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(f"DeepVariant failed: {e}") from e

    if not Path(out_vcf).exists():
        raise WGSExtractError("DeepVariant failed to produce output VCF.")
    ensure_vcf_indexed(out_vcf)
    warn_if_no_variants(out_vcf, ref, args.region)
