import contextlib
import glob
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
from collections.abc import Callable
from pathlib import Path
from typing import Any

from wgsextract_cli.api._cli import cli_handler
from wgsextract_cli.api.vcf import DEEPVARIANT_STEPS, VcfDeepvariantParams
from wgsextract_cli.core import alignment_memory, ultima
from wgsextract_cli.core.alignment_metadata import get_bam_header
from wgsextract_cli.core.config import settings
from wgsextract_cli.core.containers import (
    docker,
    docker_info,
    host_path,
    image_identity,
    require_image,
    run_container,
)
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.dependency_checks import verify_dependencies
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.native_support import is_linux_arm64, require_native_tool
from wgsextract_cli.core.output_checks import warn_if_no_variants
from wgsextract_cli.core.paths import absolute_path
from wgsextract_cli.core.reference_assets import read_fai_lengths
from wgsextract_cli.core.region_selection import (
    Intervals,
    region_items,
    selected_intervals,
)
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
from wgsextract_cli.core.steps import Decision, Step, StepJournal, resume_policy
from wgsextract_cli.core.utils import UsageError, WGSExtractError, run_command
from wgsextract_cli.core.variant_files import ensure_vcf_indexed
from wgsextract_cli.core.warnings import GB, check_free_space, get_free_space_needed

from .basic import record_variants, setup_caller

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


def _model_type(
    params: VcfDeepvariantParams,
    ref: str | None = None,
    platform: str | None = None,
) -> str:
    """Explicit model selectors take precedence over platform defaults: the
    detected ``platform``, else (given ``ref``) the reads' own."""
    model, ont = params.model_type, params.ont
    if (ont or model == "ONT_R104") and (
        params.pacbio or params.wes or (ont and model not in (None, "ONT_R104"))
    ):
        raise UsageError(
            "--ont/--model-type ONT_R104 cannot be combined with "
            "--pacbio, --wes or a different --model-type."
        )
    if ont:
        return "ONT_R104"
    if params.pacbio:
        return "PACBIO"
    if model:
        return str(model)
    if params.wes:
        return "WES"
    input_file = str(params.input)
    detected = platform
    if detected is None and ref:
        detected = input_platform(input_file, ref)
    if detected == "ont":
        logging.info(
            "ONT reads: calling with DeepVariant's ONT_R104 model (R10.4.1 only)."
        )
        return "ONT_R104"
    if ref and ultima.alignment_flow_data(input_file, ref).flow_aware:
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


def _container_regions(region: str) -> str:
    """``region`` with its BED files at the paths mounted in the container."""
    return " ".join(
        host_path(p) if Path(p).is_file() else p
        for p in region_items(region, commas=False)
    )


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


def _supports_num_partitions(
    image: str | None, run_deepvariant: str | None, setup: str | None
) -> bool:
    """Whether postprocess_variants has --num_partitions (DeepVariant 1.9+).

    Pinned defaults and version tags decide without running anything;
    otherwise ask the image's or launcher's postprocess_variants.
    """
    if image in {DEFAULT_IMAGE, DEFAULT_ARM64_IMAGE, DEFAULT_ULTIMA_IMAGE}:
        return True
    if image:
        tag = re.search(r":v?(\d+)\.(\d+)[^/:@]*(?:@|$)", image)
        if tag:
            return (int(tag.group(1)), int(tag.group(2))) >= (1, 9)
        probe = [
            docker(setup or ""), "run", "--rm", "--pull=never",
            "--entrypoint", "/opt/deepvariant/bin/postprocess_variants",
            image, "--helpfull",
        ]  # fmt: skip
    elif run_deepvariant:
        tool = Path(run_deepvariant).with_name("postprocess_variants")
        if not tool.is_file():
            return False
        probe = [str(tool), "--helpfull"]
    else:
        return False
    try:
        result = run_command(probe, capture_output=True, check=False, env=_dv_env())
    except OSError:
        return False
    return "num_partitions" in (result.stdout or "") + (result.stderr or "")


