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


def cmd_clinvar(args):
    verify_dependencies(["bcftools", "tabix"])
    log_dependency_info(["bcftools", "tabix"])
    input_file = args.input if args.input else args.vcf_input
    if not input_file:
        logging.error(LOG_MESSAGES["input_required"])
        raise WGSExtractError("VCF processing failed.")

    outdir = (
        args.outdir if args.outdir else os.path.dirname(os.path.abspath(input_file))
    )
    logging.info(LOG_MESSAGES["vcf_clinvar_start"].format(input=input_file))

    # Resolve ClinVar VCF
    md5_sig = (
        calculate_bam_md5(input_file, None)
        if input_file.lower().endswith((".bam", ".cram"))
        else None
    )
    lib = ReferenceLibrary(args.ref, md5_sig, input_path=input_file)
    clinvar_vcf = args.clinvar_file if args.clinvar_file else lib.clinvar_vcf

    _exit_if_missing(clinvar_vcf, "vcf_clinvar_missing", "clinvar")

    logging.info(LOG_MESSAGES["vcf_clinvar_resolve"].format(path=clinvar_vcf))

    # 1. Prepare Inputs
    input_vcf = ensure_vcf_prepared(input_file)
    clinvar_prepared = ensure_vcf_prepared(clinvar_vcf)

    # 2. Match chromosome styles (chr1 vs 1)
    from wgsextract_cli.core.variant_files import normalize_vcf_chromosomes

    try:
        res_c = run_command(
            ["bcftools", "index", "-s", clinvar_prepared],
            capture_output=True,
        )
        c_chroms = [line.split("\t")[0] for line in res_c.stdout.strip().split("\n")]
        normalized_input = normalize_vcf_chromosomes(input_vcf, c_chroms)
    except Exception:
        normalized_input = input_vcf

    # 3. Annotate with ClinVar
    # We transfer CLNSIG (Significance) and CLNDN (Disease Name)
    ann_out = os.path.join(outdir, "clinvar_annotated.vcf.gz")
    try:
        run_command(
            [
                "bcftools",
                "annotate",
                "-a",
                clinvar_prepared,
                "-c",
                "CLNSIG,CLNDN",
                "-Oz",
                "-o",
                ann_out,
                normalized_input,
            ],
            capture_output=True,
        )
        ensure_vcf_indexed(ann_out)
    except Exception as e:
        logging.error(f"ClinVar annotation failed: {e}")
        raise WGSExtractError("VCF processing failed.") from e
    finally:
        if normalized_input != input_vcf and os.path.exists(normalized_input):
            os.remove(normalized_input)

    # 4. Filter for Pathogenic
    path_out = os.path.join(outdir, "clinvar_pathogenic.vcf.gz")
    logging.info(LOG_MESSAGES["vcf_clinvar_filtering"].format(output=path_out))
    try:
        # Filter for Pathogenic or Likely_pathogenic in CLNSIG
        # The exact string can vary slightly, so we use a regex/substring match
        filter_expr = 'CLNSIG ~ "Pathogenic" || CLNSIG ~ "Likely_pathogenic"'
        run_command(
            ["bcftools", "filter", "-i", filter_expr, "-Oz", "-o", path_out, ann_out],
            capture_output=True,
        )
        ensure_vcf_indexed(path_out)
        logging.info(LOG_MESSAGES["vcf_clinvar_done"].format(output=path_out))
    except Exception as e:
        logging.error(f"ClinVar filtering failed: {e}")
        raise WGSExtractError("ClinVar filtering failed.") from e


