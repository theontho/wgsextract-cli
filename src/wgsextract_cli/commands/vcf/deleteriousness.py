import argparse
import logging
import os
import subprocess
import tempfile
from collections.abc import Iterator
from typing import Any

from wgsextract_cli.core.annotation_resources import open_bigwig
from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.utils import WGSExtractError, popen, run_command
from wgsextract_cli.core.variant_files import (
    check_vcf_build,
    ensure_vcf_prepared,
    match_chromosome_name,
    vcf_index_chromosomes,
)

from .annotation_helpers import (
    annotate_vcf,
    annotation_context,
    prepare_tabix_annotation,
    require_annotation_file,
    run_min_score_filter,
)

CADD_ANNOTATED = "cadd_annotated.vcf.gz"
# CADD TSV: #Chrom, Pos, Ref, Alt, RawScore, PHRED (one row per allele)
_CADD_COLUMNS = "CHROM,POS,REF,ALT,INFO/CADD_RAW,INFO/CADD_PHRED"
_CADD_HEADER = [
    '##INFO=<ID=CADD_RAW,Number=A,Type=Float,Description="CADD v1.7 raw score">',
    "##INFO=<ID=CADD_PHRED,Number=A,Type=Float,"
    'Description="CADD v1.7 PHRED-scaled score">',
]
GERP_ANNOTATED = "gerp_annotated.vcf.gz"
_GERP_COLUMNS = "CHROM,POS,REF,ALT,INFO/GERP"
_GERP_HEADER = [
    "##INFO=<ID=GERP,Number=1,Type=Float,"
    'Description="GERP++ RS conservation score (maximum over the REF bases)">',
]


def cmd_cadd(args: argparse.Namespace) -> None:
    verify_dependencies(["bcftools", "tabix"])
    log_dependency_info(["bcftools", "tabix"])
    input_file, outdir, lib = annotation_context(args)
    logging.info(LOG_MESSAGES["vcf_cadd_start"].format(input=input_file))

    snv_file = require_annotation_file(
        args.cadd_file or lib.cadd_file, "vcf_cadd_missing", "cadd"
    )
    indel_file = args.cadd_indel_file or lib.cadd_indel_file
    logging.info(LOG_MESSAGES["vcf_cadd_resolve"].format(path=snv_file))
    if indel_file:
        logging.info(LOG_MESSAGES["vcf_cadd_resolve"].format(path=indel_file))
    else:
        logging.warning(LOG_MESSAGES["vcf_cadd_no_indels"])

    ann_out = os.path.join(outdir, CADD_ANNOTATED)
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
            threads=getattr(args, "threads", None),
        )
    else:
        indel_tsv = prepare_tabix_annotation(indel_file, "CADD indel")
        # Number=A values merge per allele, so the indel pass keeps SNV scores.
        with tempfile.TemporaryDirectory(dir=outdir, prefix=".wgse_cadd_") as work:
            snv_out = os.path.join(work, "snv.vcf.gz")
            annotate_vcf(
                vcf_in,
                snv_tsv,
                _CADD_COLUMNS,
                snv_out,
                work,
                "CADD",
                _CADD_HEADER,
                threads=getattr(args, "threads", None),
            )
            annotate_vcf(
                snv_out,
                indel_tsv,
                _CADD_COLUMNS,
                ann_out,
                work,
                "CADD indel",
                threads=getattr(args, "threads", None),
            )

    run_min_score_filter(
        ann_out,
        outdir,
        args.min_score,
        "CADD_PHRED",
        "cadd",
        "CADD",
        "vcf_cadd_done",
    )


def _gerp_rows(vcf: str, bw: Any) -> Iterator[str]:
    """CHROM POS REF ALT GERP rows (the VCF's contig names) for its variant records,
    scoring each with the maximum GERP over its REF bases."""
    bw_lengths: dict[str, int] = bw.chroms()
    to_bw = {
        chrom: match_chromosome_name(chrom, bw_lengths)
        for chrom in vcf_index_chromosomes(vcf)
    }
    if not any(to_bw.values()):
        raise WGSExtractError(
            "None of the VCF's contigs are in the GERP scores; check its genome build."
        )
    with tempfile.TemporaryFile() as err:
        proc = popen(
            ["bcftools", "query", "-f", r"%CHROM\t%POS\t%REF\t%ALT\n", vcf],
            stdout=subprocess.PIPE,
            stderr=err,
            text=True,
        )
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                chrom, pos, ref, alt = line.rstrip("\n").split("\t")
                bw_chrom = to_bw.get(chrom)
                # Skip gVCF reference blocks and symbolic (structural) alleles.
                if not bw_chrom or all(
                    a in (".", "*") or a.startswith("<") for a in alt.split(",")
                ):
                    continue
                start = int(pos) - 1
                end = min(start + len(ref), bw_lengths[bw_chrom])
                if start >= end:
                    continue
                score = bw.stats(bw_chrom, start, end, type="max", exact=True)[0]
                if score is not None:
                    yield f"{chrom}\t{pos}\t{ref}\t{alt}\t{score:.4g}\n"
            if proc.wait() != 0:
                err.seek(0)
                raise WGSExtractError(
                    f"bcftools query failed on {vcf}: {err.read().decode().strip()}"
                )
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()


def cmd_gerp(args: argparse.Namespace) -> None:
    verify_dependencies(["bcftools", "tabix", "bgzip"])
    log_dependency_info(["bcftools", "tabix", "bgzip"])
    input_file, outdir, lib = annotation_context(args)
    logging.info(LOG_MESSAGES["vcf_gerp_start"].format(input=input_file))

    gerp_file = require_annotation_file(
        args.gerp_file or lib.gerp_file, "vcf_gerp_missing", "gerp"
    )
    logging.info(LOG_MESSAGES["vcf_gerp_resolve"].format(path=gerp_file))
    vcf_in = ensure_vcf_prepared(input_file)
    ann_out = os.path.join(outdir, GERP_ANNOTATED)
    bw = open_bigwig(gerp_file)
    try:
        check_vcf_build(vcf_in, bw.chroms(), "the GERP scores")
        with tempfile.TemporaryDirectory(dir=outdir, prefix=".wgse_gerp_") as work:
            scores = os.path.join(work, "gerp.tsv.gz")
            with open(scores, "wb") as out:
                bgzip = popen(
                    ["bgzip", "-c"], stdin=subprocess.PIPE, stdout=out, text=True
                )
                assert bgzip.stdin is not None
                try:
                    bgzip.stdin.writelines(_gerp_rows(vcf_in, bw))
                finally:
                    bgzip.stdin.close()
                    if bgzip.wait() != 0:
                        raise WGSExtractError("bgzip failed writing GERP scores.")
            run_command(["tabix", "-s1", "-b2", "-e2", scores], capture_output=True)
            annotate_vcf(
                vcf_in,
                scores,
                _GERP_COLUMNS,
                ann_out,
                work,
                "GERP",
                _GERP_HEADER,
                threads=getattr(args, "threads", None),
            )
    finally:
        bw.close()

    run_min_score_filter(
        ann_out, outdir, args.min_score, "GERP", "gerp", "GERP", "vcf_gerp_done"
    )
