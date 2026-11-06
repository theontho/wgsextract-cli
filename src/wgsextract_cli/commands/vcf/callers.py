import ctypes
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from wgsextract_cli.api._cli import cli_handler
from wgsextract_cli.api.vcf import VcfFreebayesParams, VcfGatkParams
from wgsextract_cli.core import ultima
from wgsextract_cli.core.atomic_output import safe_output
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.dependency_checks import verify_dependencies
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.native_support import require_native_tool
from wgsextract_cli.core.output_checks import warn_if_no_variants
from wgsextract_cli.core.paths import java_local_path
from wgsextract_cli.core.reference_assets import read_fai_lengths, reference_dict_path
from wgsextract_cli.core.region_selection import region_items
from wgsextract_cli.core.resource_estimates import (
    estimate_resources,
    preflight_resources,
)
from wgsextract_cli.core.temp_directory import configured_temp_directory
from wgsextract_cli.core.utils import (
    UsageError,
    WGSExtractError,
    run_command,
    run_pipeline,
)
from wgsextract_cli.core.variant_files import ensure_vcf_indexed

from .basic import plain_reference, record_variants, setup_caller
from .gatk_scatter import call_scattered, scatter_intervals


def _prepare_gatk_input(
    input_file: str, region: str | None, ref: str, outdir: str
) -> tuple[str, str | None]:
    """Return an input GATK can read, converting regional CRAM input when needed."""
    if not input_file.lower().endswith(".cram"):
        return input_file, None

    if not region:
        logging.warning(
            "GATK may not support CRAM 3.1 directly. For whole-genome CRAM input, "
            "convert to BAM first with 'wgsextract bam to-bam' or pass --region for "
            "automatic regional conversion."
        )
        return input_file, None

    fd, temp_bam = tempfile.mkstemp(
        suffix=".bam", dir=configured_temp_directory() or outdir
    )
    os.close(fd)
    logging.info(f"Preparing regional BAM for GATK from CRAM input ({region})...")
    try:
        samtools = get_tool_path("samtools")
        regions = region_items(region, commas=True)
        run_command(
            [
                samtools,
                "view",
                "-bh",
                *(["-M"] if len(regions) > 1 else []),
                "-T",
                ref,
                "-o",
                temp_bam,
                "--",  # a region can't be read as another option
                input_file,
                *regions,
            ],
            check=True,
        )
        run_command([samtools, "index", temp_bam], check=True)
    except (OSError, subprocess.SubprocessError, WGSExtractError):
        _cleanup_gatk_temp_input(temp_bam)
        raise
    return temp_bam, temp_bam


def _gatk_region_args(region: str | None, outdir: str) -> tuple[list[str], str | None]:
    """-L through an interval-list file: on Windows GATK parses a bare `20:1-200`
    as a file path and fails on the ':'; a .intervals file reads the same everywhere."""
    if not region:
        return [], None
    # A new file, never one a link at a fixed name points to.
    fd, intervals = tempfile.mkstemp(
        dir=outdir, prefix=".gatk_region_", suffix=".intervals"
    )
    # One interval per line: a comma-separated --region is several.
    lines = "".join(f"{r}\n" for r in region_items(region, commas=True))
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(lines)
    return ["-L", java_local_path(intervals)], intervals


def _gatk_tmp_dir() -> str:
    """A temp dir for GATK without spaces. GATK 4.6 can't load its codecs'
    bundled config from a temp path with a space ("Unable to automatically
    instantiate codec"), and on Windows %TEMP% is under the user's profile, so
    any user whose name has a space would hit it; use the 8.3 short path there."""
    path = tempfile.mkdtemp(prefix="wgse-gatk-", dir=configured_temp_directory())
    if " " in path and sys.platform == "win32":
        buffer = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.kernel32.GetShortPathNameW(path, buffer, len(buffer)):
            path = buffer.value
    if " " in path:
        logging.warning(
            f"GATK's temp directory has a space ({path}); GATK may fail to start."
        )
    return path


def _cleanup_gatk_temp_input(temp_input: str | None) -> None:
    if not temp_input:
        return
    for path in (temp_input, temp_input + ".bai", temp_input + ".csi"):
        try:
            Path(path).unlink(missing_ok=True)
        except OSError as e:
            logging.warning(f"Failed to remove temporary GATK artifact {path}: {e}")


