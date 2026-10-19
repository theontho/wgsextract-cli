import argparse
import logging
import os

from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.pharmgkb import INFO_HEADER as PHARMGKB_INFO_HEADER
from wgsextract_cli.core.variant_files import ensure_vcf_prepared

from .annotation_helpers import (
    VCF_SUFFIXES,
    annotate_vcf,
    annotation_context,
    prepare_tabix_annotation,
    require_annotation_file,
    run_min_score_filter,
)

SPLICEAI_ANNOTATED = "spliceai_annotated.vcf.gz"
ALPHAMISSENSE_ANNOTATED = "alphamissense_annotated.vcf.gz"
PHARMGKB_ANNOTATED = "pharmgkb_annotated.vcf.gz"


def cmd_spliceai(args: argparse.Namespace) -> None:
    verify_dependencies(["bcftools", "tabix"])
    log_dependency_info(["bcftools", "tabix"])
    input_file, outdir, lib = annotation_context(args)
    logging.info(f"Annotating VCF with SpliceAI scores: {input_file}")

    spliceai_file = require_annotation_file(
        args.spliceai_file or lib.spliceai_vcf, "vcf_spliceai_missing", "spliceai"
    )
    ann_out = os.path.join(outdir, SPLICEAI_ANNOTATED)
    annotate_vcf(
        ensure_vcf_prepared(input_file),
        ensure_vcf_prepared(spliceai_file),
        "INFO/SpliceAI",
        ann_out,
        outdir,
        "SpliceAI",
    )
    logging.info(LOG_MESSAGES["vcf_spliceai_done"].format(output=ann_out))


def cmd_alphamissense(args: argparse.Namespace) -> None:
    verify_dependencies(["bcftools", "tabix"])
    log_dependency_info(["bcftools", "tabix"])
    input_file, outdir, lib = annotation_context(args)
    logging.info(f"Annotating VCF with AlphaMissense scores: {input_file}")

    am_file = require_annotation_file(
        args.am_file or lib.alphamissense_vcf,
        "vcf_alphamissense_missing",
        "alphamissense",
    )
    am_vcf = prepare_tabix_annotation(am_file, "AlphaMissense")
    ann_out = os.path.join(outdir, ALPHAMISSENSE_ANNOTATED)
    if am_vcf.lower().endswith(VCF_SUFFIXES):
        annotate_vcf(
            ensure_vcf_prepared(input_file),
            am_vcf,
            "INFO/am_pathogenicity,INFO/am_class",
            ann_out,
            outdir,
            "AlphaMissense",
        )
    else:
        # TSV: CHROM POS REF ALT genome uniprot_id transcript_id protein_variant
        #      am_pathogenicity am_class
        annotate_vcf(
            ensure_vcf_prepared(input_file),
            am_vcf,
            "CHROM,POS,REF,ALT,-,-,-,-,INFO/am_pathogenicity,INFO/am_class",
            ann_out,
            outdir,
            "AlphaMissense",
            [
                "##INFO=<ID=am_pathogenicity,Number=1,Type=Float,"
                'Description="AlphaMissense pathogenicity score">',
                "##INFO=<ID=am_class,Number=1,Type=String,"
                'Description="AlphaMissense classification">',
            ],
        )

    run_min_score_filter(
        ann_out,
        outdir,
        args.min_score,
        "am_pathogenicity",
        "alphamissense",
        "AlphaMissense",
        "vcf_alphamissense_done",
    )


def cmd_pharmgkb(args: argparse.Namespace) -> None:
    verify_dependencies(["bcftools", "tabix"])
    log_dependency_info(["bcftools", "tabix"])
    input_file, outdir, lib = annotation_context(args)
    logging.info(f"Annotating VCF with PharmGKB data: {input_file}")

    pharmgkb_file = require_annotation_file(
        args.pharmgkb_file or lib.pharmgkb_vcf, "vcf_pharmgkb_missing", "pharmgkb"
    )
    ann_out = os.path.join(outdir, PHARMGKB_ANNOTATED)
    if pharmgkb_file.lower().endswith(VCF_SUFFIXES):
        annotate_vcf(
            ensure_vcf_prepared(input_file),
            ensure_vcf_prepared(pharmgkb_file),
            "INFO/PHARMGKB",
            ann_out,
            outdir,
            "PharmGKB",
        )
    else:
        # The table `ref pharmgkb` builds: #CHROM, POS, REF, ALT, PHARMGKB.
        annotate_vcf(
            ensure_vcf_prepared(input_file),
            prepare_tabix_annotation(pharmgkb_file, "PharmGKB"),
            "CHROM,POS,REF,ALT,INFO/PHARMGKB",
            ann_out,
            outdir,
            "PharmGKB",
            [PHARMGKB_INFO_HEADER],
        )
    logging.info(f"PharmGKB annotation complete: {ann_out}")
