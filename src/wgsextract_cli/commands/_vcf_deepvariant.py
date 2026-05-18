import logging
import os
import shutil
import subprocess

from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.utils import (
    WGSExtractError,
    run_command,
)
from wgsextract_cli.core.variant_files import ensure_vcf_indexed

from ._vcf_basic import (
    get_base_args,
)


def cmd_deepvariant(args):
    import shlex

    # DeepVariant can be run via:
    # 1. Official 'run_deepvariant' wrapper
    # 2. Bioconda 'dv_make_examples.py' + 'dv_call_variants.py' + 'dv_postprocess_variants.py'

    executable = shutil.which("run_deepvariant")
    use_bioconda = False

    if not executable:
        executable = shutil.which("dv_make_examples.py")
        if executable:
            use_bioconda = True
            logging.info("Found Bioconda DeepVariant scripts.")
        else:
            logging.error(
                "DeepVariant not found. Please install it or ensure it is in your PATH."
            )
            raise WGSExtractError(
                "DeepVariant not found. Please install it or add it to PATH."
            )

    base = get_base_args(args)
    if not base:
        raise WGSExtractError("Failed to resolve base arguments for DeepVariant.")
    threads, outdir, ref, lib = base

    out_vcf = os.path.join(outdir, "deepvariant.vcf.gz")
    intermediate_vcf = os.path.join(outdir, "deepvariant.vcf")

    logging.info(LOG_MESSAGES["vcf_calling_deepvariant"].format(output=out_vcf))

    if getattr(args, "pacbio", False):
        model_type = "PACBIO"
    elif getattr(args, "model_type", None):
        model_type = args.model_type
    else:
        model_type = "WGS" if not args.wes else "WES"
    region_args = ["--regions", args.region] if args.region else []

    try:
        if use_bioconda:
            # 0. Prepare clean environment for conda run
            clean_env = os.environ.copy()
            dv_bin_dir = os.path.dirname(executable)
            # Remove virtualenv stuff that interferes with conda internal python
            clean_env.pop("VIRTUAL_ENV", None)
            clean_env.pop("PYTHONPATH", None)
            # Put Bioconda at the front
            clean_env["PATH"] = dv_bin_dir + os.pathsep + clean_env.get("PATH", "")

            # Multi-step pipeline for Bioconda
            examples = os.path.join(outdir, "dv_examples.tfrecord.gz")
            call_vcf = os.path.join(outdir, "dv_calls.tfrecord.gz")
            log_dir = os.path.join(outdir, "dv_logs")
            os.makedirs(log_dir, exist_ok=True)

            # Get sample name from BAM
            sample_name = "sample"
            try:
                res = run_command(
                    ["samtools", "view", "-H", args.input],
                    capture_output=True,
                    env=clean_env,
                )
                for line in res.stdout.splitlines():
                    if line.startswith("@RG"):
                        for part in line.split("\t"):
                            if part.startswith("SM:"):
                                sample_name = part[3:]
                                break
            except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
                logging.debug(f"Failed to infer DeepVariant sample name: {e}")

            # 1. Make Examples
            logging.info("DeepVariant Step 1/3: Making examples...")
            make_cmd_inner = [
                "dv_make_examples.py",
                "--cores",
                str(threads),
                "--ref",
                ref,
                "--reads",
                args.input,
                "--sample",
                sample_name,
                "--examples",
                examples,
                "--logdir",
                log_dir,
            ] + region_args

            run_command(
                [
                    "conda",
                    "run",
                    "-n",
                    "wgse",
                    "--no-capture-output",
                    "bash",
                    "-c",
                    f"{' '.join(map(shlex.quote, make_cmd_inner))} < /dev/null",
                ],
                env=clean_env,
                check=True,
            )
            # 2. Call Variants
            logging.info("DeepVariant Step 2/3: Calling variants...")
            call_cmd_inner = [
                "dv_call_variants.py",
                "--cores",
                str(threads),
                "--examples",
                examples,
                "--outfile",
                call_vcf,
                "--sample",
                sample_name,
                "--model",
                model_type.lower(),
            ]
            if args.checkpoint:
                call_cmd_inner.extend(["--checkpoint", args.checkpoint])

            run_command(
                [
                    "conda",
                    "run",
                    "-n",
                    "wgse",
                    "--no-capture-output",
                    "bash",
                    "-c",
                    f"{' '.join(map(shlex.quote, call_cmd_inner))} < /dev/null",
                ],
                env=clean_env,
                check=True,
            )
            # 3. Postprocess
            logging.info("DeepVariant Step 3/3: Postprocessing...")
            post_cmd_inner = [
                "dv_postprocess_variants.py",
                "--ref",
                ref,
                "--infile",
                call_vcf,
                "--outfile",
                intermediate_vcf,
            ]
            run_command(
                [
                    "conda",
                    "run",
                    "-n",
                    "wgse",
                    "--no-capture-output",
                    "bash",
                    "-c",
                    f"{' '.join(map(shlex.quote, post_cmd_inner))} < /dev/null",
                ],
                env=clean_env,
                check=True,
            )
            # Cleanup intermediate tfrecords
            for f in os.listdir(outdir):
                if f.startswith("dv_examples.tfrecord") or f == "dv_calls.tfrecord.gz":
                    try:
                        os.remove(os.path.join(outdir, f))
                    except Exception:
                        pass
        else:
            # Single wrapper
            cmd = [
                "run_deepvariant",
                "--model_type",
                model_type,
                "--ref",
                ref,
                "--reads",
                args.input,
                "--output_vcf",
                intermediate_vcf,
                "--num_shards",
                str(threads),
            ] + region_args
            run_command(cmd, check=True)

        # DeepVariant outputs plain VCF, we compress it
        if os.path.exists(intermediate_vcf):
            run_command(["bgzip", "-f", intermediate_vcf], check=True)
            ensure_vcf_indexed(out_vcf)
        else:
            raise WGSExtractError("DeepVariant failed to produce output VCF.")

    except Exception as e:
        logging.error(f"DeepVariant failed: {e}")
        raise WGSExtractError("VCF processing failed.") from e