def write_region_bed(region: str, lengths: dict[str, int], bed: str) -> None:
    """chr1:1000-1050,chr2 (1-based, inclusive) as BED (0-based, half-open)."""
    with open(bed, "w", newline="\n") as f:
        for part in (r.strip() for r in region.split(",") if r.strip()):
            contig, _, span = part.partition(":")
            start, _, end = span.partition("-")
            if not end and contig not in lengths:
                raise WGSExtractError(f"{contig} isn't in the reference.")
            first = int(start) - 1 if start else 0
            last = int(end) if end else lengths[contig]
            f.write(f"{contig}\t{first}\t{last}\n")


def run_freebayes(params: VcfFreebayesParams) -> None:
    """``vcf freebayes``: FreeBayes into ``freebayes.vcf.gz``."""
    verify_dependencies(["freebayes", "bcftools", "tabix", "samtools"])
    setup = setup_caller(params)
    outdir, ref = setup.outdir, setup.ref
    input_file, region = str(params.input), params.region
    if ultima.alignment_is_ultima(input_file):
        ultima.warn_no_flow_model("FreeBayes")

    out_vcf = str(Path(outdir, "freebayes.vcf.gz"))
    logging.info(LOG_MESSAGES["vcf_calling_freebayes"].format(output=out_vcf))

    # FreeBayes needs an uncompressed reference.
    with (
        plain_reference(ref, outdir) as use_ref,
        tempfile.TemporaryDirectory(dir=outdir, prefix=".wgse_fb_") as work,
    ):
        # Regions as a BED: freebayes reads -r as 0-based (so chr1:1000-1050
        # lost position 1000).
        bed = None
        if region:
            bed = str(Path(work, "regions.bed"))
            write_region_bed(region, read_fai_lengths(use_ref), bed)
        if input_file.lower().endswith(".cram"):
            # Decode CRAM with samtools and stream uncompressed BAM to freebayes.
            # It can't seek in a stream, so its calls are cut to the region
            # afterwards: the streamed reads overlap the region's edges.
            view_cmd = ["samtools", "view", "-u", "-h", "-T", use_ref, "--", input_file]
            if region:
                view_cmd += region_items(region, commas=True)
            stages = [view_cmd, ["freebayes", "-f", use_ref, "--stdin"]]
            keep = ["-T", bed] if bed else []
        else:
            targets = ["--targets", bed] if bed else []
            stages = [["freebayes", "-f", use_ref, *targets, input_file]]
            keep = []
        with safe_output(out_vcf) as staged:
            stages.append(["bcftools", "view", *keep, "-Oz", "-o", staged])
            run_pipeline(stages, description="FreeBayes variant calling")
    ensure_vcf_indexed(out_vcf)
    record_variants(out_vcf, "variants", warn_if_no_variants(out_vcf, ref, region))


def _gatk_gpu(params: VcfGatkParams) -> None:
    """HaplotypeCaller on an NVIDIA GPU (Parabricks), which reads CRAM itself."""
    from wgsextract_cli.core.parabricks import call_variants, verify_parabricks

    verify_dependencies(["samtools", "tabix"])
    verify_parabricks()
    setup = setup_caller(params)
    outdir, ref = setup.outdir, setup.ref
    input_file, region = str(params.input), params.region
    if ultima.alignment_is_ultima(input_file):
        ultima.warn_no_flow_model("Parabricks HaplotypeCaller (--gpu)")
    out_vcf = str(Path(outdir, "gatk.vcf.gz"))
    logging.info(LOG_MESSAGES["vcf_calling_gatk"].format(output=out_vcf))
    with safe_output(out_vcf) as staged:
        call_variants("haplotypecaller", input_file, ref, staged, region)
    ensure_vcf_indexed(out_vcf)
    record_variants(out_vcf, "variants", warn_if_no_variants(out_vcf, ref, region))


