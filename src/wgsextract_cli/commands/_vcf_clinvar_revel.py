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

from ._vcf_annotation_helpers import (
    annotation_context,
    cleanup_annotation_temporaries,
    normalize_to_annotation_chroms,
    prepare_tabix_annotation,
    run_min_score_filter,
)
from ._vcf_structural import (
    _exit_if_missing,
)


def cmd_clinvar(args: argparse.Namespace) -> None:
    verify_dependencies(["bcftools", "tabix"])
    log_dependency_info(["bcftools", "tabix"])
    input_file, outdir, lib = annotation_context(args)
    logging.info(LOG_MESSAGES["vcf_clinvar_start"].format(input=input_file))

    # Resolve ClinVar VCF
    clinvar_vcf = args.clinvar_file if args.clinvar_file else lib.clinvar_vcf

    _exit_if_missing(clinvar_vcf, "vcf_clinvar_missing", "clinvar")

    logging.info(LOG_MESSAGES["vcf_clinvar_resolve"].format(path=clinvar_vcf))

    # 1. Prepare Inputs
    input_vcf = ensure_vcf_prepared(input_file)
    clinvar_prepared = ensure_vcf_prepared(clinvar_vcf)

    # 2. Match chromosome styles (chr1 vs 1)
    from wgsextract_cli.core.variant_files import normalize_vcf_chromosomes

    try:
        res_c = run_command(
            ["bcftools", "index", "-s", clinvar_prepared],
            capture_output=True,
        )
        c_chroms = [line.split("\t")[0] for line in res_c.stdout.strip().split("\n")]
        normalized_input = normalize_vcf_chromosomes(input_vcf, c_chroms)
    except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
        logging.warning(f"ClinVar chromosome normalization skipped: {e}")
        normalized_input = input_vcf

    # 3. Annotate with ClinVar
    # We transfer CLNSIG (Significance) and CLNDN (Disease Name)
    ann_out = os.path.join(outdir, "clinvar_annotated.vcf.gz")
    try:
        run_command(
            [
                "bcftools",
                "annotate",
                "-a",
                clinvar_prepared,
                "-c",
                "CLNSIG,CLNDN",
                "-Oz",
                "-o",
                ann_out,
                normalized_input,
            ],
            capture_output=True,
        )
        ensure_vcf_indexed(ann_out)
    except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
        logging.error(f"ClinVar annotation failed: {e}")
        raise WGSExtractError("VCF processing failed.") from e
    finally:
        if normalized_input != input_vcf and os.path.exists(normalized_input):
            os.remove(normalized_input)

    # 4. Filter for Pathogenic
    path_out = os.path.join(outdir, "clinvar_pathogenic.vcf.gz")
    logging.info(LOG_MESSAGES["vcf_clinvar_filtering"].format(output=path_out))
    try:
        # Filter for Pathogenic or Likely_pathogenic in CLNSIG
        # The exact string can vary slightly, so we use a regex/substring match
        filter_expr = 'CLNSIG ~ "Pathogenic" || CLNSIG ~ "Likely_pathogenic"'
        run_command(
            ["bcftools", "filter", "-i", filter_expr, "-Oz", "-o", path_out, ann_out],
            capture_output=True,
        )
        ensure_vcf_indexed(path_out)
        logging.info(LOG_MESSAGES["vcf_clinvar_done"].format(output=path_out))
    except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
        logging.error(f"ClinVar filtering failed: {e}")
        raise WGSExtractError("ClinVar filtering failed.") from e


def cmd_revel(args: argparse.Namespace) -> None:
    verify_dependencies(["bcftools", "tabix"])
    log_dependency_info(["bcftools", "tabix"])
    input_file, outdir, lib = annotation_context(args)
    logging.info(LOG_MESSAGES["vcf_revel_start"].format(input=input_file))

    # Resolve REVEL data file
    revel_file = args.revel_file if args.revel_file else lib.revel_file

    _exit_if_missing(revel_file, "vcf_revel_missing", "revel")
    revel_file = str(revel_file)

    logging.info(LOG_MESSAGES["vcf_revel_resolve"].format(path=revel_file))

    # 1. Prepare Inputs
    input_vcf = ensure_vcf_prepared(input_file)
    revel_vcf = prepare_tabix_annotation(revel_file, "REVEL")

    normalized_input, needs_cleanup = normalize_to_annotation_chroms(
        input_vcf, revel_vcf, outdir, "REVEL", "input_revel_norm.vcf.gz"
    )

    # 3. Annotate with REVEL
    ann_out = os.path.join(outdir, "revel_annotated.vcf.gz")
    header_tmp = None
    try:
        # Annovar REVEL TSV: #Chr, Start, End, Ref, Alt, REVEL
        cols = "CHROM,POS,-,REF,ALT,INFO/REVEL"
        annotate_args = [
            "bcftools",
            "annotate",
            "-a",
            revel_vcf,
            "-Oz",
            "-o",
            ann_out,
        ]

        if revel_vcf.lower().endswith((".vcf", ".vcf.gz")):
            annotate_args.extend(["-c", "INFO/REVEL"])
        else:
            import tempfile

            fd, header_tmp = tempfile.mkstemp(suffix=".hdr", dir=outdir)
            with os.fdopen(fd, "w") as f:
                f.write(
                    '##INFO=<ID=REVEL,Number=1,Type=Float,Description="REVEL score">\n'
                )
            annotate_args.extend(["-c", cols, "-h", header_tmp])

        annotate_args.append(normalized_input)
        run_command(annotate_args, capture_output=True)
        ensure_vcf_indexed(ann_out)
    except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
        logging.error(f"REVEL annotation failed: {e}")
        raise WGSExtractError("VCF processing failed.") from e
    finally:
        cleanup_annotation_temporaries(header_tmp, normalized_input, needs_cleanup)

    run_min_score_filter(
        ann_out,
        outdir,
        args.min_score,
        "REVEL",
        "revel",
        "vcf_revel_filtering",
        "vcf_revel_done",
        "REVEL",
    )
