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
from .bigwig_scores import annotate_from_bigwig

PHYLOP_ANNOTATED = "phylop_annotated.vcf.gz"
GNOMAD_ANNOTATED = "gnomad_annotated.vcf.gz"


def cmd_phylop(args: argparse.Namespace) -> None:
    verify_dependencies(["bcftools", "tabix", "bgzip"])
    log_dependency_info(["bcftools", "tabix", "bgzip"])
    input_file, outdir, lib = annotation_context(args)
    logging.info(LOG_MESSAGES["vcf_phylop_start"].format(input=input_file))

    phylop_file = require_annotation_file(
        args.phylop_file or lib.phylop_file, "vcf_phylop_missing", "phylop"
    )
    logging.info(LOG_MESSAGES["vcf_phylop_resolve"].format(path=phylop_file))

    ann_out = str(Path(outdir, PHYLOP_ANNOTATED))
    if phylop_file.lower().endswith((".bw", ".bigwig")):
        # `ref phylop`'s UCSC bigWig: one score per base.
        annotate_from_bigwig(
            ensure_vcf_prepared(input_file),
            phylop_file,
            "PHYLOP",
            "PhyloP",
            "PhyloP 100-way vertebrate conservation score",
            ann_out,
            outdir,
            threads=getattr(args, "threads", None),
        )
    else:
        _annotate_from_phylop_table(args, input_file, phylop_file, ann_out, outdir)

    run_min_score_filter(
        ann_out, outdir, args.min_score, "PHYLOP", "phylop", "PhyloP", "vcf_phylop_done"
    )


def _annotate_from_phylop_table(
    args: argparse.Namespace,
    input_file: str,
    phylop_file: str,
    ann_out: str,
    outdir: str,
) -> None:
    """A PhyloP VCF, or a TSV of #Chr, Start, End, Score, the user supplied."""
    phylop_vcf = prepare_tabix_annotation(phylop_file, "PhyloP")
    if phylop_vcf.lower().endswith(VCF_SUFFIXES):
        annotate_vcf(
            ensure_vcf_prepared(input_file),
            phylop_vcf,
            "INFO/PHYLOP",
            ann_out,
            outdir,
            "PhyloP",
            threads=getattr(args, "threads", None),
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
            threads=getattr(args, "threads", None),
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
    ann_out = str(Path(outdir, GNOMAD_ANNOTATED))
    header = [
        '##INFO=<ID=GNOMAD_AF,Number=A,Type=Float,Description="gnomAD Allele Frequency">'
    ]
    if gnomad_file.lower().endswith(VCF_SUFFIXES):
        annotation = ensure_vcf_prepared(gnomad_file)
        columns = "INFO/GNOMAD_AF:=INFO/AF"
    else:
        # `ref gnomad --release 4.1` table: #CHROM, POS, REF, ALT, AF
        annotation = prepare_tabix_annotation(gnomad_file, "gnomAD")
        columns = "CHROM,POS,REF,ALT,INFO/GNOMAD_AF"
    annotate_vcf(
        ensure_vcf_prepared(input_file),
        annotation,
        columns,
        ann_out,
        outdir,
        "gnomAD",
        header,
        threads=getattr(args, "threads", None),
    )

    if args.max_af is None:
        logging.info(LOG_MESSAGES["vcf_gnomad_done"].format(output=ann_out))
        return

    filter_out = str(Path(outdir, f"gnomad_af_lt_{args.max_af}.vcf.gz"))
    logging.info(
        f"Filtering for variants with gnomAD Allele Frequency < {args.max_af} "
        f"to {filter_out}"
    )
    # Rare-variant filtering keeps variants absent from gnomAD too.
    run_filter(
        ann_out, filter_out, f"GNOMAD_AF < {args.max_af} || GNOMAD_AF='.'", "gnomAD"
    )
    logging.info(f"gnomAD filtering complete: {filter_out}")
