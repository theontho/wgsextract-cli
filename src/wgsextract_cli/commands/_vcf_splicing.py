import argparse
import logging
import os
import subprocess

from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.utils import (
    WGSExtractError,
    run_command,
)
from wgsextract_cli.core.variant_files import (
    ensure_vcf_indexed,
    ensure_vcf_prepared,
)

from ._vcf_annotation_helpers import annotation_context, prepare_tabix_annotation
from ._vcf_structural import (
    _exit_if_missing,
)


def cmd_spliceai(args: argparse.Namespace) -> None:
    verify_dependencies(["bcftools", "tabix"])
    log_dependency_info(["bcftools", "tabix"])
    input_file, outdir, lib = annotation_context(args)
    logging.info(f"Annotating VCF with SpliceAI scores: {input_file}")

    spliceai_file = args.spliceai_file if args.spliceai_file else lib.spliceai_vcf

    _exit_if_missing(spliceai_file, "vcf_spliceai_missing", "spliceai")

    # 1. Prepare Inputs

    input_vcf = ensure_vcf_prepared(input_file)
    spliceai_vcf = ensure_vcf_prepared(spliceai_file)

    # 2. Match chromosome styles
    from wgsextract_cli.core.variant_files import normalize_vcf_chromosomes

    try:
        res_s = run_command(
            ["bcftools", "index", "-s", spliceai_vcf], capture_output=True
        )
        s_chroms = [line.split("\t")[0] for line in res_s.stdout.strip().split("\n")]
        normalized_input = normalize_vcf_chromosomes(input_vcf, s_chroms)
    except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
        logging.warning(f"SpliceAI chromosome normalization skipped: {e}")
        normalized_input = input_vcf

    # 3. Annotate with SpliceAI
    ann_out = os.path.join(outdir, "spliceai_annotated.vcf.gz")
    try:
        run_command(
            [
                "bcftools",
                "annotate",
                "-a",
                spliceai_vcf,
                "-c",
                "INFO/SpliceAI",
                "-Oz",
                "-o",
                ann_out,
                normalized_input,
            ],
            capture_output=True,
        )
        ensure_vcf_indexed(ann_out)
    except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
        logging.error(f"SpliceAI annotation failed: {e}")
        raise WGSExtractError("VCF processing failed.") from None
    finally:
        if normalized_input != input_vcf and os.path.exists(normalized_input):
            os.remove(normalized_input)

    # 4. Finish
    logging.info(LOG_MESSAGES["vcf_spliceai_done"].format(output=ann_out))


