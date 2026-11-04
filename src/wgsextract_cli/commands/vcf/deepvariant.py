import argparse
import gzip
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import zlib
from bisect import bisect_left
from pathlib import Path

from wgsextract_cli.core import alignment_memory, ultima
from wgsextract_cli.core.alignment_metadata import get_bam_header
from wgsextract_cli.core.config import settings
from wgsextract_cli.core.containers import (
    docker,
    docker_info,
    host_path,
    require_image,
    run_container,
)
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.dependency_checks import verify_dependencies
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.native_support import is_linux_arm64, require_native_tool
from wgsextract_cli.core.output_checks import warn_if_no_variants
from wgsextract_cli.core.reference_assets import read_fai_lengths
from wgsextract_cli.core.region_selection import Intervals, selected_intervals
from wgsextract_cli.core.resource_estimates import (
    GIB,
    PostprocessPlan,
    dv_calling_regions,
    estimate_resources,
    plan_dv_postprocess,
    preflight_resources,
)
from wgsextract_cli.core.samtools_commands import (
    alignment_index,
    get_sam_index_cmd,
    get_sam_view_cmd,
)
from wgsextract_cli.core.sequencing import input_platform
from wgsextract_cli.core.utils import WGSExtractError, run_command
from wgsextract_cli.core.variant_files import ensure_vcf_indexed
from wgsextract_cli.core.warnings import GB, check_free_space, get_free_space_needed

from .basic import get_base_args

# Google's image; Bioconda's deepvariant has shipped no DeepVariant since 1.8.
DEFAULT_IMAGE = "google/deepvariant:1.10.0"
DEFAULT_ARM64_IMAGE = "localhost/wgsextract-deepvariant:1.10.0-arm64"
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
    default = DEFAULT_ARM64_IMAGE if is_linux_arm64() else DEFAULT_IMAGE
    return str(settings.get("deepvariant_image") or default)


_ARM64_SETUP = (
    "Build the native CPU DeepVariant 1.10.0 image with "
    "packaging/deepvariant-arm64/build.sh (see docs/deepvariant-arm64.md); "
    "set deepvariant_image in config.toml if you "
    "use another local tag. Dependencies and images must already be installed. "
    "Google's x86 image, emulation and unaudited PATH launchers are not supported."
)


def _require_arm64_wgs(model_type: str, checkpoint: str | None = None) -> None:
    if model_type != "WGS" or checkpoint:
        selection = "--checkpoint" if checkpoint else f"model {model_type}"
        raise WGSExtractError(
            f"DeepVariant {selection} is not qualified on Linux ARM64; "
            f"only the default WGS model is qualified. {_ARM64_SETUP}"
        )


def _verify_arm64_image(model_type: str = "WGS", checkpoint: str | None = None) -> str:
    _require_arm64_wgs(model_type, checkpoint)
    docker_info(_ARM64_SETUP)
    architecture = docker_info(_ARM64_SETUP, "{{.Architecture}}").strip().lower()
    if architecture not in {"aarch64", "arm64"}:
        raise WGSExtractError(
            "DeepVariant requires a native ARM64 Docker engine on Linux ARM64; "
            "an x64 engine or emulation is not supported."
        )
    image = deepvariant_image(model_type)
    require_image(
        image, f"Native DeepVariant image isn't installed: {image}. {_ARM64_SETUP}"
    )
    inspected = run_command(
        [docker(_ARM64_SETUP), "image", "inspect", "--format",
         "{{.Os}}/{{.Architecture}}", image],
        capture_output=True,
        check=False,
    )  # fmt: skip
    if inspected.returncode or (inspected.stdout or "").strip() != "linux/arm64":
        raise WGSExtractError(
            f"DeepVariant image {image} must be linux/arm64; "
            "x86 images and emulation are not supported."
        )
    return image


