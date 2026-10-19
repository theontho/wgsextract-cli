import argparse
import logging
import os
import shlex
import subprocess
import sys
import tempfile

from wgsextract_cli.core.alignment_metadata import get_vcf_build
from wgsextract_cli.core.builds import is_hg37_build, is_hg38_build, is_t2t_build
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.reference_assets import input_stem
from wgsextract_cli.core.reference_resolver import ReferenceLibrary
from wgsextract_cli.core.utils import (
    WGSExtractError,
    get_resource_defaults,
    run_command,
)
from wgsextract_cli.core.variant_files import (
    calculate_bam_md5,
    chromosome_aliases,
    ensure_vcf_indexed,
    get_vcf_chr_name,
    verify_paths_exist,
)

VCF_SUFFIXES = (".vcf", ".vcf.gz")
_UNCOMPRESSED_FASTA_SUFFIXES = (".fa", ".fasta", ".fna")


def resolve_lineage_tool(explicit_path: str | None, tool: str, flag: str) -> str:
    """Resolve a lineage tool from its CLI flag/setting or the installed tool."""
    if not explicit_path:
        verify_dependencies([tool])
    path = explicit_path or get_tool_path(tool)
    # A bare command name (no directory part) is resolved like any other tool.
    if path and not os.path.isabs(path) and os.path.basename(path) == path:
        path = get_tool_path(path) or path
    if not path:
        raise WGSExtractError(f"{tool} path missing.")
    verify_paths_exist({flag: path})
    return path


def tool_command(path: str) -> list[str]:
    """Return argv for a tool path; run_command expands `pixi run` wrappers."""
    if path.endswith(".py"):
        return [sys.executable, path]
    return [path]


def extract_vcf_chromosome(input_vcf: str, target_chr: str, out_vcf: str) -> str:
    """Write the records of one chromosome (e.g. MT, Y) of ``input_vcf`` to ``out_vcf``."""
    ensure_vcf_indexed(input_vcf)
    indexed = any(os.path.exists(input_vcf + ext) for ext in (".tbi", ".csi"))
    if indexed:
        chrom = get_vcf_chr_name(input_vcf, target_chr)
        region_args = ["-r", chrom]
    else:
        # A plain .vcf can't be indexed; stream it and accept any naming of the contig.
        chrom = target_chr
        region_args = ["-t", ",".join(chromosome_aliases(target_chr))]
    logging.info(f"Filtering VCF to {chrom}...")
    run_command(["bcftools", "view", *region_args, "-Oz", "-o", out_vcf, input_vcf])
    records = run_command(["bcftools", "view", "-H", out_vcf], capture_output=True)
    if not records.stdout.strip():
        raise WGSExtractError(f"{input_vcf} has no variants on chromosome {chrom}.")
    return chrom


def _yleaf_build(build: str | None) -> str:
    if build and is_hg38_build(build):
        return "hg38"
    if build and is_hg37_build(build):
        return "hg19"
    if build and is_t2t_build(build):
        return "t2t"
    logging.warning(f"Build {build} not supported by Yleaf, defaulting to hg38")
    return "hg38"


