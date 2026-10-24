import argparse
import logging
import os
import shlex
import subprocess
import tempfile
import time
from datetime import timedelta
from pathlib import Path

from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.paths import absolute_path, expand_path
from wgsextract_cli.core.reference_resolver import ReferenceLibrary
from wgsextract_cli.core.utils import (
    WGSExtractError,
    get_resource_defaults,
    run_command,
    run_pipeline,
)
from wgsextract_cli.core.variant_files import (
    calculate_bam_md5,
    ensure_vcf_indexed,
)

from ._analyze_workflows import detect_vcf_type
from ._vep_resources import preprocess_vcf_chr_prefix

GENOMIC_SUFFIXES = (".vcf", ".vcf.gz", ".bcf", ".bam", ".cram")
_FORMAT_EXT = {"vcf": ".vcf", "tab": ".txt", "json": ".json"}


# The VEP options of each --preset. 'fast' leaves out what makes VEP slow,
# looking up known variants (population frequencies) and HGVS: about half the
# time of --everything on a genome's chr20.
VEP_PRESETS = {
    "everything": ["--everything"],
    "fast": [
        "--symbol",
        "--canonical",
        "--mane",
        "--biotype",
        "--variant_class",
        "--sift",
        "b",
        "--polyphen",
        "b",
    ],  # fmt: skip
}


def _input_stem(input_path: str) -> str:
    """The file name without its genomic suffix: s.chr1.vcf.gz -> s.chr1."""
    name = Path(input_path).name
    for suffix in sorted(GENOMIC_SUFFIXES, key=len, reverse=True):
        if name.lower().endswith(suffix):
            return name[: -len(suffix)]
    return name


def vep_output_paths(input_path: str, outdir: str, fmt: str) -> tuple[str, str]:
    """Return (output path without .gz, final output path) for an input file."""
    output_file = str(Path(outdir, f"{_input_stem(input_path)}_vep{_FORMAT_EXT[fmt]}"))
    return output_file, output_file + ".gz" if fmt == "vcf" else output_file


def _is_vep_output(path: str) -> bool:
    return _input_stem(path).endswith("_vep")


def _vep_input_files(input_path: str) -> list[str]:
    if not Path(input_path).is_dir():
        if not input_path.lower().endswith(GENOMIC_SUFFIXES):
            raise WGSExtractError(
                f"Unsupported VEP input (expected VCF/BCF/BAM/CRAM): {input_path}"
            )
        return [input_path]
    # A rerun over the directory mustn't annotate the outputs written into it.
    input_files = sorted(
        str(Path(input_path, f))
        for f in os.listdir(input_path)
        if f.lower().endswith(GENOMIC_SUFFIXES) and not _is_vep_output(f)
    )
    if not input_files:
        raise WGSExtractError(f"No valid genomic files found in {input_path}")
    stems: dict[str, str] = {}
    for path in input_files:
        if (other := stems.setdefault(_input_stem(path), path)) != path:
            raise WGSExtractError(
                f"{Path(other).name} and {Path(path).name} would both be annotated "
                f"to {_input_stem(path)}_vep; annotate them separately."
            )
    return input_files


def _apply_forced_assembly(lib: ReferenceLibrary, vep_assembly: str | None) -> None:
    """Make the library agree with an explicit --vep-assembly."""
    if not vep_assembly:
        return
    forced_build = "hg38" if vep_assembly == "GRCh38" else "hg19"
    lib.build = forced_build
    if not lib.fasta:
        return
    f_lower = str(lib.fasta).lower()
    is_hg38_path = "hg38" in f_lower or "grch38" in f_lower
    is_hg19_path = "hg19" in f_lower or "grch37" in f_lower or "hs37d5" in f_lower
    if (forced_build == "hg38" and is_hg19_path) or (
        forced_build == "hg19" and is_hg38_path
    ):
        logging.warning(
            f"Discarding auto-resolved FASTA '{lib.fasta}' because it conflicts "
            f"with --vep-assembly {vep_assembly}"
        )
        lib.fasta = None


