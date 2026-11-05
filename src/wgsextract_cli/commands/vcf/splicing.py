import logging
from pathlib import Path

from wgsextract_cli.api.vcf import (
    VcfAlphamissenseParams,
    VcfPharmgkbParams,
    VcfSpliceaiParams,
)
from wgsextract_cli.core.alignment_metadata import get_vcf_build
from wgsextract_cli.core.builds import is_hg38_build
from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.pharmgkb import INFO_HEADER as PHARMGKB_INFO_HEADER
from wgsextract_cli.core.utils import WGSExtractError
from wgsextract_cli.core.variant_files import ensure_vcf_prepared

from .annotation_helpers import (
    VCF_SUFFIXES,
    annotate_vcf,
    annotation_context,
    prepare_tabix_annotation,
    record_annotated,
    require_annotation_file,
    resource,
    run_min_score_filter,
)
from .basic import vcf_input_handler

SPLICEAI_ANNOTATED = "spliceai_annotated.vcf.gz"
ALPHAMISSENSE_ANNOTATED = "alphamissense_annotated.vcf.gz"
PHARMGKB_ANNOTATED = "pharmgkb_annotated.vcf.gz"


def run_spliceai(params: VcfSpliceaiParams) -> None:
    verify_dependencies(["bcftools", "tabix"])
    log_dependency_info(["bcftools", "tabix"])
    input_file, outdir, lib = annotation_context(params)
    logging.info(f"Annotating VCF with SpliceAI scores: {input_file}")

    spliceai_file = require_annotation_file(
        resource(params.spliceai_file, lib.spliceai_vcf),
        "vcf_spliceai_missing",
        "spliceai",
    )
    ann_out = str(Path(outdir, SPLICEAI_ANNOTATED))
    annotate_vcf(
        ensure_vcf_prepared(input_file),
        ensure_vcf_prepared(spliceai_file),
        "INFO/SpliceAI",
        ann_out,
        outdir,
        "SpliceAI",
        threads=params.threads,
    )
    record_annotated(ann_out)
    logging.info(LOG_MESSAGES["vcf_spliceai_done"].format(output=ann_out))


def run_alphamissense(params: VcfAlphamissenseParams) -> None:
    verify_dependencies(["bcftools", "tabix"])
    log_dependency_info(["bcftools", "tabix"])
    input_file, outdir, lib = annotation_context(params)
    logging.info(f"Annotating VCF with AlphaMissense scores: {input_file}")

    am_file = require_annotation_file(
        resource(params.am_file, lib.alphamissense_vcf),
        "vcf_alphamissense_missing",
        "alphamissense",
    )
    am_vcf = prepare_tabix_annotation(am_file, "AlphaMissense")
    ann_out = str(Path(outdir, ALPHAMISSENSE_ANNOTATED))
    if am_vcf.lower().endswith(VCF_SUFFIXES):
        annotate_vcf(
            ensure_vcf_prepared(input_file),
            am_vcf,
            "INFO/am_pathogenicity,INFO/am_class",
            ann_out,
            outdir,
            "AlphaMissense",
            threads=params.threads,
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
            threads=params.threads,
        )

    run_min_score_filter(
        ann_out,
        outdir,
        params.min_score,
        "am_pathogenicity",
        "alphamissense",
        "AlphaMissense",
        "vcf_alphamissense_done",
    )


def run_pharmgkb(params: VcfPharmgkbParams) -> None:
    verify_dependencies(["bcftools", "tabix"])
    log_dependency_info(["bcftools", "tabix"])
    input_file, outdir, lib = annotation_context(params)
    logging.info(f"Annotating VCF with PharmGKB data: {input_file}")

    pharmgkb_file = resource(params.pharmgkb_file, lib.pharmgkb_vcf)
    # Without a library, the build comes from the VCF's own header.
    build = lib.build or get_vcf_build(input_file)
    if not pharmgkb_file and build and not is_hg38_build(build):
        # `ref pharmgkb` builds GRCh38 positions only; running it won't help.
        raise WGSExtractError(
            LOG_MESSAGES["vcf_pharmgkb_grch38_only"].format(build=build)
        )
    pharmgkb_file = require_annotation_file(
        pharmgkb_file, "vcf_pharmgkb_missing", "pharmgkb"
    )
    ann_out = str(Path(outdir, PHARMGKB_ANNOTATED))
    if pharmgkb_file.lower().endswith(VCF_SUFFIXES):
        annotate_vcf(
            ensure_vcf_prepared(input_file),
            ensure_vcf_prepared(pharmgkb_file),
            "INFO/PHARMGKB",
            ann_out,
            outdir,
            "PharmGKB",
            threads=params.threads,
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
            threads=params.threads,
        )
    record_annotated(ann_out)
    logging.info(f"PharmGKB annotation complete: {ann_out}")


cmd_spliceai = vcf_input_handler(VcfSpliceaiParams, run_spliceai)
cmd_alphamissense = vcf_input_handler(VcfAlphamissenseParams, run_alphamissense)
cmd_pharmgkb = vcf_input_handler(VcfPharmgkbParams, run_pharmgkb)