def _model_type(args: argparse.Namespace, ref: str | None = None) -> str:
    """Explicit model selectors take precedence over platform defaults."""
    model = getattr(args, "model_type", None)
    ont = getattr(args, "ont", False)
    if (ont or model == "ONT_R104") and (
        getattr(args, "pacbio", False)
        or getattr(args, "wes", False)
        or (ont and model not in (None, "ONT_R104"))
    ):
        raise WGSExtractError(
            "--ont/--model-type ONT_R104 cannot be combined with "
            "--pacbio, --wes or a different --model-type."
        )
    if ont:
        return "ONT_R104"
    if getattr(args, "pacbio", False):
        return "PACBIO"
    if model:
        return str(model)
    if getattr(args, "wes", False):
        return "WES"
    detected = getattr(args, "input_platform", None)
    if detected is None and ref:
        detected = input_platform(args.input, ref)
    if detected == "ont":
        logging.info(
            "ONT reads: calling with DeepVariant's ONT_R104 model (R10.4.1 only)."
        )
        return "ONT_R104"
    if ref and ultima.alignment_flow_data(args.input, ref).flow_aware:
        logging.info("Ultima reads: calling with DeepVariant's ULTIMA model.")
        return "ULTIMA"
    return "WGS"


def _require_ont_x86(model_type: str) -> None:
    if model_type == "ONT_R104" and platform.machine().lower() not in {
        "x86_64",
        "amd64",
    }:
        raise WGSExtractError(
            "DeepVariant ONT_R104 requires x86-64; it is not qualified on ARM64, "
            "including Apple Silicon emulation. Use an x86-64 host."
        )


def _warn_ont_chemistry(alignment: str, ref: str) -> None:
    metadata = []
    for line in get_bam_header(alignment, ref).splitlines():
        if line.startswith("@PG\t"):
            metadata.append(line)
        elif line.startswith("@RG\t"):
            metadata.extend(
                field for field in line.split("\t") if field.startswith("DS:")
            )
    if re.search(
        r"(?<![a-z0-9])r9[._-]4(?:[._-]1)?(?![a-z0-9])",
        "\n".join(metadata),
        re.IGNORECASE,
    ):
        logging.warning(
            "R9.4.1/R9.4 basecaller metadata detected: this chemistry is unsupported "
            "by DeepVariant ONT_R104, which supports R10.4.1 only. "
            "Do not interpret these calls as validated ONT results."
        )


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


def _reference_lengths(ref: str) -> dict[str, int]:
    lengths = read_fai_lengths(ref)
    if not lengths:
        run_command(["samtools", "faidx", ref])
        lengths = read_fai_lengths(ref)
    return lengths


def _selected_intervals(region: str, lengths: dict[str, int]) -> Intervals:
    try:
        return selected_intervals(region, lengths)
    except (ValueError, OSError) as error:
        raise WGSExtractError(
            f"Invalid DeepVariant region selection: {region}: {error}"
        ) from error


def _transcode_regions(
    region: str | None, ref: str, work: str
) -> tuple[list[str], Intervals]:
    if not region:
        return [], {}
    # DV accepts whitespace-separated literals and BED/BED.gz/BEDPE files.
    # One BED + the multi-region iterator preserves their union without duplicates.
    merged = _selected_intervals(region, _reference_lengths(ref))
    bed = Path(work, "regions.bed")
    with bed.open("w", newline="\n") as destination:
        for chrom, ranges in merged.items():
            for start, end in ranges:
                destination.write(f"{chrom}\t{start}\t{end}\n")
    return ["-M", "-L", str(bed)], merged


