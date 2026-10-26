import argparse
import gzip
import logging
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from wgsextract_cli.core.alignment_health import warn_about_alignment
from wgsextract_cli.core.builds import is_hg37_build, is_hg38_build, ploidy_for_build
from wgsextract_cli.core.config import settings
from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.output_checks import warn_if_no_variants
from wgsextract_cli.core.paths import absolute_path
from wgsextract_cli.core.reference_resolver import ReferenceLibrary
from wgsextract_cli.core.sample_ploidy import sex_samples_args
from wgsextract_cli.core.utils import (
    WGSExtractError,
    get_resource_defaults,
    run_command,
    run_pipeline,
)
from wgsextract_cli.core.variant_files import (
    calculate_bam_md5,
    ensure_vcf_indexed,
    ensure_vcf_prepared,
    verify_paths_exist,
)
from wgsextract_cli.core.warnings import print_warning


def _select_vcf_input(args: argparse.Namespace) -> str | None:
    """
    Pick the VCF a command should read.

    An explicit --vcf-input wins, then an explicit --input; otherwise the
    configured default VCF, then the (possibly configured) --input.
    """
    input_path = getattr(args, "input", None)
    vcf_input = getattr(args, "vcf_input", None)
    default_vcf = settings.get("default_input_vcf")
    explicit_dests: set[str] = getattr(args, "_explicit_dests", set())

    if vcf_input and vcf_input != default_vcf:
        return str(vcf_input)
    if "input" in explicit_dests and input_path:
        return str(input_path)
    selected = vcf_input if vcf_input else input_path
    return str(selected) if selected else None


def require_vcf_input(args: argparse.Namespace) -> str:
    input_file = _select_vcf_input(args)
    if not input_file:
        raise WGSExtractError(LOG_MESSAGES["input_required"])
    return input_file


def get_base_args(args: argparse.Namespace) -> tuple[str, str, str, ReferenceLibrary]:
    """Resolve (threads, outdir, reference FASTA, library) for BAM/CRAM callers."""
    input_file = getattr(args, "input", None)
    if not input_file:
        raise WGSExtractError(LOG_MESSAGES["input_required"])

    logging.debug(f"Input file: {str(absolute_path(input_file))}")
    threads, _ = get_resource_defaults(args.threads, None)
    outdir = args.outdir or str(absolute_path(input_file).parent)
    Path(outdir).mkdir(parents=True, exist_ok=True)
    logging.debug(f"Output directory: {str(absolute_path(outdir))}")

    lib = ReferenceLibrary(
        args.ref, calculate_bam_md5(input_file, None), input_path=input_file
    )
    resolved_ref = lib.fasta
    logging.debug(f"Resolved reference: {resolved_ref}")

    paths_to_check = {"--input": input_file}
    if resolved_ref:
        paths_to_check["--ref"] = resolved_ref
    verify_paths_exist(paths_to_check)

    if not resolved_ref or not Path(resolved_ref).is_file():
        raise WGSExtractError(
            LOG_MESSAGES["ref_required_for"].format(task="variant calling")
        )
    warn_about_alignment(input_file, resolved_ref)
    return threads, outdir, resolved_ref, lib


def _ploidy_args(args: argparse.Namespace, lib: ReferenceLibrary) -> list[str]:
    ploidy = getattr(args, "ploidy", None)
    ploidy_file = getattr(args, "ploidy_file", None)
    if not ploidy and not ploidy_file:
        ploidy_file = lib.ploidy_file
    if ploidy_file:
        verify_paths_exist({"--ploidy-file": ploidy_file})
        return ["--ploidy-file", ploidy_file]
    if ploidy:
        return ["--ploidy", ploidy]
    # bcftools' predefined GRCh37/GRCh38 ploidy covers both X and chrX naming.
    # T2T's sex-chromosome coordinates differ, so it needs a ploidy file.
    if lib.build and (is_hg37_build(lib.build) or is_hg38_build(lib.build)):
        return ["--ploidy", ploidy_for_build(lib.build)]
    raise WGSExtractError(
        "Ploidy (--ploidy or --ploidy-file) is required and could not be "
        f"auto-resolved from --ref (reference build: {lib.build or 'unknown'})."
    )


@contextmanager
def plain_reference(ref: str, outdir: str) -> Iterator[str]:
    """
    Yield an uncompressed, faidx-indexed FASTA for tools that cannot read .fa.gz.

    Uses the uncompressed sibling (ref.fa next to ref.fa.gz) when present, indexing
    it if needed; otherwise decompresses into a temporary directory under outdir
    that is removed on exit.
    """
    if not ref.lower().endswith(".gz"):
        yield ref
        return
    sibling = ref[:-3]
    if Path(sibling).is_file():
        if not Path(sibling + ".fai").is_file():
            try:
                run_command(["samtools", "faidx", sibling], capture_output=True)
            except (OSError, subprocess.SubprocessError) as e:
                logging.debug(f"Could not index {sibling} in place: {e}")
        if Path(sibling + ".fai").is_file():
            yield sibling
            return
    with tempfile.TemporaryDirectory(dir=outdir, prefix=".wgse_ref_") as tmp:
        target = str(Path(tmp, Path(sibling).name))
        logging.info(f"Decompressing reference for this tool to {target}...")
        try:
            with gzip.open(ref, "rb") as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, 16 * 1024 * 1024)
            run_command(["samtools", "faidx", target], capture_output=True)
        except (OSError, EOFError, subprocess.SubprocessError) as e:
            raise WGSExtractError(
                f"Failed to prepare uncompressed reference: {e}"
            ) from e
        yield target