def cmd_alphamissense(args: argparse.Namespace) -> None:
    verify_dependencies(["bcftools", "tabix"])
    log_dependency_info(["bcftools", "tabix"])
    input_file, outdir, lib = annotation_context(args)
    logging.info(f"Annotating VCF with AlphaMissense scores: {input_file}")

    am_file = args.am_file if args.am_file else lib.alphamissense_vcf

    _exit_if_missing(am_file, "vcf_alphamissense_missing", "alphamissense")
    am_file = str(am_file)

    # 1. Prepare Inputs

    input_vcf = ensure_vcf_prepared(input_file)
    am_vcf = prepare_tabix_annotation(am_file, "AlphaMissense")

    # 2. Match chromosome styles
    from wgsextract_cli.core.variant_files import normalize_vcf_chromosomes

    try:
        res_a = run_command(["bcftools", "index", "-s", am_vcf], capture_output=True)
        a_chroms = [line.split("\t")[0] for line in res_a.stdout.strip().split("\n")]
        normalized_input = normalize_vcf_chromosomes(input_vcf, a_chroms)
    except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
        logging.warning(f"AlphaMissense chromosome normalization skipped: {e}")
        normalized_input = input_vcf

    # 3. Annotate with AlphaMissense
    ann_out = os.path.join(outdir, "alphamissense_annotated.vcf.gz")
    header_tmp = None
    try:
        if am_vcf.lower().endswith((".vcf", ".vcf.gz", ".vcf.bgz")):
            cols = "INFO/am_pathogenicity,INFO/am_class"
            h_arg = []
        else:
            # TSV: #CHROM  POS     REF     ALT     genome  uniprot_id      transcript_id   protein_variant am_pathogenicity        am_class
            cols = "CHROM,POS,REF,ALT,-,-,-,-,INFO/am_pathogenicity,INFO/am_class"
            import tempfile

            fd, header_tmp = tempfile.mkstemp(suffix=".hdr", dir=outdir)
            with os.fdopen(fd, "w") as f:
                f.write(
                    '##INFO=<ID=am_pathogenicity,Number=1,Type=Float,Description="AlphaMissense pathogenicity score">\n'
                )
                f.write(
                    '##INFO=<ID=am_class,Number=1,Type=String,Description="AlphaMissense classification">\n'
                )
            h_arg = ["-h", header_tmp]

        run_command(
            [
                "bcftools",
                "annotate",
                "-a",
                am_vcf,
                "-c",
                cols,
            ]
            + h_arg
            + [
                "-Oz",
                "-o",
                ann_out,
                normalized_input,
            ],
            capture_output=True,
        )
        ensure_vcf_indexed(ann_out)
    except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
        logging.error(f"AlphaMissense annotation failed: {e}")
        raise WGSExtractError("VCF processing failed.") from None
    finally:
        if header_tmp and os.path.exists(header_tmp):
            os.remove(header_tmp)
        if normalized_input != input_vcf and os.path.exists(normalized_input):
            os.remove(normalized_input)

    # 4. Optional Filtering
    if args.min_score is not None:
        path_out = os.path.join(outdir, f"alphamissense_gt_{args.min_score}.vcf.gz")
        try:
            run_command(
                [
                    "bcftools",
                    "filter",
                    "-i",
                    f"am_pathogenicity >= {args.min_score}",
                    "-Oz",
                    "-o",
                    path_out,
                    ann_out,
                ],
                capture_output=True,
            )
            ensure_vcf_indexed(path_out)
            logging.info(f"AlphaMissense filtering complete: {path_out}")
        except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
            logging.error(f"AlphaMissense filtering failed: {e}")
            raise WGSExtractError("AlphaMissense filtering failed.") from e
    else:
        logging.info(LOG_MESSAGES["vcf_alphamissense_done"].format(output=ann_out))


def cmd_pharmgkb(args: argparse.Namespace) -> None:
    verify_dependencies(["bcftools", "tabix"])
    log_dependency_info(["bcftools", "tabix"])
    input_file, outdir, lib = annotation_context(args)
    logging.info(f"Annotating VCF with PharmGKB data: {input_file}")

    pharmgkb_file = args.pharmgkb_file if args.pharmgkb_file else lib.pharmgkb_vcf

    _exit_if_missing(pharmgkb_file, "vcf_pharmgkb_missing", "pharmgkb")

    # 1. Prepare Inputs

    input_vcf = ensure_vcf_prepared(input_file)
    pharmgkb_vcf = ensure_vcf_prepared(pharmgkb_file)

    # 2. Match chromosome styles
    from wgsextract_cli.core.variant_files import normalize_vcf_chromosomes

    try:
        res_p = run_command(
            ["bcftools", "index", "-s", pharmgkb_vcf], capture_output=True
        )
        p_chroms = [line.split("\t")[0] for line in res_p.stdout.strip().split("\n")]
        normalized_input = normalize_vcf_chromosomes(input_vcf, p_chroms)
    except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
        logging.warning(f"PharmGKB chromosome normalization skipped: {e}")
        normalized_input = input_vcf

    # 3. Annotate with PharmGKB
    ann_out = os.path.join(outdir, "pharmgkb_annotated.vcf.gz")
    try:
        # Transfer all INFO fields from PharmGKB
        run_command(
            [
                "bcftools",
                "annotate",
                "-a",
                pharmgkb_vcf,
                "-c",
                "PHARMGKB",
                "-Oz",
                "-o",
                ann_out,
                normalized_input,
            ],
            capture_output=True,
        )

        ensure_vcf_indexed(ann_out)
    except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
        logging.error(f"PharmGKB annotation failed: {e}")
        raise WGSExtractError("VCF processing failed.") from None
    finally:
        if normalized_input != input_vcf and os.path.exists(normalized_input):
            os.remove(normalized_input)

    logging.info(f"PharmGKB annotation complete: {ann_out}")
