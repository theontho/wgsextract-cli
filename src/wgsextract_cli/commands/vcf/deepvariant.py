import argparse
import logging
import os
import subprocess
import tempfile

from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.dependency_checks import verify_dependencies
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.utils import WGSExtractError, run_command
from wgsextract_cli.core.variant_files import ensure_vcf_indexed

from .basic import get_base_args

# Bioconda ships wrapper scripts instead of Google's run_deepvariant.
BIOCONDA_SCRIPTS = (
    "dv_make_examples.py",
    "dv_call_variants.py",
    "dv_postprocess_variants.py",
)
_BIOCONDA_MODELS = {
    "WGS": "wgs",
    "WES": "wes",
    "PACBIO": "pacbio",
    "HYBRID_PACBIO_ILLUMINA": "hybrid",
}


def _model_type(args: argparse.Namespace) -> str:
    if getattr(args, "pacbio", False):
        return "PACBIO"
    if getattr(args, "model_type", None):
        return str(args.model_type)
    return "WES" if getattr(args, "wes", False) else "WGS"


def _sample_name(input_path: str, ref: str) -> str:
    cram_args = ["-T", ref] if input_path.lower().endswith(".cram") else []
    try:
        res = run_command(
            ["samtools", "view", "-H", *cram_args, input_path], capture_output=True
        )
    except (OSError, subprocess.SubprocessError) as e:
        logging.debug(f"Failed to infer DeepVariant sample name: {e}")
        return "sample"
    for line in res.stdout.splitlines():
        if line.startswith("@RG"):
            for field in line.split("\t"):
                if field.startswith("SM:") and field[3:]:
                    return field[3:]
    return "sample"


def _dv_env() -> dict[str, str]:
    # DeepVariant runs its own Python; don't leak ours into it.
    env = dict(os.environ)
    for var in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
        env.pop(var, None)
    return env


def _run_dv(cmd: list[str]) -> None:
    # stdin is closed: GNU parallel (used by dv_make_examples.py) reads it otherwise.
    run_command(cmd, env=_dv_env(), stdin=subprocess.DEVNULL)


def cmd_deepvariant(args: argparse.Namespace) -> None:
    run_deepvariant = get_tool_path("run_deepvariant")
    if run_deepvariant:
        dv_tools = ["run_deepvariant"]
    elif all(get_tool_path(script) for script in BIOCONDA_SCRIPTS):
        dv_tools = list(BIOCONDA_SCRIPTS)
        logging.info("Using Bioconda DeepVariant wrapper scripts.")
    else:
        raise WGSExtractError(
            "DeepVariant not found. Install it with 'pixi install -e deepvariant' "
            "(linux-64/osx-arm64) or put run_deepvariant on PATH."
        )
    if not run_deepvariant and args.checkpoint:
        raise WGSExtractError(
            "--checkpoint requires Google's run_deepvariant; the Bioconda wrapper "
            "scripts only support the bundled models."
        )
    verify_dependencies(["samtools", "tabix", *dv_tools])

    threads, outdir, ref, _lib = get_base_args(args)
    out_vcf = os.path.join(outdir, "deepvariant.vcf.gz")
    logging.info(LOG_MESSAGES["vcf_calling_deepvariant"].format(output=out_vcf))
    model_type = _model_type(args)
    region_args = ["--regions", args.region] if args.region else []

    try:
        with tempfile.TemporaryDirectory(dir=outdir, prefix=".wgse_dv_") as work:
            if run_deepvariant:
                cmd = [
                    run_deepvariant,
                    "--model_type",
                    model_type,
                    "--ref",
                    ref,
                    "--reads",
                    args.input,
                    "--output_vcf",
                    out_vcf,
                    "--num_shards",
                    str(threads),
                    "--intermediate_results_dir",
                    work,
                    *region_args,
                ]
                if args.checkpoint:
                    cmd += ["--customized_model", args.checkpoint]
                _run_dv(cmd)
            else:
                _run_bioconda_pipeline(
                    args, ref, threads, model_type, region_args, work, out_vcf
                )
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(f"DeepVariant failed: {e}") from e

    if not os.path.exists(out_vcf):
        raise WGSExtractError("DeepVariant failed to produce output VCF.")
    ensure_vcf_indexed(out_vcf)


def _run_bioconda_pipeline(
    args: argparse.Namespace,
    ref: str,
    threads: str,
    model_type: str,
    region_args: list[str],
    work: str,
    out_vcf: str,
) -> None:
    make_examples, call_variants, postprocess = (
        str(get_tool_path(script)) for script in BIOCONDA_SCRIPTS
    )
    sample = _sample_name(args.input, ref)
    examples_dir = os.path.join(work, "examples")
    log_dir = os.path.join(work, "logs")
    calls = os.path.join(work, "calls.tfrecord.gz")
    os.makedirs(examples_dir)
    os.makedirs(log_dir)

    logging.info("DeepVariant Step 1/3: Making examples...")
    _run_dv(
        [
            make_examples,
            "--cores",
            threads,
            "--ref",
            ref,
            "--reads",
            args.input,
            "--sample",
            sample,
            "--examples",
            examples_dir,
            "--logdir",
            log_dir,
            *region_args,
        ]
    )
    logging.info("DeepVariant Step 2/3: Calling variants...")
    _run_dv(
        [
            call_variants,
            "--cores",
            threads,
            "--examples",
            examples_dir,
            "--outfile",
            calls,
            "--sample",
            sample,
            "--model",
            _BIOCONDA_MODELS[model_type],
        ]
    )
    logging.info("DeepVariant Step 3/3: Postprocessing...")
    _run_dv([postprocess, "--ref", ref, "--infile", calls, "--outfile", out_vcf])