def _yleaf_supports_ref_fasta(cmd: list[str]) -> bool:
    try:
        result = run_command(cmd + ["--help"], capture_output=True, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        logging.debug("Could not inspect Yleaf --help for --ref-fasta support: %s", e)
        return False
    return "--ref-fasta" in f"{result.stdout}\n{result.stderr}"


def _placeholder_yleaf_data_dir(work_dir: str) -> dict[str, str]:
    """
    Yleaf checks for (and otherwise downloads, ~3 GB) a full reference genome
    before every run, but only FASTQ alignment reads it; BAM/CRAM/VCF runs use
    the bundled marker tables plus -cr for CRAM decoding. Point YLEAF_DATA_DIR
    at a placeholder so those runs never trigger the download.
    """
    data_dir = os.path.join(work_dir, "yleaf_data")
    for build in ("hg19", "hg38", "t2t"):
        build_dir = os.path.join(data_dir, build)
        os.makedirs(build_dir, exist_ok=True)
        with open(os.path.join(build_dir, "full_reference.fa"), "w") as f_out:
            f_out.write(">placeholder\n" + "N" * 120 + "\n")
    return {**os.environ, "YLEAF_DATA_DIR": data_dir}


def cmd_ydna(args: argparse.Namespace) -> None:
    if not args.input:
        raise WGSExtractError(LOG_MESSAGES["input_required"])
    verify_paths_exist({"--input": args.input})

    # Check dependencies only after cheap input validation so invalid invocations fail fast.
    yleaf_path = resolve_lineage_tool(args.yleaf_path, "yleaf", "--yleaf-path")
    input_lower = args.input.lower()
    is_vcf = input_lower.endswith(VCF_SUFFIXES)
    if is_vcf:
        verify_dependencies(["bcftools", "tabix"])
    log_dependency_info(["yleaf", "bcftools"])

    outdir = os.path.abspath(
        args.outdir or os.path.dirname(os.path.abspath(args.input))
    )
    # Yleaf deletes and recreates its -o folder, so never hand it the user's
    # output directory itself.
    yleaf_outdir = os.path.join(outdir, "yleaf")
    logging.debug(f"Input file: {os.path.abspath(args.input)}")
    logging.debug(f"Output directory: {yleaf_outdir}")

    lib = None
    if not is_vcf:
        lib = ReferenceLibrary(args.ref, calculate_bam_md5(args.input, None))
    build = get_vcf_build(args.input) if is_vcf else None
    if not build:
        lib = lib or ReferenceLibrary(args.ref, None)
        build = lib.build
    yleaf_build = _yleaf_build(build)

    cmd = tool_command(yleaf_path)
    threads, _ = get_resource_defaults(args.threads, None)
    os.makedirs(outdir, exist_ok=True)

    logging.info(LOG_MESSAGES["running_yleaf"].format(input=args.input))
    with tempfile.TemporaryDirectory(prefix="yleaf_", dir=outdir) as work_dir:
        input_file = args.input
        command_env = None
        extra: list[str] = []
        if is_vcf:
            input_flag = "-vcf"
            # Yleaf rejects VCFs with several Y-like contigs, so pass chrY only.
            input_file = os.path.join(work_dir, f"{input_stem(args.input)}.vcf.gz")
            extract_vcf_chromosome(args.input, "Y", input_file)
        elif input_lower.endswith(".bam"):
            input_flag = "-bam"
        elif input_lower.endswith(".cram"):
            input_flag = "-cram"
            cram_ref = (lib.fasta if lib else None) or (
                args.ref if args.ref and os.path.isfile(args.ref) else None
            )
            if not cram_ref:
                raise WGSExtractError(
                    LOG_MESSAGES["ref_required_for"].format(task="Yleaf on CRAM")
                )
            extra += ["-cr", cram_ref]
        elif input_lower.endswith((".fastq", ".fastq.gz", ".fq", ".fq.gz")):
            input_flag = "-fastq"
        else:
            raise WGSExtractError(
                f"Unsupported Yleaf input type (expected BAM, CRAM, VCF or FASTQ): {args.input}"
            )

        if input_flag == "-fastq":
            # FASTQ is aligned by Yleaf itself, which needs the real reference.
            ref_fasta = lib.fasta if lib else None
            if (
                ref_fasta
                and ref_fasta.endswith(_UNCOMPRESSED_FASTA_SUFFIXES)
                and _yleaf_supports_ref_fasta(cmd)
            ):
                extra += ["--ref-fasta", ref_fasta]
            else:
                logging.warning(
                    "No uncompressed reference FASTA usable by this Yleaf; it will "
                    "use its own configured (or downloaded) reference."
                )
        else:
            command_env = _placeholder_yleaf_data_dir(work_dir)

        if args.pos_file:
            extra += ["-pos", args.pos_file]
        if args.extra_args:
            extra += [a for a in shlex.split(args.extra_args) if a != "-force"]

        final_cmd = cmd + [
            input_flag,
            input_file,
            "-rg",
            yleaf_build,
            "-o",
            yleaf_outdir,
            "-force",
            "-t",
            str(threads),
            *extra,
        ]
        try:
            run_command(final_cmd, env=command_env)
        except (OSError, subprocess.SubprocessError) as e:
            raise WGSExtractError(f"Yleaf failed: {e}") from e

    prediction = os.path.join(yleaf_outdir, "hg_prediction.hg")
    try:
        with open(prediction) as f:
            content = f.read().strip()
    except OSError as e:
        raise WGSExtractError(f"Yleaf did not write {prediction}: {e}") from e
    # Header line plus one row per sample; a header alone means no markers matched.
    if len(content.splitlines()) < 2:
        raise WGSExtractError(
            "Yleaf could not predict a Y haplogroup: no Y-chromosome markers "
            "matched. Check that the input has chrY reads and that its reference "
            f"build is correct; details are in {os.path.join(yleaf_outdir, 'run.log')}."
        )
    print(f"\n🧬 Yleaf Lineage Result ({input_stem(args.input)}):")
    print("-" * 30)
    print(content)
    print("-" * 30 + "\n")