def _postprocess_plan(
    model_type: str,
    ref: str,
    region: str | None,
    threads: int,
    available: int | None,
    supports_partitions: Callable[[], bool],
) -> PostprocessPlan | None:
    """Bound postprocess_variants by RAM; raise when one worker cannot fit.

    ``supports_partitions`` is asked only when more partitions are wanted.
    """
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
    plan = plan_dv_postprocess(regions, threads, available)
    upgrade = ""
    if plan.partitions > plan.cpus and not supports_partitions():
        upgrade = ", use DeepVariant 1.9+ (which can split this step further)"
        logging.warning(
            "This DeepVariant's postprocess_variants has no --num_partitions "
            "(DeepVariant 1.9+), so only its workers can be reduced to save memory."
        )
        plan = plan_dv_postprocess(regions, threads, available, partitions=False)
    if plan.fits is False and available is not None:
        raise WGSExtractError(
            "Not enough memory for DeepVariant's postprocess_variants step: one "
            f"worker sorting {plan.partitions} partitions needs ~"
            f"{plan.worker_bytes / GIB:.1f} GiB plus 1 GiB of headroom, but "
            f"{available / GIB:.1f} GiB is available. Free memory (close other "
            f"jobs), use a machine with more RAM{upgrade}, or call a smaller "
            "--region."
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


# Parabricks DeepVariant: its --mode per model type (WGS and WES are short reads).
_PARABRICKS_MODES = {
    "WGS": [],
    "WES": ["--use-wes-model"],
    "PACBIO": ["--mode", "pacbio"],
    "ONT_R104": ["--mode", "ont"],
}


def _deepvariant_gpu(params: VcfDeepvariantParams) -> None:
    from wgsextract_cli.core.parabricks import call_variants, verify_parabricks

    refusal = (
        "--gpu (Parabricks) runs DeepVariant's WGS, WES, PACBIO and ONT_R104 models; "
        "use the CPU version for --checkpoint, ULTIMA or HYBRID_PACBIO_ILLUMINA."
    )
    # The options chose an unsupported model (usage); below, the input did.
    if _model_type(params) not in _PARABRICKS_MODES or params.checkpoint:
        raise UsageError(refusal)
    verify_dependencies(["samtools", "tabix"])
    verify_parabricks()
    setup = setup_caller(params)
    outdir, ref = setup.outdir, setup.ref
    input_file, region = str(params.input), params.region
    model_type = _model_type(params, ref, setup.platform)
    if model_type not in _PARABRICKS_MODES:
        raise WGSExtractError(refusal)
    _require_ont_x86(model_type)
    if model_type == "ONT_R104":
        _warn_ont_chemistry(input_file, ref)
    out_vcf = str(Path(outdir, "deepvariant.vcf.gz"))
    logging.info(LOG_MESSAGES["vcf_calling_deepvariant"].format(output=out_vcf))
    call_variants(
        "deepvariant", input_file, ref, out_vcf, region,
        _PARABRICKS_MODES[model_type],
    )  # fmt: skip
    ensure_vcf_indexed(out_vcf)
    record_variants(out_vcf, "variants", warn_if_no_variants(out_vcf, ref, region))


def run_deepvariant(params: VcfDeepvariantParams) -> None:
    """``vcf deepvariant``: DeepVariant into ``deepvariant.vcf.gz``, in
    resumable stages, or Parabricks."""
    require_native_tool("run_deepvariant")
    batch_size = params.batch_size
    if batch_size is not None and batch_size < 1:
        raise UsageError("DeepVariant --batch-size must be positive.")
    if params.gpu:
        if batch_size is not None:
            raise UsageError("DeepVariant --batch-size is a CPU-only option.")
        if params.dry_run or params.force_steps:
            raise UsageError(
                "--dry-run and --force-step apply to CPU DeepVariant's stages; "
                "--gpu always runs Parabricks in one step."
            )
        require_native_tool("parabricks")
        _deepvariant_gpu(params)
        return
    checkpoint = str(params.checkpoint) if params.checkpoint else None
    arm64_image = (
        _verify_arm64_image(_model_type(params), checkpoint)
        if is_linux_arm64()
        else None
    )
    if not arm64_image:
        _require_ont_x86(_model_type(params))
    local_dv = None if arm64_image else get_tool_path("run_deepvariant")
    if not local_dv and not arm64_image:
        _verify_docker()
    verify_dependencies(["samtools", "tabix"])
    caller = setup_caller(params)
    threads, outdir, ref = caller.threads, caller.outdir, caller.ref
    input_file, region = str(params.input), params.region
    model_type = _model_type(params, ref, caller.platform)
    if arm64_image:
        _require_arm64_wgs(model_type, checkpoint)
    _require_ont_x86(model_type)
    if model_type == "ONT_R104":
        _warn_ont_chemistry(input_file, ref)
    if model_type == "ULTIMA" and local_dv:
        # A local run_deepvariant is a release, which has no ULTIMA model yet.
        _verify_docker(model_type)
        local_dv = None
    image = arm64_image or (None if local_dv else _verify_docker_image(model_type))
    setup = (
        _ARM64_SETUP if arm64_image else _SETUP.format(image=image) if image else None
    )
    available = _available_memory(setup)
    postprocess = _postprocess_plan(
        model_type, ref, region, int(threads), available,
        lambda: _supports_num_partitions(image, local_dv, setup),
    )  # fmt: skip
    out_vcf = str(Path(outdir, "deepvariant.vcf.gz"))
    # Absolute, like the journal's paths, so stage files match their records.
    work = absolute_path(Path(outdir, WORK_DIRECTORY))
    staged_reads = _needs_staging(input_file)
    reads = str(work / "reads" / "reads.cram") if staged_reads else input_file
    region_files = [
        p for p in region_items(region or "", commas=False) if Path(p).is_file()
    ]
    launcher = _Launcher(
        local_dv,
        arm64_image or image,
        bool(arm64_image),
        [ref, input_file, out_vcf, str(work), checkpoint, *region_files],
        work,
    )
    inputs: dict[str, str | None] = {
        "alignment": input_file,
        "reference": ref,
        "launcher": local_dv if launcher.image is None else None,
        **{f"regions:{i}": path for i, path in enumerate(region_files)},
        **_checkpoint_files(checkpoint),
    }

    with StepJournal.for_output(
        out_vcf, "deepvariant", resume_policy(params)
    ) as journal:
        shards = _shard_count(journal, int(threads))
        dv_args = [
            "--model_type", model_type,
            "--ref", host_path(ref),
            "--reads", host_path(reads),
            "--output_vcf", host_path(str(work / Path(out_vcf).name)),
            "--num_shards", str(shards),
            "--intermediate_results_dir", host_path(str(work)),
            *(["--regions", _container_regions(region)] if region else []),
        ]  # fmt: skip
        if checkpoint:
            dv_args += ["--customized_model", host_path(checkpoint)]
        if batch_size is not None:
            dv_args += ["--call_variants_extra_args", f"batch_size={batch_size}"]
        dv_args += _postprocess_args(postprocess, int(threads))
        if not journal.dry_run and not any(journal.recorded(s) for s in STEPS):
            preflight_resources(
                lambda: estimate_resources(
                    "deepvariant",
                    input_paths=[input_file],
                    output_dir=outdir,
                    reference=ref,
                    threads=threads,
                    region=region,
                    inference_batch_size=batch_size or 1024,
                    deepvariant_model=model_type,
                    postprocess_plan=postprocess,
                    available_ram=available,
                )
            )
        logging.info(LOG_MESSAGES["vcf_calling_deepvariant"].format(output=out_vcf))
        try:
            done = _run_stages(
                journal, launcher, dv_args,
                inputs=inputs,
                params={
                    "image": (
                        image_identity(launcher.image)
                        if launcher.image else local_dv
                    ),
                    "model_type": model_type,
                    "shards": shards,
                },
                region=region,
                jobs=int(threads),
                staging=(
                    (lambda: _compatible_reads(
                        input_file, ref, str(work / "reads"), threads, region
                    )) if staged_reads else None
                ),
                out_vcf=out_vcf,
            )  # fmt: skip
        except (OSError, subprocess.SubprocessError) as e:
            raise WGSExtractError(
                f"DeepVariant failed: {e}. Its finished stages are kept in {work}; "
                "rerun the same command to resume from them."
            ) from e
    if done:
        record_variants(out_vcf, "variants", warn_if_no_variants(out_vcf, ref, region))


cmd_deepvariant = cli_handler(VcfDeepvariantParams, run_deepvariant)
STEPS = DEEPVARIANT_STEPS
WORK_DIRECTORY = ".deepvariant.work"
_COMMAND_MARK = "***** Running the command:*****"
_STAGE_BINARY = re.compile(r"/(make_examples|call_variants|postprocess_variants)\s")
# Settings that bound a stage's memory or CPU use but not what it writes.
_RESOURCE_FLAGS = re.compile(r'\s--(?:cpus|num_partitions|batch_size)[= ]"?\d+"?')
_JOBLOG = ".make_examples.joblog"
_DV_BIN = "/opt/deepvariant/bin"


def _shard_count(journal: StepJournal, threads: int) -> int:
    """make_examples' shard count: an earlier run's, so a rerun with other
    --threads keeps its finished shards and stages (--threads then sets only
    how many run at once), unless steps are forced to rerun."""
    shards = journal.recorded_params("make_examples").get("shards")
    if not isinstance(shards, int) or any(journal.policy.forced(s) for s in STEPS):
        return threads
    if shards != threads:
        logging.info(
            f"Keeping the {shards} make_examples shards of the earlier run; "
            f"{threads} run at once."
        )
    return shards


def _checkpoint_files(checkpoint: str | None) -> dict[str, str]:
    """The files of a --checkpoint (a model directory, or a checkpoint prefix
    and the files beside it), by input role."""
    if not checkpoint:
        return {}
    path = Path(checkpoint)
    if path.is_dir():
        files = [p for p in path.rglob("*") if p.is_file()]
    else:
        files = list(path.parent.glob(glob.escape(path.name) + "*"))
        files.append(path.parent / "example_info.json")
    return {
        f"checkpoint:{p.relative_to(path if path.is_dir() else path.parent)}": str(p)
        for p in sorted(set(files))
        if p.is_file()
    }


def _needs_staging(alignment: str) -> bool:
    version = _cram_version(alignment)
    return version is not None and version >= (3, 1)


class _Launcher:
    """Runs DeepVariant's run_deepvariant, or one of the stage commands it
    prints, on this machine or in its image."""

    def __init__(
        self,
        local: str | None,
        image: str | None,
        arm64: bool,
        paths: list[str | None],
        work: Path,
    ):
        self.local = local
        self.image = image
        self.arm64 = arm64
        self.paths = paths
        self.work = work

    def launcher(self) -> str:
        return str(self.local) if not self.image else f"{_DV_BIN}/run_deepvariant"

    def __call__(self, argv: list[str], *, capture: bool = False) -> str:
        if not self.image:
            # stdin closed: DeepVariant's GNU parallel would read it otherwise.
            result = run_command(
                argv, env=_dv_env(), stdin=subprocess.DEVNULL, capture_output=capture
            )
            return (result.stdout or "") if capture else ""
        if not capture:
            # Its temporary files stay out of the stage outputs beside them.
            return self._in_image(argv, self.work / ".tmp", capture)
        # Planning (also in --dry-run, which takes no lock) gets its own
        # temporary folder, away from a running stage's.
        self.work.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix=".plan-", dir=self.work))
        try:
            return self._in_image(argv, tmp, capture)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _in_image(self, argv: list[str], tmp: Path, capture: bool) -> str:
        assert self.image is not None
        tmp.mkdir(parents=True, exist_ok=True)
        options: dict[str, Any] = {
            "temporary_directory": str(tmp),
            "oom_error": None if capture else _OOM_ERROR,
            "capture_output": capture,
        }
        if self.arm64:
            # Bypass community entrypoints that auto-select INT8/BF16.
            result = run_container(
                self.image, argv[1:], self.paths,
                entrypoint=argv[0], local_only=True, **options,
            )  # fmt: skip
        else:
            result = run_container(self.image, argv, self.paths, **options)
        return (result.stdout or "") if capture else ""