def _indexed_slice_bytes(
    alignment: str, ref: str, intervals: dict[str, list[tuple[int, int]]]
) -> int | None:
    """CRAI slice sizes reflect capture depth; reference-base fractions do not."""
    index = alignment_index(alignment)
    if not index or not index.endswith(".crai"):
        return None
    try:
        names = [
            next(field[3:] for field in line.split("\t") if field.startswith("SN:"))
            for line in get_bam_header(alignment, ref).splitlines()
            if line.startswith("@SQ\t")
        ]
        if not names or not all(names) or len(set(names)) != len(names):
            return None
        with open(index, "rb") as signature:
            compressed = signature.read(2) == b"\x1f\x8b"
        opener = gzip.open if compressed else open
        seen: dict[tuple[int, int], int] = {}
        selected: set[tuple[int, int]] = set()
        with opener(index, "rt") as source:
            for line in source:
                rid, start, span, container, offset, size = map(int, line.split())
                if (
                    rid < -1
                    or rid >= len(names)
                    or min(container, offset) < 0
                    or size <= 0
                    or (rid >= 0 and (start < 1 or span < 1))
                ):
                    return None
                key = (container, offset)
                if key in seen and seen[key] != size:
                    return None
                seen[key] = size
                if rid == -1:
                    continue
                ranges = intervals.get(names[rid], [])
                start -= 1  # CRAI is 1-based; the staging BED is 0-based.
                hit = bisect_left(ranges, (start + span, 0)) - 1
                if hit >= 0 and ranges[hit][1] > start:
                    # A multi-reference slice has one CRAI row per reference.
                    selected.add(key)
        return sum(seen[key] for key in selected) if seen else None
    except (OSError, EOFError, ValueError, StopIteration, zlib.error):
        return None


def _compatible_reads(
    alignment: str, ref: str, work: str, threads: str, region: str | None
) -> str:
    version = _cram_version(alignment)
    if version is None or version < (3, 1):
        return alignment
    # Gate/remove this workaround when supported DV images ship htslib >=1.19
    # (htscodecs >=1.5.2); a host-samtools decode cannot detect their SSE4 bug.
    selection, intervals = _transcode_regions(region, ref, work)
    _, final_gb = get_free_space_needed(Path(alignment).stat().st_size, is_cram=True)
    required_gb = final_gb + 1  # Round up the CRAM expansion estimate; allow its index.
    selected_bytes = _indexed_slice_bytes(alignment, ref, intervals) if region else None
    # Round the existing ~1.95x CRAM expansion allowance up to 2x for slices.
    required_bytes = (
        max(64 * 1024**2, 2 * selected_bytes)
        if selected_bytes is not None
        else required_gb * GB
    )
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
        "Transcoding CRAM %s.%s to temporary CRAM 3.0: DeepVariant's x86-64 "
        "images (htslib 1.18 / htscodecs 1.5.1) have an SSE4 rANS decoder bug "
        "that can fail on valid CRAM 3.1 blocks; staging applies to every image.",
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


def _available_memory(setup: str | None) -> int | None:
    """Free host RAM, capped by the Docker engine's (e.g. Docker Desktop's VM)."""
    try:
        available = alignment_memory.available_memory()
    except OSError:
        return None
    if setup is not None:
        engine = docker_info(setup, "{{.MemTotal}}").strip()
        if engine.isdigit() and int(engine) > 0:
            available = min(available, int(engine))
    return available


def _postprocess_plan(
    model_type: str, ref: str, region: str | None, threads: int, setup: str | None
) -> PostprocessPlan | None:
    """Bound postprocess_variants by RAM; raise when one worker cannot fit."""
    if model_type == "WES":
        return None  # run_deepvariant postprocesses WES sequentially.
    lengths = read_fai_lengths(ref)
    if not lengths:
        logging.warning(
            f"No {ref}.fai: DeepVariant's postprocess_variants RAM isn't bounded."
        )
        return None
    if region:
        _selected_intervals(region, lengths)  # Report bad selections clearly.
    regions = dv_calling_regions(lengths, region)
    assert regions is not None
    available = _available_memory(setup)
    plan = plan_dv_postprocess(regions, threads, available)
    if plan.fits is False and available is not None:
        raise WGSExtractError(
            "Not enough memory for DeepVariant's postprocess_variants step: one "
            f"worker sorting {plan.partitions} partitions needs ~"
            f"{plan.worker_bytes / GIB:.1f} GiB plus 1 GiB of headroom, but "
            f"{available / GIB:.1f} GiB is available. Free memory (close other "
            "jobs), use a machine with more RAM, or call a smaller --region."
        )
    if plan.cpus < threads:
        logging.warning(
            f"Limiting DeepVariant postprocess_variants to {plan.cpus} of {threads} "
            f"workers over {plan.partitions} partitions (~"
            f"{plan.worker_bytes / GIB:.1f} GiB each) to fit "
            f"{(available or 0) / GIB:.1f} GiB of available memory."
        )
    elif plan.partitions > threads:
        logging.info(
            f"Splitting DeepVariant postprocess_variants into {plan.partitions} "
            f"partitions (~{plan.worker_bytes / GIB:.1f} GiB per worker) to fit "
            f"{(available or 0) / GIB:.1f} GiB of available memory."
        )
    return plan


