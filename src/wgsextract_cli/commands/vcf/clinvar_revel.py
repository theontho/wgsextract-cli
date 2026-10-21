import argparse
import logging
from pathlib import Path

from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.variant_files import ensure_vcf_prepared

from .annotation_helpers import (
    VCF_SUFFIXES,
    annotate_vcf,
    annotation_context,
    prepare_tabix_annotation,
    require_annotation_file,
    run_filter,
    run_min_score_filter,
)

CLINVAR_ANNOTATED = "clinvar_annotated.vcf.gz"
REVEL_ANNOTATED = "revel_annotated.vcf.gz"


def cmd_clinvar(args: argparse.Namespace) -> None:
    verify_dependencies(["bcftools", "tabix"])
    log_dependency_info(["bcftools", "tabix"])
    input_file, outdir, lib = annotation_context(args)
    logging.info(LOG_MESSAGES["vcf_clinvar_start"].format(input=input_file))

    clinvar_vcf = require_annotation_file(
        args.clinvar_file or lib.clinvar_vcf, "vcf_clinvar_missing", "clinvar"
    )
    logging.info(LOG_MESSAGES["vcf_clinvar_resolve"].format(path=clinvar_vcf))

    # Transfer CLNSIG (significance) and CLNDN (disease name).
    ann_out = str(Path(outdir, CLINVAR_ANNOTATED))
    annotate_vcf(
        ensure_vcf_prepared(input_file),
        ensure_vcf_prepared(clinvar_vcf),
        "CLNSIG,CLNDN",
        ann_out,
        outdir,
        "ClinVar",
        threads=getattr(args, "threads", None),
    )

    path_out = str(Path(outdir, "clinvar_pathogenic.vcf.gz"))
    logging.info(LOG_MESSAGES["vcf_clinvar_filtering"].format(output=path_out))
    # Substring match also covers Pathogenic/Likely_pathogenic combinations.
    run_filter(
        ann_out,
        path_out,
        'CLNSIG ~ "Pathogenic" || CLNSIG ~ "Likely_pathogenic"',
        "ClinVar",
    )
    logging.info(LOG_MESSAGES["vcf_clinvar_done"].format(output=path_out))


def cmd_revel(args: argparse.Namespace) -> None:
    verify_dependencies(["bcftools", "tabix"])
    log_dependency_info(["bcftools", "tabix"])
    input_file, outdir, lib = annotation_context(args)
    logging.info(LOG_MESSAGES["vcf_revel_start"].format(input=input_file))

    revel_file = require_annotation_file(
        args.revel_file or lib.revel_file, "vcf_revel_missing", "revel"
    )
    logging.info(LOG_MESSAGES["vcf_revel_resolve"].format(path=revel_file))

    revel_vcf = prepare_tabix_annotation(revel_file, "REVEL")
    ann_out = str(Path(outdir, REVEL_ANNOTATED))
    if revel_vcf.lower().endswith(VCF_SUFFIXES):
        annotate_vcf(
            ensure_vcf_prepared(input_file),
            revel_vcf,
            "INFO/REVEL",
            ann_out,
            outdir,
            "REVEL",
            threads=getattr(args, "threads", None),
        )
    else:
        # Annovar REVEL TSV: #Chr, Start, End, Ref, Alt, REVEL
        annotate_vcf(
            ensure_vcf_prepared(input_file),
            revel_vcf,
            "CHROM,POS,-,REF,ALT,INFO/REVEL",
            ann_out,
            outdir,
            "REVEL",
            ['##INFO=<ID=REVEL,Number=1,Type=Float,Description="REVEL score">'],
            threads=getattr(args, "threads", None),
        )

    run_min_score_filter(
        ann_out, outdir, args.min_score, "REVEL", "revel", "REVEL", "vcf_revel_done"
    )
