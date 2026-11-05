import logging
import tempfile
from pathlib import Path

from wgsextract_cli.api.vcf import VcfCaddParams, VcfGerpParams
from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.results import warn
from wgsextract_cli.core.variant_files import (
    ensure_vcf_prepared,
)

from .annotation_helpers import (
    annotate_vcf,
    annotation_context,
    prepare_tabix_annotation,
    require_annotation_file,
    resource,
    run_min_score_filter,
)
from .basic import vcf_input_handler
from .bigwig_scores import annotate_from_bigwig

CADD_ANNOTATED = "cadd_annotated.vcf.gz"
# CADD TSV: #Chrom, Pos, Ref, Alt, RawScore, PHRED (one row per allele)
_CADD_COLUMNS = "CHROM,POS,REF,ALT,INFO/CADD_RAW,INFO/CADD_PHRED"
_CADD_HEADER = [
    '##INFO=<ID=CADD_RAW,Number=A,Type=Float,Description="CADD v1.7 raw score">',
    "##INFO=<ID=CADD_PHRED,Number=A,Type=Float,"
    'Description="CADD v1.7 PHRED-scaled score">',
]
GERP_ANNOTATED = "gerp_annotated.vcf.gz"


def run_cadd(params: VcfCaddParams) -> None:
    verify_dependencies(["bcftools", "tabix"])
    log_dependency_info(["bcftools", "tabix"])
    input_file, outdir, lib = annotation_context(params)
    logging.info(LOG_MESSAGES["vcf_cadd_start"].format(input=input_file))

    snv_file = require_annotation_file(
        resource(params.cadd_file, lib.cadd_file), "vcf_cadd_missing", "cadd"
    )
    indel_file = resource(params.cadd_indel_file, lib.cadd_indel_file)
    logging.info(LOG_MESSAGES["vcf_cadd_resolve"].format(path=snv_file))
    if indel_file:
        logging.info(LOG_MESSAGES["vcf_cadd_resolve"].format(path=indel_file))
    else:
        warn("no_indel_scores", LOG_MESSAGES["vcf_cadd_no_indels"])

    ann_out = str(Path(outdir, CADD_ANNOTATED))
    snv_tsv = prepare_tabix_annotation(snv_file, "CADD")
    vcf_in = ensure_vcf_prepared(input_file)
    if not indel_file:
        annotate_vcf(
            vcf_in,
            snv_tsv,
            _CADD_COLUMNS,
            ann_out,
            outdir,
            "CADD",
            _CADD_HEADER,
            threads=params.threads,
        )
    else:
        indel_tsv = prepare_tabix_annotation(indel_file, "CADD indel")
        # Number=A values merge per allele, so the indel pass keeps SNV scores.
        with tempfile.TemporaryDirectory(dir=outdir, prefix=".wgse_cadd_") as work:
            snv_out = str(Path(work, "snv.vcf.gz"))
            annotate_vcf(
                vcf_in,
                snv_tsv,
                _CADD_COLUMNS,
                snv_out,
                work,
                "CADD",
                _CADD_HEADER,
                threads=params.threads,
            )
            annotate_vcf(
                snv_out,
                indel_tsv,
                _CADD_COLUMNS,
                ann_out,
                work,
                "CADD indel",
                threads=params.threads,
            )

    run_min_score_filter(
        ann_out,
        outdir,
        params.min_score,
        "CADD_PHRED",
        "cadd",
        "CADD",
        "vcf_cadd_done",
    )


def run_gerp(params: VcfGerpParams) -> None:
    verify_dependencies(["bcftools", "tabix", "bgzip"])
    log_dependency_info(["bcftools", "tabix", "bgzip"])
    input_file, outdir, lib = annotation_context(params)
    logging.info(LOG_MESSAGES["vcf_gerp_start"].format(input=input_file))

    gerp_file = require_annotation_file(
        resource(params.gerp_file, lib.gerp_file), "vcf_gerp_missing", "gerp"
    )
    logging.info(LOG_MESSAGES["vcf_gerp_resolve"].format(path=gerp_file))
    ann_out = str(Path(outdir, GERP_ANNOTATED))
    annotate_from_bigwig(
        ensure_vcf_prepared(input_file),
        gerp_file,
        "GERP",
        "GERP",
        "GERP++ RS conservation score",
        ann_out,
        outdir,
        threads=params.threads,
    )

    run_min_score_filter(
        ann_out, outdir, params.min_score, "GERP", "gerp", "GERP", "vcf_gerp_done"
    )


cmd_cadd = vcf_input_handler(VcfCaddParams, run_cadd)
cmd_gerp = vcf_input_handler(VcfGerpParams, run_gerp)