def _call_variants_for_vep(
    args: argparse.Namespace,
    bam_path: str,
    ref: str,
    lib: ReferenceLibrary,
    threads: str,
    out_vcf: str,
) -> None:
    region_args = ["-r", args.region] if args.region else []
    if args.ploidy_file:
        ploidy_args = ["--ploidy-file", args.ploidy_file]
    elif args.ploidy:
        ploidy_args = ["--ploidy", args.ploidy]
    elif lib.ploidy_file:
        ploidy_args = ["--ploidy-file", lib.ploidy_file]
    else:
        ploidy_args = ["--ploidy", "GRCh38" if lib.build == "hg38" else "GRCh37"]

    run_pipeline(
        [
            ["bcftools", "mpileup", "-B", "-I", "-C", "50", "-f", ref, "-Ou"]
            + region_args
            + [bam_path],
            ["bcftools", "call", *ploidy_args, "-mv", "-P", "0"]
            + ["--threads", threads, "-Oz", "-o", out_vcf],
        ],
        description="Variant calling for VEP",
    )
    ensure_vcf_indexed(out_vcf)


def _build_vep_command(
    args: argparse.Namespace,
    vep_input_vcf: str,
    output_file: str,
    vcf_type: str,
    lib: ReferenceLibrary,
    threads: str,
) -> list[str]:
    # One element: run_command splits wrapper commands itself and keeps a path
    # with spaces whole.
    vep_cmd = [get_tool_path("vep") or "vep"]
    vep_cmd += ["-i", vep_input_vcf, "-o", output_file, "--fork", threads]
    vep_cmd.append({"vcf": "--vcf", "json": "--json", "tab": "--tab"}[args.format])

    if args.vep_assembly:
        vep_cmd += ["--assembly", args.vep_assembly]
    elif lib.build and "38" in lib.build:
        vep_cmd += ["--assembly", "GRCh38"]
    elif lib.build and ("37" in lib.build or "19" in lib.build):
        vep_cmd += ["--assembly", "GRCh37"]

    if lib.fasta:
        vep_cmd += ["--fasta", lib.fasta]

    # The first cache that exists: --vep-cache (or the configured one), the
    # reference library's, then VEP's default.
    candidates = [args.vep_cache, lib.vep_cache, expand_path("~/.vep")]
    cache_dir = next(
        (str(c) for c in candidates if c and Path(c).is_dir()),
        str(args.vep_cache or lib.vep_cache or candidates[-1]),
    )
    vep_cmd += ["--dir_cache", cache_dir, "--cache_version", args.vep_cache_version]
    if Path(cache_dir).exists():
        vep_cmd.append("--offline")
        logging.info(LOG_MESSAGES["vep_using_cache"].format(path=cache_dir))
        _warn_if_cache_version_missing(cache_dir, args, vep_cmd)
    else:
        logging.warning(LOG_MESSAGES["vep_no_cache_warn"].format(path=cache_dir))
        vep_cmd.append("--database")
        if "GRCh37" in vep_cmd:
            vep_cmd += ["--port", "3337"]  # Ensembl serves GRCh37 there

    vep_cmd += VEP_PRESETS[getattr(args, "preset", "everything")]
    if vcf_type in ("sv", "cnv"):
        vep_cmd += ["--overlaps", "--max_sv_size", "300000000", "--buffer_size", "500"]
    if args.vep_args:
        vep_cmd += shlex.split(args.vep_args)
    return vep_cmd


def _warn_if_cache_version_missing(
    cache_dir: str, args: argparse.Namespace, vep_cmd: list[str]
) -> None:
    assembly = (
        vep_cmd[vep_cmd.index("--assembly") + 1] if "--assembly" in vep_cmd else None
    )
    if not assembly:
        return
    species = "homo_sapiens"
    version_dir = Path(cache_dir, species, f"{args.vep_cache_version}_{assembly}")
    if not version_dir.is_dir():
        logging.warning(
            f"{version_dir} is missing, so VEP will find no cache. Download it "
            f"with 'wgsextract vep download --vep-version {args.vep_cache_version} "
            f"--assembly {assembly}', or pass the installed release with "
            "--vep-cache-version."
        )


def _prepare_vcf_input(
    args: argparse.Namespace, vcf: str, work: str, threads: str
) -> str:
    """The VCF VEP reads: BCF converted to VCF, and cut to --region if given."""
    if not args.region and not vcf.lower().endswith(".bcf"):
        return vcf
    out = str(Path(work, "input.vcf.gz"))
    region = ["-r", args.region] if args.region else []
    run_command(
        ["bcftools", "view", *region, "--threads", threads, "-Oz", "-o", out, vcf]
    )
    return out


