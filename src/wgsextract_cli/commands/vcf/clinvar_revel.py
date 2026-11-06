import logging
import subprocess
from pathlib import Path

from wgsextract_cli.api.vcf import VcfClinvarParams, VcfRevelParams
from wgsextract_cli.core import variant_confidence
from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.utils import WGSExtractError, run_command
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

CLINVAR_ANNOTATED = "clinvar_annotated.vcf.gz"
CLINVAR_PATHOGENIC = "clinvar_pathogenic.vcf.gz"
# GENEINFO (SYMBOL:GeneID|...) names the genes ClinVar placed the variant in.
CLINVAR_COLUMNS = ("CLNSIG", "CLNDN", "GENEINFO")
REVEL_ANNOTATED = "revel_annotated.vcf.gz"


def run_clinvar(params: VcfClinvarParams) -> None:
    verify_dependencies(["bcftools", "tabix"])
    log_dependency_info(["bcftools", "tabix"])
    input_file, outdir, lib = annotation_context(params)
    logging.info(LOG_MESSAGES["vcf_clinvar_start"].format(input=input_file))

    clinvar_vcf = require_annotation_file(
        resource(params.clinvar_file, lib.clinvar_vcf), "vcf_clinvar_missing", "clinvar"
    )
    logging.info(LOG_MESSAGES["vcf_clinvar_resolve"].format(path=clinvar_vcf))

    # Transfer CLNSIG (significance), CLNDN (disease name) and, when the
    # ClinVar file has it, GENEINFO. The input's own GENEINFO is dropped, so
    # any left names ClinVar's genes.
    clinvar_vcf = ensure_vcf_prepared(clinvar_vcf)
    input_vcf = ensure_vcf_prepared(input_file)
    geneinfo = "##INFO=<ID=GENEINFO,"
    columns = [
        c
        for c in CLINVAR_COLUMNS
        if c != "GENEINFO" or geneinfo in _vcf_header(clinvar_vcf)
    ]
    ann_out = str(Path(outdir, CLINVAR_ANNOTATED))
    annotate_vcf(
        input_vcf,
        clinvar_vcf,
        ",".join(columns),
        ann_out,
        outdir,
        "ClinVar",
        threads=params.threads,
        remove=("INFO/GENEINFO",) if geneinfo in _vcf_header(input_vcf) else (),
    )

    path_out = str(Path(outdir, CLINVAR_PATHOGENIC))
    logging.info(LOG_MESSAGES["vcf_clinvar_filtering"].format(output=path_out))
    # Substring match also covers Pathogenic/Likely_pathogenic combinations.
    try:
        summary = variant_confidence.label_vcf(
            ann_out,
            path_out,
            variant_confidence.thresholds_from(params),
            include='CLNSIG ~ "Pathogenic" || CLNSIG ~ "Likely_pathogenic"',
            summary_columns=CLINVAR_COLUMNS,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(f"ClinVar filtering failed: {e}") from e
    table = str(Path(outdir, "clinvar_pathogenic.confidence.tsv"))
    variant_confidence.write_table(
        table, summary, CLINVAR_COLUMNS, "ClinVar pathogenic/likely pathogenic calls"
    )
    variant_confidence.record(
        summary,
        output=path_out,
        role="clinvar_pathogenic",
        what="ClinVar pathogenic/likely pathogenic calls",
        table=table,
        clinical=summary.carried,
    )
    record_annotated(ann_out)
    logging.info(LOG_MESSAGES["vcf_clinvar_done"].format(output=path_out))


def _vcf_header(vcf: str) -> str:
    return run_command(["bcftools", "view", "-h", vcf], capture_output=True).stdout


def run_revel(params: VcfRevelParams) -> None:
    verify_dependencies(["bcftools", "tabix"])
    log_dependency_info(["bcftools", "tabix"])
    input_file, outdir, lib = annotation_context(params)
    logging.info(LOG_MESSAGES["vcf_revel_start"].format(input=input_file))

    revel_file = require_annotation_file(
        resource(params.revel_file, lib.revel_file), "vcf_revel_missing", "revel"
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
            threads=params.threads,
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
            threads=params.threads,
        )

    run_min_score_filter(
        ann_out, outdir, params.min_score, "REVEL", "revel", "REVEL", "vcf_revel_done"
    )


cmd_clinvar = vcf_input_handler(VcfClinvarParams, run_clinvar)
cmd_revel = vcf_input_handler(VcfRevelParams, run_revel)