def _stage_commands(plan: str) -> dict[str, list[str]]:
    """The shell commands run_deepvariant --dry_run=true printed, by stage.
    Commands after postprocess_variants (reports) belong to it."""
    stages: dict[str, list[str]] = {}
    stage = None
    for chunk in plan.split(_COMMAND_MARK)[1:]:
        command = chunk.strip().split("\n\n", 1)[0].strip()
        if match := _STAGE_BINARY.search(command):
            stage = match.group(1)
        if stage is None:
            break
        stages.setdefault(stage, []).append(command)
    if list(stages) != list(STEPS[1:]):
        raise WGSExtractError(
            "Couldn't read DeepVariant's make_examples, call_variants and "
            "postprocess_variants commands from run_deepvariant --dry_run=true "
            f"(found: {', '.join(stages) or 'none'})."
        )
    return stages


def _stage_files(work: Path, claimed: set[Path]) -> list[Path]:
    """The stage outputs in ``work`` that no earlier stage wrote."""
    if not work.is_dir():
        return []
    return sorted(
        path
        for path in work.iterdir()
        if path.is_file() and not path.name.startswith(".") and path not in claimed
    )


def _run_stages(
    journal: StepJournal,
    run: _Launcher,
    dv_args: list[str],
    *,
    inputs: dict[str, str | None],
    params: dict[str, Any],
    region: str | None,
    jobs: int,
    staging: Callable[[], str] | None,
    out_vcf: str,
) -> bool:
    """Run DeepVariant stage by stage, skipping the ones an earlier run
    finished. A killed make_examples keeps its finished shards, and runs
    ``jobs`` of them at once. Returns whether the VCF was made or kept (not
    in --dry-run)."""
    work = run.work
    work.mkdir(parents=True, exist_ok=True)
    try:
        plan = run([run.launcher(), *dv_args, "--dry_run=true"], capture=True)
    finally:
        if journal.dry_run:
            with contextlib.suppress(OSError):
                work.rmdir()
    commands = _stage_commands(plan)
    staged_vcf = work / Path(out_vcf).name

    def step(name: str) -> Step:
        # A stage's key holds its own and every earlier stage's commands, so
        # even the final VCF, kept after the others are deleted, is redone
        # when an earlier stage's settings (such as the model) change.
        upto = STEPS[1 : STEPS.index(name) + 1]
        text = "\n".join(c for stage in upto for c in commands[stage])
        return Step(
            name,
            inputs=inputs,
            outputs=[out_vcf] if name == "postprocess_variants" else (),
            params={**params, "commands": _RESOURCE_FLAGS.sub("", text)},
        )

    staging_step = Step(
        "stage_reads",
        inputs={
            role: path
            for role, path in inputs.items()
            if role in ("alignment", "reference") or role.startswith("regions:")
        },
        outputs=[work / "reads" / "reads.cram", work / "reads" / "reads.cram.crai"],
        params={"region": region},
        tools=["samtools"],
    )
    final = step("postprocess_variants")
    if not any(journal.policy.forced(s) for s in STEPS[:-1]):
        if not journal.check(final).run:
            journal.decide(final)
            if not journal.dry_run:
                ensure_vcf_indexed(out_vcf)
            return not journal.dry_run

    decisions: list[Decision] = []
    claimed: set[Path] = set()
    for name in STEPS:
        if name == "stage_reads" and staging is None:
            continue
        current = staging_step if name == "stage_reads" else step(name)
        decision = journal.decide(current, after=decisions)
        decisions.append(decision)
        if journal.dry_run:
            continue
        if not decision.run:
            claimed |= {journal.resolve(item["path"]) for item in decision.outputs}
            continue
        # A make_examples killed with the same inputs and settings keeps the
        # shards it finished (GNU parallel's job log); anything else restarts.
        resume = name == "make_examples" and decision.partial
        if not resume:
            for stale in _stage_files(work, claimed):
                stale.unlink()
            with contextlib.suppress(FileNotFoundError):
                (work / _JOBLOG).unlink()
        with journal.running(decision, keep_partial=resume) as handle:
            if name == "stage_reads":
                assert staging is not None
                (work / "reads").mkdir(exist_ok=True)
                staging()
                continue
            for command in commands[name]:
                if name == "make_examples":
                    # GNU parallel logs each finished shard; --resume-failed
                    # then runs only the shards a killed run didn't finish.
                    joblog = (
                        f'--jobs {jobs} --joblog "{host_path(str(work / _JOBLOG))}"'
                    )
                    if resume:
                        logging.info(
                            "Resuming make_examples: keeping the shards an "
                            "interrupted run finished."
                        )
                        joblog += " --resume-failed"
                    command = command.replace("parallel ", f"parallel {joblog} ", 1)
                run(["/bin/bash", "-c", command])
            if name == "postprocess_variants":
                for suffix in ("", ".tbi"):
                    source = Path(str(staged_vcf) + suffix)
                    if source.exists():
                        os.replace(source, out_vcf + suffix)
                if not Path(out_vcf).exists():
                    raise WGSExtractError("DeepVariant failed to produce output VCF.")
                ensure_vcf_indexed(out_vcf)
                continue
            outputs = _stage_files(work, claimed)
            for path in outputs:
                handle.add_output(path)
            claimed |= set(outputs)
    if journal.dry_run:
        return False
    for name in STEPS[:-1]:
        journal.retire(name)
    shutil.rmtree(work, ignore_errors=True)
    return True
