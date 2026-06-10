import argparse
import gzip
import logging
import os
import shlex
import subprocess
import tempfile

from wgsextract_cli.core.alignment_metadata import get_vcf_build
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.reference_resolver import ReferenceLibrary
from wgsextract_cli.core.regions import get_vcf_chr_name
from wgsextract_cli.core.utils import (
    WGSExtractError,
    get_resource_defaults,
    run_command,
)
from wgsextract_cli.core.variant_files import (
    calculate_bam_md5,
    verify_paths_exist,
)


def _resolve_yleaf_reference_fasta(ref_path: str | None, build: str) -> str | None:
    if not ref_path:
        return None

    if os.path.isfile(ref_path):
        return ref_path

    lib = ReferenceLibrary(ref_path)
    for filename in os.listdir(ref_path):
        filename_upper = filename.upper()
        if build.upper() in filename_upper and filename.endswith(
            (".fa", ".fasta", ".fna", ".fa.gz", ".fasta.gz", ".fna.gz")
        ):
            return os.path.join(ref_path, filename)

    return lib.fasta


def _yleaf_supports_ref_fasta(cmd: list[str]) -> bool:
    try:
        result = subprocess.run(
            cmd + ["--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as e:
        logging.debug("Could not inspect Yleaf --help for --ref-fasta support: %s", e)
        return False

    return "--ref-fasta" in f"{result.stdout}\n{result.stderr}"


def _prepare_yleaf_vcf_data_dir(
    temp_dir: tempfile.TemporaryDirectory[str], ref_fasta: str | None, build: str
) -> dict[str, str]:
    data_dir = os.path.join(temp_dir.name, "yleaf_data")
    build_dir = os.path.join(data_dir, build)
    os.makedirs(build_dir, exist_ok=True)
    full_reference = os.path.join(build_dir, "full_reference.fa")

    if ref_fasta and ref_fasta.endswith((".fa", ".fasta", ".fna")):
        try:
            os.symlink(os.path.abspath(ref_fasta), full_reference)
        except OSError:
            with open(ref_fasta, "rb") as f_in, open(full_reference, "wb") as f_out:
                f_out.write(f_in.read())
    else:
        with open(full_reference, "w", encoding="utf-8") as f_out:
            f_out.write(">chrY\n")
            f_out.write("N" * 120 + "\n")

    env = os.environ.copy()
    env["YLEAF_DATA_DIR"] = data_dir
    return env


def update_yleaf_config(
    yleaf_path: str | None, ref_path: str | None, build: str
) -> None:
    """
    Attempts to update yleaf's config.txt to point to the local reference library.
    """
    try:
        if not yleaf_path or not ref_path:
            return

        # 1. Identify where config.txt lives.
        # It usually lives in the same folder as Yleaf.py in site-packages
        # or relative to the executable if it's a wrapper.
        yleaf_dir = None
        if os.path.isfile(yleaf_path):
            bin_dir = os.path.dirname(yleaf_path)
            if os.path.basename(bin_dir).lower() == "bin":
                env_root = os.path.dirname(bin_dir)
                # Walk to find yleaf/config.txt
                for d, _, files in os.walk(env_root):
                    if "config.txt" in files and "yleaf" in d:
                        yleaf_dir = d
                        break
            else:
                yleaf_dir = os.path.dirname(yleaf_path)

        if not yleaf_dir:
            return

        config_path = os.path.join(yleaf_dir, "config.txt")
        if not os.path.exists(config_path):
            # Try one level up if in a sub-package
            config_path = os.path.join(os.path.dirname(yleaf_dir), "config.txt")
            if not os.path.exists(config_path):
                return

        # 2. Resolve the actual FASTA file from the reference library
        fasta_path = _resolve_yleaf_reference_fasta(ref_path, build)
        if not fasta_path:
            return

        # 3. Read and update the config
        with open(config_path) as f:
            lines = f.readlines()

        new_lines = []
        for line in lines:
            if build == "hg19" and line.startswith("full hg19 genome fasta location"):
                new_lines.append(f"full hg19 genome fasta location = {fasta_path}\n")
            elif build == "hg38" and line.startswith("full hg38 genome fasta location"):
                new_lines.append(f"full hg38 genome fasta location = {fasta_path}\n")
            else:
                new_lines.append(line)

        with open(config_path, "w") as f:
            f.writelines(new_lines)

        logging.info(f"Updated yleaf config.txt to use reference: {fasta_path}")

    except OSError as e:
        logging.debug(f"Failed to update yleaf config: {e}")


def cmd_ydna(args: argparse.Namespace) -> None:
    if not args.input:
        raise WGSExtractError(LOG_MESSAGES["input_required"])
    if not verify_paths_exist({"--input": args.input}):
        return

    # Check dependencies only after cheap input validation so invalid invocations fail fast.
    if not args.yleaf_path:
        verify_dependencies(["yleaf"])
    log_dependency_info(["yleaf"])

    yleaf_path = args.yleaf_path or get_tool_path("yleaf")

    # If it's just a tool name, resolve it to a full path or pixi command first
    # so that verify_paths_exist doesn't fail on it.
    if yleaf_path and not os.path.isabs(yleaf_path) and "/" not in yleaf_path:
        resolved = get_tool_path(yleaf_path)
        if resolved:
            yleaf_path = resolved

    if not yleaf_path or not verify_paths_exist({"--yleaf-path": yleaf_path}):
        raise WGSExtractError("Yleaf path missing.")

    logging.debug(f"Input file: {os.path.abspath(args.input)}")
    logging.debug(f"Output directory: {os.path.abspath(args.outdir)}")

    # Build detection for -rg
    build = None
    if args.input.lower().endswith((".vcf", ".vcf.gz")):
        build = get_vcf_build(args.input)

    if not build:
        md5_sig = calculate_bam_md5(args.input, None)
        lib = ReferenceLibrary(args.ref, md5_sig)
        build = lib.build or "hg38"

    if build not in ["hg19", "hg38"]:
        logging.warning(f"Build {build} not supported by Yleaf, defaulting to hg38")
        build = "hg38"

    logging.info(LOG_MESSAGES["running_yleaf"].format(input=args.input))
    temp_dir = None
    temp_vcf = None
    try:
        # Check if yleaf_path is a python script or a wrapper
        cmd = shlex.split(yleaf_path)
        if yleaf_path.endswith(".py"):
            cmd = ["python3", yleaf_path]

        ref_fasta = (
            _resolve_yleaf_reference_fasta(args.ref, build) if args.ref else None
        )
        yleaf_accepts_ref_fasta = _yleaf_supports_ref_fasta(cmd)
        if args.ref:
            logging.debug(f"Resolved reference: {ref_fasta or args.ref}")
            if not yleaf_accepts_ref_fasta:
                update_yleaf_config(yleaf_path, args.ref, build)

        # Map input type to flag (Yleaf 3.2.1 style)
        input_ext = args.input.lower()
        input_file = args.input
        if input_ext.endswith(".bam"):
            input_flag = "-bam"
        elif input_ext.endswith(".cram"):
            input_flag = "-cram"
        elif input_ext.endswith((".vcf", ".vcf.gz")):
            input_flag = "-vcf"
            # Filter VCF to main Y chromosome to avoid "Multiple Y-chromosome annotations" error
            chr_y = get_vcf_chr_name(args.input, "Y")
            logging.info(f"Filtering VCF to {chr_y} for Yleaf...")

            # Use a space-free path in /tmp to avoid shell quoting bugs in Yleaf
            temp_dir = tempfile.TemporaryDirectory(prefix="yleaf_")
            temp_vcf = os.path.join(temp_dir.name, "input.vcf.gz")

            bcftools = get_tool_path("bcftools")
            bgzip = get_tool_path("bgzip")

            run_command(
                [bcftools, "view", "-r", chr_y, "-Oz", "-o", temp_vcf, args.input]
            )
            # Force lowercase 'chry' in both header and body using sed for maximum compatibility
            # because some bcftools versions might handle --rename-chrs differently with temp files.
            # We use a temp file for sed output then move it back.
            sed_vcf = temp_vcf + ".sed.gz"

            with (
                gzip.open(temp_vcf, "rt") as f_in,
                open(temp_vcf + ".plain", "w") as f_out,
            ):
                for line in f_in:
                    if line.startswith(f"{chr_y}\t"):
                        f_out.write(line.replace(f"{chr_y}\t", "chry\t", 1))
                    elif line.startswith("##contig=<ID=" + chr_y):
                        f_out.write(line.replace("ID=" + chr_y, "ID=chry", 1))
                    else:
                        f_out.write(line)

            # Recompress with bgzip to ensure it's BGZF
            with open(sed_vcf, "wb") as f_out:
                run_command([bgzip, "-c", temp_vcf + ".plain"], stdout=f_out)
            os.remove(temp_vcf + ".plain")

            os.rename(sed_vcf, temp_vcf)
            run_command([bcftools, "index", "-t", temp_vcf])

            input_file = temp_vcf
            logging.debug(f"Created shell-safe temp_vcf at: {temp_vcf}")
        elif input_ext.endswith((".fastq", ".fastq.gz", ".fq", ".fq.gz")):
            input_flag = "-fastq"
        else:
            # Fallback to -input for older versions
            input_flag = "-input"

        # Use absolute path for outdir to be safe
        outdir_abs = os.path.abspath(args.outdir)
        os.makedirs(outdir_abs, exist_ok=True)

        threads, _ = get_resource_defaults(args.threads, None)
        final_cmd = cmd + [
            input_flag,
            input_file,
            "-rg",
            build,
            "-o",
            outdir_abs,
            "-force",
            "-t",
            str(threads),
        ]

        command_env = None
        if input_flag == "-vcf" and temp_dir is not None:
            command_env = _prepare_yleaf_vcf_data_dir(temp_dir, ref_fasta, build)
        elif ref_fasta and yleaf_accepts_ref_fasta:
            final_cmd.extend(["--ref-fasta", ref_fasta])

        # Add reference for CRAM if available
        if input_flag == "-cram":
            md5_sig = calculate_bam_md5(args.input, None)
            lib = ReferenceLibrary(args.ref, md5_sig)
            if lib.fasta:
                final_cmd.extend(["-cr", lib.fasta])
            elif args.ref and os.path.isfile(args.ref):
                # If --ref is a direct file, use it as the CRAM reference
                final_cmd.extend(["-cr", args.ref])

        # Add -pos if provided (legacy)
        if args.pos_file:
            final_cmd.extend(["-pos", args.pos_file])

        # Add extra args if provided
        if args.extra_args:
            extra = shlex.split(args.extra_args)
            # Remove -force from extra if it's already there to avoid duplicates
            # though Yleaf likely doesn't mind
            final_cmd.extend([a for a in extra if a != "-force"])

        # Execute and WAIT explicitly
        run_command(final_cmd, env=command_env)
        logging.debug("Yleaf execution finished successfully")

        # Print results directly to terminal
        y_out = os.path.join(outdir_abs, "At_level_3.txt")
        if os.path.exists(y_out):
            print("\n🧬 Yleaf Lineage Result (At Level 3):")
            print("-" * 30)
            try:
                with open(y_out) as f:
                    content = f.read().strip()
                    if content:
                        print(content)
                    else:
                        print("Yleaf output is empty (Level 3).")
            except OSError as e:
                logging.debug(f"Failed to print Yleaf results: {e}")
            print("-" * 30 + "\n")

    except WGSExtractError:
        raise
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        raise WGSExtractError(f"Yleaf failed: {e}") from e
    finally:
        if temp_dir is not None:
            try:
                temp_dir.cleanup()
            except OSError as e:
                logging.debug(
                    "Failed to remove temporary Yleaf VCF %s: %s", temp_vcf, e
                )