def cmd_revel(args):
    verify_dependencies(["bcftools", "tabix"])
    log_dependency_info(["bcftools", "tabix"])
    input_file = args.input if args.input else args.vcf_input
    if not input_file:
        logging.error(LOG_MESSAGES["input_required"])
        raise WGSExtractError("VCF processing failed.")

    outdir = (
        args.outdir if args.outdir else os.path.dirname(os.path.abspath(input_file))
    )
    logging.info(LOG_MESSAGES["vcf_revel_start"].format(input=input_file))

    # Resolve REVEL data file
    md5_sig = (
        calculate_bam_md5(input_file, None)
        if input_file.lower().endswith((".bam", ".cram"))
        else None
    )
    lib = ReferenceLibrary(args.ref, md5_sig, input_path=input_file)
    revel_file = args.revel_file if args.revel_file else lib.revel_file

    _exit_if_missing(revel_file, "vcf_revel_missing", "revel")

    logging.info(LOG_MESSAGES["vcf_revel_resolve"].format(path=revel_file))

    # 1. Prepare Inputs
    input_vcf = ensure_vcf_prepared(input_file)
    revel_vcf = ensure_vcf_prepared(revel_file)

    # 2. Match chromosome styles (chr1 vs 1)
    normalized_input = input_vcf
    needs_cleanup = False
    try:
        res_v = run_command(["bcftools", "index", "-s", input_vcf], capture_output=True)
        v_chroms = [line.split("\t")[0] for line in res_v.stdout.strip().split("\n")]

        if revel_vcf.lower().endswith((".vcf", ".vcf.gz")):
            res_r = run_command(
                ["bcftools", "index", "-s", revel_vcf], capture_output=True
            )
            r_chroms = [
                line.split("\t")[0] for line in res_r.stdout.strip().split("\n")
            ]
        else:
            res_r = run_command(["tabix", "-l", revel_vcf], capture_output=True)
            r_chroms = res_r.stdout.strip().split("\n")

        v_has_chr = any(c.startswith("chr") for c in v_chroms)
        r_has_chr = any(c.startswith("chr") for c in r_chroms if c)

        if v_has_chr != r_has_chr:
            import tempfile

            fd, map_path = tempfile.mkstemp(suffix=".map", dir=outdir)
            with os.fdopen(fd, "w") as f:
                for vc in v_chroms:
                    if v_has_chr and not r_has_chr:
                        rc = vc[3:] if vc.startswith("chr") else vc
                        if rc == "MT":
                            rc = "M"
                        f.write(f"{vc} {rc}\n")
                    elif not v_has_chr and r_has_chr:
                        rc = "chr" + vc
                        if rc == "chrMT":
                            rc = "chrM"
                        f.write(f"{vc} {rc}\n")

            norm_out = os.path.join(outdir, "input_revel_norm.vcf.gz")
            logging.info(
                f"Normalizing chromosome naming for REVEL: {'chr1 -> 1' if v_has_chr else '1 -> chr1'}"
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

    # 3. Annotate with REVEL
    ann_out = os.path.join(outdir, "revel_annotated.vcf.gz")
    header_tmp = None
    try:
        # Annovar REVEL TSV: #Chr, Start, End, Ref, Alt, REVEL
        cols = "CHROM,POS,-,REF,ALT,INFO/REVEL"
        annotate_args = [
            "bcftools",
            "annotate",
            "-a",
            revel_vcf,
            "-Oz",
            "-o",
            ann_out,
        ]

        if revel_vcf.lower().endswith((".vcf", ".vcf.gz")):
            annotate_args.extend(["-c", "INFO/REVEL"])
        else:
            import tempfile

            fd, header_tmp = tempfile.mkstemp(suffix=".hdr", dir=outdir)
            with os.fdopen(fd, "w") as f:
                f.write(
                    '##INFO=<ID=REVEL,Number=1,Type=Float,Description="REVEL score">\n'
                )
            annotate_args.extend(["-c", cols, "-h", header_tmp])

        annotate_args.append(normalized_input)
        run_command(annotate_args, capture_output=True)
        ensure_vcf_indexed(ann_out)
    except Exception as e:
        logging.error(f"REVEL annotation failed: {e}")
        raise WGSExtractError("VCF processing failed.") from e
    finally:
        if header_tmp and os.path.exists(header_tmp):
            os.remove(header_tmp)
        if needs_cleanup and os.path.exists(normalized_input):
            os.remove(normalized_input)
            if os.path.exists(normalized_input + ".tbi"):
                os.remove(normalized_input + ".tbi")

    # 4. Optional Filtering
    if args.min_score is not None:
        path_out = os.path.join(outdir, f"revel_gt_{args.min_score}.vcf.gz")
        logging.info(
            LOG_MESSAGES["vcf_revel_filtering"].format(
                min_score=args.min_score, output=path_out
            )
        )
        try:
            filter_expr = f"REVEL >= {args.min_score}"
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
            logging.info(LOG_MESSAGES["vcf_revel_done"].format(output=path_out))
        except Exception as e:
            logging.error(f"REVEL filtering failed: {e}")
            raise WGSExtractError("REVEL filtering failed.") from e
    else:
        logging.info(LOG_MESSAGES["vcf_revel_done"].format(output=ann_out))
