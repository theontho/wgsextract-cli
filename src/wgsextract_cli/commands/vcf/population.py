import argparse
import logging
import os

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

PHYLOP_ANNOTATED = "phylop_annotated.vcf.gz"
GNOMAD_ANNOTATED = "gnomad_annotated.vcf.gz"


def cmd_phylop(args: argparse.Namespace) -> None:
    verify_dependencies(["bcftools", "tabix"])
    log_dependency_info(["bcftools", "tabix"])
    input_file, outdir, lib = annotation_context(args)
    logging.info(LOG_MESSAGES["vcf_phylop_start"].format(input=input_file))

    phylop_file = require_annotation_file(
        args.phylop_file or lib.phylop_file, "vcf_phylop_missing", "phylop"
    )
    logging.info(LOG_MESSAGES["vcf_phylop_resolve"].format(path=phylop_file))

    phylop_vcf = prepare_tabix_annotation(phylop_file, "PhyloP")
    ann_out = os.path.join(outdir, PHYLOP_ANNOTATED)
    if phylop_vcf.lower().endswith(VCF_SUFFIXES):
        annotate_vcf(
            ensure_vcf_prepared(input_file),
            phylop_vcf,
            "INFO/PHYLOP",
            ann_out,
            outdir,
            "PhyloP",
        )
    else:
        # Annovar PhyloP TSV: #Chr, Start, End, Score
        annotate_vcf(
            ensure_vcf_prepared(input_file),
            phylop_vcf,
            "CHROM,POS,-,INFO/PHYLOP",
            ann_out,
            outdir,
            "PhyloP",
            [
                "##INFO=<ID=PHYLOP,Number=1,Type=Float,"
                'Description="PhyloP conservation score">'
            ],
        )

    run_min_score_filter(
        ann_out, outdir, args.min_score, "PHYLOP", "phylop", "PhyloP", "vcf_phylop_done"
    )


def cmd_gnomad(args: argparse.Namespace) -> None:
    verify_dependencies(["bcftools", "tabix"])
    log_dependency_info(["bcftools", "tabix"])
    input_file, outdir, lib = annotation_context(args)
    logging.info(f"Annotating VCF with gnomAD data: {input_file}")

    gnomad_file = require_annotation_file(
        args.gnomad_file or lib.gnomad_vcf, "vcf_gnomad_missing", "gnomad"
    )
    logging.info(f"Using gnomAD file: {gnomad_file}")

    # Transfer AF as GNOMAD_AF to avoid colliding with the caller's AF.
    ann_out = os.path.join(outdir, GNOMAD_ANNOTATED)
    annotate_vcf(
        ensure_vcf_prepared(input_file),
        ensure_vcf_prepared(gnomad_file),
        "INFO/GNOMAD_AF:=INFO/AF",
        ann_out,
        outdir,
        "gnomAD",
        [
            "##INFO=<ID=GNOMAD_AF,Number=A,Type=Float,"
            'Description="gnomAD Allele Frequency">'
        ],
    )

    if args.max_af is None:
        logging.info(LOG_MESSAGES["vcf_gnomad_done"].format(output=ann_out))
        return

    filter_out = os.path.join(outdir, f"gnomad_af_lt_{args.max_af}.vcf.gz")
    logging.info(
        f"Filtering for variants with gnomAD Allele Frequency < {args.max_af} "
        f"to {filter_out}"
    )
    # Rare-variant filtering keeps variants absent from gnomAD too.
    run_filter(
        ann_out, filter_out, f"GNOMAD_AF < {args.max_af} || GNOMAD_AF='.'", "gnomAD"
    )
    logging.info(f"gnomAD filtering complete: {filter_out}")
