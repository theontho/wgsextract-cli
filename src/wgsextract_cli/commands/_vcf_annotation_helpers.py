import logging
import os
import subprocess
import tempfile

from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.reference_resolver import ReferenceLibrary
from wgsextract_cli.core.utils import WGSExtractError, run_command
from wgsextract_cli.core.variant_files import (
    calculate_bam_md5,
    chromosome_rename_mapping,
    ensure_vcf_indexed,
)


def annotation_context(args) -> tuple[str, str, ReferenceLibrary]:
    input_file = getattr(args, "input", None) or getattr(args, "vcf_input", None)
    if not input_file:
        logging.error(LOG_MESSAGES["input_required"])
        raise WGSExtractError("VCF processing failed.") from None

    outdir = getattr(args, "outdir", None) or os.path.dirname(
        os.path.abspath(input_file)
    )
    md5_sig = (
        calculate_bam_md5(input_file, None)
        if input_file.lower().endswith((".bam", ".cram"))
        else None
    )
    lib = ReferenceLibrary(getattr(args, "ref", None), md5_sig, input_path=input_file)
    return input_file, outdir, lib


def normalize_to_annotation_chroms(
    input_vcf: str,
    annotation_vcf: str,
    outdir: str,
    label: str,
    output_name: str,
) -> tuple[str, bool]:
    normalized_input = input_vcf
    needs_cleanup = False
    try:
        res_v = run_command(["bcftools", "index", "-s", input_vcf], capture_output=True)
        v_chroms = [line.split("\t")[0] for line in res_v.stdout.strip().split("\n")]

        if annotation_vcf.lower().endswith((".vcf", ".vcf.gz")):
            res_ann = run_command(
                ["bcftools", "index", "-s", annotation_vcf], capture_output=True
            )
            ann_chroms = [
                line.split("\t")[0] for line in res_ann.stdout.strip().split("\n")
            ]
        else:
            res_ann = run_command(["tabix", "-l", annotation_vcf], capture_output=True)
            ann_chroms = res_ann.stdout.strip().split("\n")

        chrom_mapping = chromosome_rename_mapping(v_chroms, ann_chroms)
        if chrom_mapping:
            norm_out = os.path.join(outdir, output_name)
            logging.info("Normalizing chromosome naming for %s", label)
            fd, map_path = tempfile.mkstemp(suffix=".map", dir=outdir)
            try:
                with os.fdopen(fd, "w") as f:
                    for source, target in chrom_mapping:
                        f.write(f"{source} {target}\n")

                run_command(
                    [
                        "bcftools",
                        "annotate",
                        "--rename-chrs",
                        map_path,
                        "-Oz",
                        "-o",
                        norm_out,
                        input_vcf,
                    ],
                    check=True,
                )
            finally:
                if os.path.exists(map_path):
                    os.remove(map_path)
            ensure_vcf_indexed(norm_out)
            normalized_input = norm_out
            needs_cleanup = True
    except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
        logging.warning(
            "%s chromosome normalization failed; annotation may miss variants: %s",
            label,
            e,
        )
    return normalized_input, needs_cleanup


def cleanup_annotation_temporaries(
    header_tmp: str | None,
    normalized_input: str,
    needs_cleanup: bool,
) -> None:
    if header_tmp and os.path.exists(header_tmp):
        os.remove(header_tmp)
    if needs_cleanup and os.path.exists(normalized_input):
        os.remove(normalized_input)
        if os.path.exists(normalized_input + ".tbi"):
            os.remove(normalized_input + ".tbi")


def run_min_score_filter(
    ann_out: str,
    outdir: str,
    min_score: object | None,
    info_field: str,
    output_prefix: str,
    log_filter_key: str,
    log_done_key: str,
    label: str,
) -> None:
    if min_score is None:
        logging.info(LOG_MESSAGES[log_done_key].format(output=ann_out))
        return

    path_out = os.path.join(outdir, f"{output_prefix}_gt_{min_score}.vcf.gz")
    logging.info(
        LOG_MESSAGES[log_filter_key].format(min_score=min_score, output=path_out)
    )
    try:
        run_command(
            [
                "bcftools",
                "filter",
                "-i",
                f"{info_field} >= {min_score}",
                "-Oz",
                "-o",
                path_out,
                ann_out,
            ],
            capture_output=True,
        )
        ensure_vcf_indexed(path_out)
        logging.info(LOG_MESSAGES[log_done_key].format(output=path_out))
    except Exception as e:
        logging.error("%s filtering failed: %s", label, e)
        raise WGSExtractError(f"{label} filtering failed.") from e