def _postprocess_args(plan: PostprocessPlan | None, threads: int) -> list[str]:
    if plan is None:
        return []
    flags = []
    if plan.cpus != threads:
        flags += ["--postprocess_cpus", str(plan.cpus)]
    if plan.partitions > plan.cpus:
        # postprocess_variants' --num_partitions needs DeepVariant 1.9+.
        flags += [
            "--postprocess_variants_extra_args",
            f"num_partitions={plan.partitions}",
        ]
    return flags


_OOM_ERROR = (
    "DeepVariant ran out of memory: a process in its container was OOM-killed, "
    "so the run was stopped rather than left waiting on the killed worker. "
    "Free memory (close other jobs) or lower --threads, then rerun."
)


def _complete_gzip(path: Path) -> bool:
    try:
        with gzip.open(path, "rb") as source:
            while source.read(1 << 20):
                pass
    except (OSError, EOFError, zlib.error):
        return False
    return True


def _keep_call_variants_outputs(work: str, outdir: str) -> Path | None:
    """Move complete call_variants outputs out of the deleted work directory.

    Only some models write the small model's make_examples outputs.
    """
    kept_files: list[Path] = []
    for prefix in ("call_variants_output", "make_examples_call_variant_outputs"):
        files = sorted(Path(work).glob(f"{prefix}*.gz"))
        counts = {
            int(match.group(1)) if (match := re.search(r"-of-(\d+)", f.name)) else 0
            for f in files
        }
        if files and counts != {len(files)}:
            return None  # An incomplete shard set.
        kept_files += files
    if not any(f.name.startswith("call_variants_output") for f in kept_files):
        return None
    if not all(_complete_gzip(path) for path in kept_files):
        return None
    kept = Path(outdir, "deepvariant_intermediates")
    kept.mkdir(exist_ok=True)
    for path in kept_files:
        shutil.move(path, kept / path.name)
    return kept


# Parabricks DeepVariant: its --mode per model type (WGS and WES are short reads).
_PARABRICKS_MODES = {
    "WGS": [],
    "WES": ["--use-wes-model"],
    "PACBIO": ["--mode", "pacbio"],
    "ONT_R104": ["--mode", "ont"],
}


