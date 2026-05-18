import logging
import os

from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.reference_resolver import ReferenceLibrary
from wgsextract_cli.core.utils import (
    WGSExtractError,
    run_command,
)
from wgsextract_cli.core.variant_files import (
    calculate_bam_md5,
    ensure_vcf_indexed,
    ensure_vcf_prepared,
)

from ._vcf_structural import (
    _exit_if_missing,
)


def cmd_phylop(args):
    verify_dependencies(["bcftools", "tabix"])
    log_dependency_info(["bcftools", "tabix"])
    input_file = args.input if args.input else args.vcf_input
    if not input_file:
        logging.error(LOG_MESSAGES["input_required"])
        raise WGSExtractError("VCF processing failed.") from None

    outdir = (
        args.outdir if args.outdir else os.path.dirname(os.path.abspath(input_file))
    )
    logging.info(LOG_MESSAGES["vcf_phylop_start"].format(input=input_file))

    # Resolve PhyloP data file
    md5_sig = (
        calculate_bam_md5(input_file, None)
        if input_file.lower().endswith((".bam", ".cram"))
        else None
    )
    lib = ReferenceLibrary(args.ref, md5_sig, input_path=input_file)
    phylop_file = args.phylop_file if args.phylop_file else lib.phylop_file

    _exit_if_missing(phylop_file, "vcf_phylop_missing", "phylop")

    logging.info(LOG_MESSAGES["vcf_phylop_resolve"].format(path=phylop_file))

    # 1. Prepare Inputs
    input_vcf = ensure_vcf_prepared(input_file)
    phylop_vcf = ensure_vcf_prepared(phylop_file)

    # 2. Match chromosome styles (chr1 vs 1)
    normalized_input = input_vcf
    needs_cleanup = False
    try:
        res_v = run_command(["bcftools", "index", "-s", input_vcf], capture_output=True)
        v_chroms = [line.split("\t")[0] for line in res_v.stdout.strip().split("\n")]

        if phylop_vcf.lower().endswith((".vcf", ".vcf.gz")):
            res_p = run_command(
                ["bcftools", "index", "-s", phylop_vcf], capture_output=True
            )
            p_chroms = [
                line.split("\t")[0] for line in res_p.stdout.strip().split("\n")
            ]
        else:
            res_p = run_command(["tabix", "-l", phylop_vcf], capture_output=True)
            p_chroms = res_p.stdout.strip().split("\n")

        v_has_chr = any(c.startswith("chr") for c in v_chroms)
        p_has_chr = any(c.startswith("chr") for c in p_chroms if c)

        if v_has_chr != p_has_chr:
            import tempfile

            fd, map_path = tempfile.mkstemp(suffix=".map", dir=outdir)
            with os.fdopen(fd, "w") as f:
                for vc in v_chroms:
                    if v_has_chr and not p_has_chr:
                        pc = vc[3:] if vc.startswith("chr") else vc
                        if pc == "MT":
                            pc = "M"
                        f.write(f"{vc} {pc}\n")
                    elif not v_has_chr and p_has_chr:
                        pc = "chr" + vc
                        if pc == "chrMT":
                            pc = "chrM"
                        f.write(f"{vc} {pc}\n")

            norm_out = os.path.join(outdir, "input_phylop_norm.vcf.gz")
            logging.info(
                f"Normalizing chromosome naming for PhyloP: {'chr1 -> 1' if v_has_chr else '1 -> chr1'}"
            )
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
            ensure_vcf_indexed(norm_out)
            os.remove(map_path)
            normalized_input = norm_out
            needs_cleanup = True
    except Exception as e:
        logging.debug(f"Chromosome normalization failed: {e}")

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
    except Exception as e:
        logging.error(f"PhyloP annotation failed: {e}")
        raise WGSExtractError("VCF processing failed.") from None
    finally:
        if header_tmp and os.path.exists(header_tmp):
            os.remove(header_tmp)
        if needs_cleanup and os.path.exists(normalized_input):
            os.remove(normalized_input)
            if os.path.exists(normalized_input + ".tbi"):
                os.remove(normalized_input + ".tbi")

    # 4. Optional Filtering
    if args.min_score is not None:
        path_out = os.path.join(outdir, f"phylop_gt_{args.min_score}.vcf.gz")
        logging.info(
            LOG_MESSAGES["vcf_phylop_filtering"].format(
                min_score=args.min_score, output=path_out
            )
        )
        try:
            filter_expr = f"PHYLOP >= {args.min_score}"
            run_command(
                [
                    "bcftools",
                    "filter",
                    "-i",
                    filter_expr,
                    "-Oz",
                    "-o",
                    path_out,
                    ann_out,
                ],
                capture_output=True,
            )
            ensure_vcf_indexed(path_out)
            logging.info(LOG_MESSAGES["vcf_phylop_done"].format(output=path_out))
        except Exception as e:
            logging.error(f"PhyloP filtering failed: {e}")
            raise WGSExtractError("PhyloP filtering failed.") from e
    else:
        logging.info(LOG_MESSAGES["vcf_phylop_done"].format(output=ann_out))


def cmd_gnomad(args):
    verify_dependencies(["bcftools", "tabix"])
    log_dependency_info(["bcftools", "tabix"])
    input_file = args.input if args.input else args.vcf_input
    if not input_file:
        logging.error(LOG_MESSAGES["input_required"])
        raise WGSExtractError("VCF processing failed.") from None

    outdir = (
        args.outdir if args.outdir else os.path.dirname(os.path.abspath(input_file))
    )
    logging.info(f"Annotating VCF with gnomAD data: {input_file}")

    # Resolve gnomAD VCF
    md5_sig = (
        calculate_bam_md5(input_file, None)
        if input_file.lower().endswith((".bam", ".cram"))
        else None
    )
    lib = ReferenceLibrary(args.ref, md5_sig, input_path=input_file)
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
    except Exception as e:
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
    except Exception as e:
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
        except Exception as e:
            logging.error(f"gnomAD filtering failed: {e}")
            raise WGSExtractError("gnomAD filtering failed.") from e
    else:
        logging.info(LOG_MESSAGES["vcf_gnomad_done"].format(output=ann_out))
