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


def cmd_phylop(args: argparse.Namespace) -> None:
    verify_dependencies(["bcftools", "tabix"])
    log_dependency_info(["bcftools", "tabix"])
    input_file, outdir, lib = annotation_context(args)
    logging.info(LOG_MESSAGES["vcf_phylop_start"].format(input=input_file))

    # Resolve PhyloP data file
    phylop_file = args.phylop_file if args.phylop_file else lib.phylop_file

    _exit_if_missing(phylop_file, "vcf_phylop_missing", "phylop")
    phylop_file = str(phylop_file)

    logging.info(LOG_MESSAGES["vcf_phylop_resolve"].format(path=phylop_file))

    # 1. Prepare Inputs
    input_vcf = ensure_vcf_prepared(input_file)
    phylop_vcf = prepare_tabix_annotation(phylop_file, "PhyloP")

    normalized_input, needs_cleanup = normalize_to_annotation_chroms(
        input_vcf, phylop_vcf, outdir, "PhyloP", "input_phylop_norm.vcf.gz"
    )

    # 3. Annotate with PhyloP
    ann_out = os.path.join(outdir, "phylop_annotated.vcf.gz")
    header_tmp = None
    try:
        # Annovar PhyloP TSV: #Chr, Start, End, Score
        # We want CHROM=1, POS=2, Score=4
        cols = "CHROM,POS,-,INFO/PHYLOP"
        annotate_args = [
            "bcftools",
            "annotate",
            "-a",
            phylop_vcf,
            "-Oz",
            "-o",
            ann_out,
        ]

        if phylop_vcf.lower().endswith((".vcf", ".vcf.gz")):
            annotate_args.extend(["-c", "INFO/PHYLOP"])
        else:
            import tempfile

            fd, header_tmp = tempfile.mkstemp(suffix=".hdr", dir=outdir)
            with os.fdopen(fd, "w") as f:
                f.write(
                    '##INFO=<ID=PHYLOP,Number=1,Type=Float,Description="PhyloP conservation score">\n'
                )
            annotate_args.extend(["-c", cols, "-h", header_tmp])

        annotate_args.append(normalized_input)
        run_command(annotate_args, capture_output=True)
        ensure_vcf_indexed(ann_out)
    except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
        logging.error(f"PhyloP annotation failed: {e}")
        raise WGSExtractError("VCF processing failed.") from None
    finally:
        cleanup_annotation_temporaries(header_tmp, normalized_input, needs_cleanup)

    run_min_score_filter(
        ann_out,
        outdir,
        args.min_score,
        "PHYLOP",
        "phylop",
        "vcf_phylop_filtering",
        "vcf_phylop_done",
        "PhyloP",
    )


def cmd_gnomad(args: argparse.Namespace) -> None:
    verify_dependencies(["bcftools", "tabix"])
    log_dependency_info(["bcftools", "tabix"])
    input_file, outdir, lib = annotation_context(args)
    logging.info(f"Annotating VCF with gnomAD data: {input_file}")

    # Resolve gnomAD VCF
    gnomad_file = args.gnomad_file if args.gnomad_file else lib.gnomad_vcf

    _exit_if_missing(gnomad_file, "vcf_gnomad_missing", "gnomad")

    logging.info(f"Using gnomAD file: {gnomad_file}")

    # 1. Prepare Inputs
    input_vcf = ensure_vcf_prepared(input_file)
    gnomad_vcf = ensure_vcf_prepared(gnomad_file)

    # 2. Match chromosome styles (chr1 vs 1)
    # Reuse normalization logic if possible, or just call normalize_vcf_chromosomes
    from wgsextract_cli.core.variant_files import normalize_vcf_chromosomes

    try:
        res_g = run_command(
            ["bcftools", "index", "-s", gnomad_vcf], capture_output=True
        )
        g_chroms = [line.split("\t")[0] for line in res_g.stdout.strip().split("\n")]
        normalized_input = normalize_vcf_chromosomes(input_vcf, g_chroms)
    except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
        logging.debug(f"Chromosome normalization check failed: {e}")
        normalized_input = input_vcf

    # 3. Annotate with gnomAD
    # We'll transfer AF (Allele Frequency) as GNOMAD_AF to avoid collisions
    ann_out = os.path.join(outdir, "gnomad_annotated.vcf.gz")
    header_tmp = None
    try:
        import tempfile

        fd, header_tmp = tempfile.mkstemp(suffix=".hdr", dir=outdir)
        with os.fdopen(fd, "w") as f:
            f.write(
                '##INFO=<ID=GNOMAD_AF,Number=A,Type=Float,Description="gnomAD Allele Frequency">\n'
            )

        run_command(
            [
                "bcftools",
                "annotate",
                "-a",
                gnomad_vcf,
                "-h",
                header_tmp,
                "-c",
                "INFO/GNOMAD_AF:=INFO/AF",
                "-Oz",
                "-o",
                ann_out,
                normalized_input,
            ],
            capture_output=True,
        )
        ensure_vcf_indexed(ann_out)
    except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
        logging.error(f"gnomAD annotation failed: {e}")
        raise WGSExtractError("VCF processing failed.") from None
    finally:
        if header_tmp and os.path.exists(header_tmp):
            os.remove(header_tmp)
        if normalized_input != input_vcf and os.path.exists(normalized_input):
            os.remove(normalized_input)
            if os.path.exists(normalized_input + ".tbi"):
                os.remove(normalized_input + ".tbi")

    # 4. Optional Filtering
    if args.max_af is not None:
        filter_out = os.path.join(outdir, f"gnomad_af_lt_{args.max_af}.vcf.gz")
        logging.info(
            f"Filtering for variants with gnomAD Allele Frequency < {args.max_af} to {filter_out}"
        )
        try:
            # Note: bcftools filter handles missing values (not in gnomAD) by excluding them by default
            # unless we explicitly ask to keep them. Common practice for 'rare' filtering is
            # to keep anything with AF < threshold OR AF is missing.
            filter_expr = f"GNOMAD_AF < {args.max_af} || GNOMAD_AF='.'"
            run_command(
                [
                    "bcftools",
                    "filter",
                    "-i",
                    filter_expr,
                    "-Oz",
                    "-o",
                    filter_out,
                    ann_out,
                ],
                capture_output=True,
            )
            ensure_vcf_indexed(filter_out)
            logging.info(f"gnomAD filtering complete: {filter_out}")
        except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
            logging.error(f"gnomAD filtering failed: {e}")
            raise WGSExtractError("gnomAD filtering failed.") from e
    else:
        logging.info(LOG_MESSAGES["vcf_gnomad_done"].format(output=ann_out))