def _deepvariant_gpu(args: argparse.Namespace) -> None:
    from wgsextract_cli.core.parabricks import call_variants, verify_parabricks

    refusal = WGSExtractError(
        "--gpu (Parabricks) runs DeepVariant's WGS, WES, PACBIO and ONT_R104 models; "
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
    _require_ont_x86(model_type)
    if model_type == "ONT_R104":
        _warn_ont_chemistry(args.input, ref)
    out_vcf = str(Path(outdir, "deepvariant.vcf.gz"))
    logging.info(LOG_MESSAGES["vcf_calling_deepvariant"].format(output=out_vcf))
    call_variants(
        "deepvariant", args.input, ref, out_vcf, args.region,
        _PARABRICKS_MODES[model_type],
    )  # fmt: skip
    ensure_vcf_indexed(out_vcf)
    warn_if_no_variants(out_vcf, ref, args.region)


def cmd_deepvariant(args: argparse.Namespace) -> None:
    require_native_tool("run_deepvariant")
    batch_size = getattr(args, "batch_size", None)
    if batch_size is not None and batch_size < 1:
        raise WGSExtractError("DeepVariant --batch-size must be positive.")
    if getattr(args, "gpu", False):
        if batch_size is not None:
            raise WGSExtractError("DeepVariant --batch-size is a CPU-only option.")
        require_native_tool("parabricks")
        _deepvariant_gpu(args)
        return
    arm64_image = (
        _verify_arm64_image(_model_type(args), getattr(args, "checkpoint", None))
        if is_linux_arm64()
        else None
    )
    if not arm64_image:
        _require_ont_x86(_model_type(args))
    run_deepvariant = None if arm64_image else get_tool_path("run_deepvariant")
    if not run_deepvariant and not arm64_image:
        _verify_docker()
    verify_dependencies(["samtools", "tabix"])
    threads, outdir, ref, _lib = get_base_args(args)
    model_type = _model_type(args, ref)
    if arm64_image:
        _require_arm64_wgs(model_type, getattr(args, "checkpoint", None))
    _require_ont_x86(model_type)
    if model_type == "ONT_R104":
        _warn_ont_chemistry(args.input, ref)
    if model_type == "ULTIMA" and run_deepvariant:
        # A local run_deepvariant is a release, which has no ULTIMA model yet.
        _verify_docker(model_type)
        run_deepvariant = None
    image = arm64_image or (
        None if run_deepvariant else _verify_docker_image(model_type)
    )
    setup = (
        _ARM64_SETUP if arm64_image else _SETUP.format(image=image) if image else None
    )
    postprocess = _postprocess_plan(model_type, ref, args.region, int(threads), setup)
    preflight_resources(
        lambda: estimate_resources(
            "deepvariant",
            input_paths=[args.input],
            output_dir=outdir,
            reference=ref,
            threads=threads,
            region=args.region,
            inference_batch_size=batch_size or 1024,
            deepvariant_model=model_type,
        )
    )
    out_vcf = str(Path(outdir, "deepvariant.vcf.gz"))
    logging.info(LOG_MESSAGES["vcf_calling_deepvariant"].format(output=out_vcf))
    region_args = ["--regions", args.region] if args.region else []

    try:
        with tempfile.TemporaryDirectory(dir=outdir, prefix=".wgse_dv_") as work:
            try:
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
                if batch_size is not None:
                    dv_args += [
                        "--call_variants_extra_args",
                        f"batch_size={batch_size}",
                    ]
                dv_args += _postprocess_args(postprocess, int(threads))
                if image:
                    logging.info(f"Running DeepVariant from {image}...")
                    paths = [ref, args.input, out_vcf, work, args.checkpoint]
                    if arm64_image:
                        # Bypass community entrypoints that auto-select INT8/BF16.
                        run_container(
                            image, dv_args, paths,
                            entrypoint="/opt/deepvariant/bin/run_deepvariant",
                            temporary_directory=work,
                            local_only=True,
                            oom_error=_OOM_ERROR,
                        )  # fmt: skip
                    else:
                        run_container(
                            image,
                            ["/opt/deepvariant/bin/run_deepvariant", *dv_args],
                            paths,
                            temporary_directory=work,
                            oom_error=_OOM_ERROR,
                        )
                else:
                    # stdin closed: DeepVariant's GNU parallel would read it otherwise.
                    run_command(
                        [str(run_deepvariant), *dv_args],
                        env=_dv_env(),
                        stdin=subprocess.DEVNULL,
                    )
            except BaseException:
                kept = _keep_call_variants_outputs(work, outdir)
                if kept:
                    logging.warning(
                        f"Kept DeepVariant's complete call_variants outputs in {kept}."
                    )
                raise
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(f"DeepVariant failed: {e}") from e

    if not Path(out_vcf).exists():
        raise WGSExtractError("DeepVariant failed to produce output VCF.")
    ensure_vcf_indexed(out_vcf)
    warn_if_no_variants(out_vcf, ref, args.region)