def cmd_snp(args: argparse.Namespace) -> None:
    verify_dependencies(["bcftools", "tabix"])
    log_dependency_info(["bcftools", "tabix"])
    threads, outdir, ref, lib = get_base_args(args)
    print_warning("ButtonSNPVCF", threads=threads)

    out_vcf = str(Path(outdir, "snps.vcf.gz"))
    logging.info(LOG_MESSAGES["vcf_calling_snps"].format(output=out_vcf))
    region_args = ["-r", args.region] if args.region else []
    ploidy_args = _ploidy_args(args, lib)

    with sex_samples_args(
        args.input, outdir, ploidy_args, region=args.region
    ) as sex_args:
        run_pipeline(
            [
                ["bcftools", "mpileup", "-B", "-I", "-C", "50", "-f", ref, "-Ou"]
                + region_args
                + [args.input],
                ["bcftools", "call"]
                + ploidy_args
                + sex_args
                + ["-V", "indels", "-v", "-m", "-P", "0", "--threads", threads]
                + ["-Oz", "-o", out_vcf],
            ],
            description="SNP variant calling",
        )
    ensure_vcf_indexed(out_vcf)
    warn_if_no_variants(out_vcf, ref, args.region)


def cmd_indel(args: argparse.Namespace) -> None:
    verify_dependencies(["bcftools", "tabix"])
    log_dependency_info(["bcftools", "tabix"])
    threads, outdir, ref, lib = get_base_args(args)
    print_warning("ButtonInDelVCF", threads=threads)

    out_vcf = str(Path(outdir, "indels.vcf.gz"))
    logging.info(LOG_MESSAGES["vcf_calling_indels"].format(output=out_vcf))
    region_args = ["-r", args.region] if args.region else []
    ploidy_args = _ploidy_args(args, lib)

    with sex_samples_args(
        args.input, outdir, ploidy_args, region=args.region
    ) as sex_args:
        run_pipeline(
            [
                ["bcftools", "mpileup", "-B", "-C", "50", "-f", ref, "-Ou"]
                + region_args
                + [args.input],
                ["bcftools", "call"]
                + ploidy_args
                + sex_args
                + ["-V", "snps", "-v", "-m", "-P", "0", "--threads", threads, "-Ou"],
                ["bcftools", "norm", "-f", ref, "--threads", threads]
                + ["-Oz", "-o", out_vcf],
            ],
            description="InDel variant calling",
        )
    ensure_vcf_indexed(out_vcf)
    warn_if_no_variants(out_vcf, ref, args.region)


def _annotation_columns(ann_vcf: str) -> str:
    """Infer bcftools annotate -c columns from a tab file header or VCF header."""
    if ann_vcf.lower().endswith(
        (".tab", ".tab.gz", ".txt", ".txt.gz", ".csv", ".csv.gz")
    ):
        col_map = {
            "CHROM": "CHROM",
            "CHR": "CHROM",
            "POS": "POS",
            "ID": "ID",
            "HG": "INFO/HG",
            "RSID": "ID",
        }
        try:
            open_func = gzip.open if ann_vcf.endswith(".gz") else open
            with open_func(ann_vcf, "rt") as f:
                header = f.readline().strip().lstrip("#")
            found_cols = [
                col_map.get(col.upper().lstrip("#"), "-") for col in header.split()
            ]
            if "CHROM" in found_cols and "POS" in found_cols:
                cols = ",".join(found_cols)
                logging.info(f"Auto-resolved columns from header: {cols}")
                return cols
        except (OSError, UnicodeError, ValueError):
            logging.debug("Failed to parse tab header.", exc_info=True)

    if ann_vcf.lower().endswith((".vcf", ".vcf.gz")):
        try:
            res = run_command(["bcftools", "view", "-h", ann_vcf], capture_output=True)
            cols = "ID,INFO/HG" if "ID=HG," in res.stdout else "ID"
            logging.info(f"Auto-resolved VCF columns: {cols}")
            return cols
        except (OSError, subprocess.SubprocessError):
            logging.debug(
                "Failed to inspect VCF header for annotation columns.", exc_info=True
            )

    logging.info("Using default annotation column: ID")
    return "ID"


def cmd_annotate(args: argparse.Namespace) -> None:
    verify_dependencies(["bcftools", "tabix"])
    log_dependency_info(["bcftools", "tabix"])
    input_file = require_vcf_input(args)
    logging.debug(f"Input file: {str(absolute_path(input_file))}")

    outdir = args.outdir or str(absolute_path(input_file).parent)
    logging.debug(f"Output directory: {str(absolute_path(outdir))}")
    out_vcf = str(Path(outdir, "annotated.vcf.gz"))

    ann_vcf = args.ann_vcf
    if not ann_vcf:
        md5_sig = (
            calculate_bam_md5(input_file, None)
            if input_file.lower().endswith((".bam", ".cram"))
            else None
        )
        lib = ReferenceLibrary(args.ref, md5_sig, input_path=input_file)
        if not lib.ref_vcf_tab:
            raise WGSExtractError(
                "--ann-vcf is required and could not be auto-resolved."
            )
        ann_vcf = lib.ref_vcf_tab
        logging.info(f"Auto-resolved annotation file: {ann_vcf}")

    verify_paths_exist({"--input": input_file, "--ann-vcf": ann_vcf})
    cols = args.cols or _annotation_columns(ann_vcf)

    input_vcf = ensure_vcf_prepared(input_file)
    ann_vcf = ensure_vcf_prepared(ann_vcf)

    logging.info(LOG_MESSAGES["vcf_annotating"].format(input=input_vcf, output=out_vcf))
    try:
        run_command(
            [
                "bcftools",
                "annotate",
                "-a",
                ann_vcf,
                "-c",
                cols,
                "-Oz",
                "-o",
                out_vcf,
                input_vcf,
            ]
        )
        ensure_vcf_indexed(out_vcf)
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(f"Annotation failed: {e}") from e
