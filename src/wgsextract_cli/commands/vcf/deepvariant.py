import argparse
import logging
import os
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
from wgsextract_cli.core.utils import WGSExtractError, run_command
from wgsextract_cli.core.variant_files import ensure_vcf_indexed

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
            dv_args = [
                "--model_type", model_type,
                "--ref", host_path(ref),
                "--reads", host_path(args.input),
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