def run_gatk(params: VcfGatkParams) -> None:
    """``vcf gatk``: HaplotypeCaller into ``gatk.vcf.gz``, or Parabricks."""
    require_native_tool("gatk")
    scatter = params.scatter
    if params.gpu:
        if scatter:
            raise UsageError("GATK --scatter cannot be combined with --gpu.")
        _gatk_gpu(params)
        return
    verify_dependencies(
        ["gatk", "samtools", *(["bcftools", "tabix"] if scatter else [])]
    )
    if sys.platform == "win32":
        logging.warning(
            "Native Windows GATK uses AVX OpenMP PairHMM and AVX2 Smith-Waterman; "
            "compression remains Java-based. Runtime and numeric annotations may differ "
            "from Linux; check GATK's kernel-selection logs."
        )
    setup = setup_caller(params)
    _threads, outdir, ref, lib = setup.threads, setup.outdir, setup.ref, setup.lib
    input_file, region = str(params.input), params.region
    shards = scatter_intervals(ref, region, _threads) if scatter else []
    preflight_resources(
        lambda: estimate_resources(
            "gatk",
            input_paths=[input_file],
            output_dir=outdir,
            reference=ref,
            threads=_threads,
            region=region,
            temp_dir=configured_temp_directory() or tempfile.gettempdir(),
            gatk_staging_dir=configured_temp_directory(),
            gatk_workers=len(shards) or 1,
        )
    )
    out_vcf = str(Path(outdir, "gatk.vcf.gz"))
    # Ultima's own pipeline calls with the flow-based likelihood model.
    flow_args = []
    if ultima.alignment_flow_data(input_file, ref).flow_aware:
        logging.info(
            "Ultima reads: calling in GATK's flow mode (--flow-mode ADVANCED)."
        )
        flow_args = ["--flow-mode", "ADVANCED"]
    elif scatter:
        raise WGSExtractError("GATK --scatter requires flow-aware Ultima input.")
    if len(shards) == 1:
        logging.info("GATK scattering fits one worker; using the unscattered path.")
        shards = []

    # GATK requires a .dict file
    if not lib.dict_file:
        logging.info(LOG_MESSAGES["vcf_generating_dict"])
        dict_file = java_local_path(reference_dict_path(ref))
        try:
            run_command(
                ["samtools", "dict", "-o", dict_file, java_local_path(ref)],
                check=True,
            )
            lib.dict_file = dict_file
        except (OSError, subprocess.SubprocessError) as e:
            raise WGSExtractError(f"Failed to generate GATK dict file: {e}") from e

    logging.info(LOG_MESSAGES["vcf_calling_gatk"].format(output=out_vcf))
    region_args, region_file = _gatk_region_args(region, outdir)
    gatk_input = input_file
    temp_gatk_input = None
    gatk_tmp = _gatk_tmp_dir()

    try:
        gatk_input, temp_gatk_input = _prepare_gatk_input(
            input_file, region, ref, outdir
        )
        if shards:
            with tempfile.TemporaryDirectory(
                dir=configured_temp_directory() or outdir, prefix=".wgse_gatk_scatter_"
            ) as work:
                with safe_output(out_vcf) as staged:
                    call_scattered(
                        [
                            "gatk",
                            "HaplotypeCaller",
                            "-R",
                            java_local_path(ref),
                            "-I",
                            java_local_path(gatk_input),
                            *flow_args,
                        ],
                        shards,
                        staged,
                        Path(work),
                        gatk_tmp,
                    )
            ensure_vcf_indexed(out_vcf)
            record_variants(
                out_vcf, "variants", warn_if_no_variants(out_vcf, ref, region)
            )
            return
        cmd = (
            [
                "gatk",
                "HaplotypeCaller",
                "-R",
                java_local_path(ref),
                "-I",
                java_local_path(gatk_input),
                "-O",
                java_local_path(out_vcf),
                "--tmp-dir",
                java_local_path(gatk_tmp),
            ]
            + flow_args
            + region_args
        )
        with safe_output(out_vcf) as staged:
            cmd[cmd.index("-O") + 1] = java_local_path(staged)
            run_command(cmd)
        ensure_vcf_indexed(out_vcf)
        record_variants(out_vcf, "variants", warn_if_no_variants(out_vcf, ref, region))
    except subprocess.CalledProcessError as e:
        logging.error(f"GATK failed: {e}")
        if input_file.lower().endswith(".cram"):
            logging.error(
                "Hint: Older GATK versions do not support CRAM version 3.1. "
                "If this failed with a CRAM error, please convert to BAM or upgrade GATK."
            )
        raise WGSExtractError("GATK failed.") from e
    finally:
        _cleanup_gatk_temp_input(temp_gatk_input)
        if region_file:
            Path(region_file).unlink(missing_ok=True)
        shutil.rmtree(gatk_tmp, ignore_errors=True)


cmd_freebayes = cli_handler(VcfFreebayesParams, run_freebayes)
cmd_gatk = cli_handler(VcfGatkParams, run_gatk)