def _run_vep_on_input(
    args: argparse.Namespace, current_input: str, threads: str
) -> tuple[str, str]:
    """Annotate one input; returns a (status, duration) batch summary row."""
    is_bam = current_input.lower().endswith((".bam", ".cram"))
    outdir = args.outdir or str(absolute_path(current_input).parent)
    Path(outdir).mkdir(parents=True, exist_ok=True)
    logging.debug(f"Input file: {str(absolute_path(current_input))}")
    logging.debug(f"Output directory: {str(absolute_path(outdir))}")

    output_file, final_output = vep_output_paths(current_input, outdir, args.format)
    if Path(final_output).exists() and not args.force:
        logging.info(
            LOG_MESSAGES["vep_skipping_exists"].format(
                input=current_input, output=final_output
            )
        )
        return "SKIPPED", "-"

    start_time = time.time()
    try:
        # Inside the try: one bad file fails its own row, not the whole batch.
        if not is_bam:
            ensure_vcf_indexed(current_input)
        md5_sig = calculate_bam_md5(current_input, None) if is_bam else None
        lib = ReferenceLibrary(args.ref, md5_sig, input_path=current_input)
        _apply_forced_assembly(lib, args.vep_assembly)
        logging.debug(f"Resolved reference: {lib.fasta}")
        if is_bam and not lib.fasta:
            raise WGSExtractError(
                LOG_MESSAGES["ref_required_for"].format(
                    task="variant calling from BAM/CRAM"
                )
            )

        with tempfile.TemporaryDirectory(dir=outdir, prefix=".wgse_vep_") as work:
            vep_input_vcf = current_input
            if not is_bam:
                vep_input_vcf = _prepare_vcf_input(args, current_input, work, threads)
            if is_bam:
                assert lib.fasta is not None
                logging.info(LOG_MESSAGES["vep_calling_pre"])
                vep_input_vcf = str(Path(work, "variants.vcf.gz"))
                _call_variants_for_vep(
                    args, current_input, lib.fasta, lib, threads, vep_input_vcf
                )

            if args.add_chr:
                chr_vcf = str(Path(work, "with_chr.vcf"))
                preprocess_vcf_chr_prefix(vep_input_vcf, chr_vcf)
                vep_input_vcf = chr_vcf

            vcf_type = (
                detect_vcf_type(current_input)
                if args.vcf_type == "auto"
                else args.vcf_type
            )
            # Written in work and moved into place once complete, so a failed
            # run leaves nothing that a rerun would skip as done.
            vep_output = str(Path(work, "vep" + Path(final_output).suffix))
            vep_cmd = _build_vep_command(
                args, vep_input_vcf, vep_output, vcf_type, lib, threads
            )
            if args.format == "vcf":
                # bgzipped as it's written: a genome's uncompressed VEP VCF is
                # ~25 GB.
                vep_cmd += ["--compress_output", "bgzip"]
            logging.info(LOG_MESSAGES["vep_running"].format(command=" ".join(vep_cmd)))
            run_command(vep_cmd)

            if args.format == "vcf":
                run_command(["tabix", "-f", "-p", "vcf", vep_output])
                Path(vep_output + ".tbi").replace(final_output + ".tbi")
            # VEP's report files go beside the output.
            for suffix in ("_summary.html", "_warnings.txt"):
                if Path(vep_output + suffix).exists():
                    Path(vep_output + suffix).replace(output_file + suffix)
            Path(vep_output).replace(final_output)
    except (OSError, subprocess.SubprocessError, WGSExtractError, ValueError) as e:
        logging.error(f"VEP failed for {current_input}: {e}")
        return "FAILED", "-"

    logging.info(LOG_MESSAGES["vep_complete"].format(path=final_output))
    return "SUCCESS", str(timedelta(seconds=int(time.time() - start_time)))


def cmd_vep(args: argparse.Namespace) -> None:
    if not args.input:
        raise WGSExtractError(LOG_MESSAGES["input_required"])
    input_files = _vep_input_files(args.input)

    verify_dependencies(["vep", "tabix", "bcftools"])
    log_dependency_info(["vep", "tabix", "bcftools"])
    threads, _ = get_resource_defaults(args.threads, None)

    batch_stats = [
        (Path(path).name, *_run_vep_on_input(args, path, threads))
        for path in input_files
    ]

    if len(input_files) > 1:
        print(f"\n{LOG_MESSAGES['vep_batch_summary']}")
        print("-" * 60)
        for fname, status, dur in batch_stats:
            print(
                LOG_MESSAGES["vep_batch_item"].format(
                    filename=fname, status=status, duration=dur
                )
            )
        print("-" * 60)

    if any(status == "FAILED" for _, status, _ in batch_stats):
        raise WGSExtractError("One or more VEP tasks failed.")
