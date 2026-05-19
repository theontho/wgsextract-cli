import logging
import os
import shlex
import shutil
import subprocess
import tempfile

from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.reference_resolver import ReferenceLibrary
from wgsextract_cli.core.utils import (
    WGSExtractError,
    get_resource_defaults,
    run_command,
)
from wgsextract_cli.core.variant_files import (
    calculate_bam_md5,
    ensure_vcf_indexed,
    popen,
)

from ._vep_resources import (
    preprocess_vcf_chr_prefix,
)


def cmd_vep(args):
    if getattr(args, "vep_cmd", None) == "download":
        return
    if not args.input:
        logging.error(LOG_MESSAGES["input_required"])
        return

    # Determine input files
    if os.path.isdir(args.input):
        input_files = [
            os.path.join(args.input, f)
            for f in os.listdir(args.input)
            if f.lower().endswith((".vcf", ".vcf.gz", ".bcf", ".bam", ".cram"))
        ]
        input_files.sort()
        if not input_files:
            logging.error(f"No valid genomic files found in {args.input}")
            return
    else:
        input_files = [args.input]

    verify_dependencies(["vep", "tabix", "bcftools"])
    log_dependency_info(["vep", "tabix", "bcftools"])
    threads, _ = get_resource_defaults(args.threads, None)

    batch_stats = []

    for current_input in input_files:
        is_vcf = current_input.lower().endswith((".vcf", ".vcf.gz", ".bcf"))
        is_bam = current_input.lower().endswith((".bam", ".cram"))

        if not is_vcf and not is_bam:
            continue

        outdir = (
            args.outdir
            if args.outdir
            else os.path.dirname(os.path.abspath(current_input))
        )
        os.makedirs(outdir, exist_ok=True)

        logging.debug(f"Input file: {os.path.abspath(current_input)}")
        logging.debug(f"Output directory: {os.path.abspath(outdir)}")

        # Output path determination
        base_name = os.path.basename(current_input).split(".")[0]
        out_ext = (
            ".vcf"
            if args.format == "vcf"
            else ".txt"
            if args.format == "tab"
            else ".json"
        )
        output_file = os.path.join(outdir, f"{base_name}_vep{out_ext}")
        final_output = output_file + ".gz" if args.format == "vcf" else output_file

        if os.path.exists(final_output) and not args.force:
            logging.info(
                LOG_MESSAGES["vep_skipping_exists"].format(
                    input=current_input, output=final_output
                )
            )
            batch_stats.append((os.path.basename(current_input), "SKIPPED", "-"))
            continue

        import time
        from datetime import timedelta

        start_time = time.time()

        if is_vcf:
            ensure_vcf_indexed(current_input)

        md5_sig = calculate_bam_md5(current_input, None) if is_bam else None
        lib = ReferenceLibrary(args.ref, md5_sig, input_path=current_input)

        # If user explicitly requested an assembly, ensure the resolved FASTA doesn't conflict
        if args.vep_assembly:
            forced_build = "hg38" if args.vep_assembly == "GRCh38" else "hg19"
            lib.build = forced_build
            if lib.fasta:
                f_lower = str(lib.fasta).lower()
                is_hg38_path = "hg38" in f_lower or "grch38" in f_lower
                is_hg19_path = (
                    "hg19" in f_lower or "grch37" in f_lower or "hs37d5" in f_lower
                )
                mismatch = (forced_build == "hg38" and is_hg19_path) or (
                    forced_build == "hg19" and is_hg38_path
                )
                if mismatch:
                    logging.warning(
                        f"Discarding auto-resolved FASTA '{lib.fasta}' because it conflicts with --vep-assembly {args.vep_assembly}"
                    )
                    lib.fasta = None

        resolved_ref = lib.fasta
        logging.debug(f"Resolved reference: {resolved_ref}")

        if is_bam and not resolved_ref:
            logging.error(
                LOG_MESSAGES["ref_required_for"].format(
                    task="variant calling from BAM/CRAM"
                )
            )
            batch_stats.append((os.path.basename(current_input), "FAILED", "-"))
            continue

        vep_input_vcf = current_input
        temp_dir = None

        try:
            # 1. Variant Calling (if BAM/CRAM)
            if is_bam:
                logging.info(LOG_MESSAGES["vep_calling_pre"])
                temp_dir = tempfile.mkdtemp(dir=outdir)
                temp_vcf = os.path.join(temp_dir, "variants.vcf.gz")
                region_args = ["-r", args.region] if args.region else []
                ploidy_args = []
                if args.ploidy_file:
                    ploidy_args = ["--ploidy-file", args.ploidy_file]
                elif args.ploidy:
                    ploidy_args = ["--ploidy", args.ploidy]
                elif lib.ploidy_file:
                    ploidy_args = ["--ploidy-file", lib.ploidy_file]
                else:
                    alias = "GRCh38" if lib.build == "hg38" else "GRCh37"
                    ploidy_args = ["--ploidy", alias]

                assert resolved_ref is not None
                bcftools = get_tool_path("bcftools")
                p1 = popen(
                    [
                        bcftools,
                        "mpileup",
                        "-B",
                        "-I",
                        "-C",
                        "50",
                        "-f",
                        resolved_ref,
                        "-Ou",
                    ]
                    + region_args
                    + [current_input],
                    stdout=subprocess.PIPE,
                )
                p2 = popen(
                    [bcftools, "call"]
                    + ploidy_args
                    + ["-mv", "-P", "0", "--threads", threads, "-Oz", "-o", temp_vcf],
                    stdin=p1.stdout,
                    stderr=subprocess.PIPE,
                )
                if p1.stdout:
                    p1.stdout.close()
                _, stderr = p2.communicate()
                mpileup_returncode = p1.wait()
                if mpileup_returncode != 0 or p2.returncode != 0:
                    logging.error(
                        "Variant calling failed with return codes "
                        f"mpileup={mpileup_returncode}, call={p2.returncode}: "
                        f"{stderr.decode() if stderr else ''}"
                    )
                    batch_stats.append((os.path.basename(current_input), "FAILED", "-"))
                    continue
                ensure_vcf_indexed(temp_vcf)
                vep_input_vcf = temp_vcf

            # 2. Preprocessing (add-chr)
            if args.add_chr:
                chr_temp_vcf = os.path.join(outdir, f"{base_name}_with_chr.vcf")
                preprocess_vcf_chr_prefix(vep_input_vcf, chr_temp_vcf)
                vep_input_vcf = chr_temp_vcf

            # 3. Determine VCF Type and specialized args
            vcf_type = args.vcf_type
            if vcf_type == "auto":
                fname_lower = current_input.lower()
                if "sv" in fname_lower:
                    vcf_type = "sv"
                elif "cnv" in fname_lower:
                    vcf_type = "cnv"
                else:
                    vcf_type = "snp-indel"

            # 4. Construct VEP command
            vep_path = get_tool_path("vep") or "vep"

            vep_cmd = shlex.split(vep_path)
            vep_cmd.extend(["-i", vep_input_vcf, "-o", output_file, "--fork", threads])

            if args.format == "vcf":
                vep_cmd.append("--vcf")
            elif args.format == "json":
                vep_cmd.append("--json")
            else:
                vep_cmd.append("--tab")

            # Assembly
            if args.vep_assembly:
                vep_cmd.extend(["--assembly", args.vep_assembly])
            elif lib.build:
                if "38" in lib.build:
                    vep_cmd.extend(["--assembly", "GRCh38"])
                elif "37" in lib.build or "19" in lib.build:
                    vep_cmd.extend(["--assembly", "GRCh37"])

            # Reference Fasta
            if resolved_ref:
                vep_cmd.extend(["--fasta", resolved_ref])

            # Cache settings
            cache_dir = args.vep_cache or lib.vep_cache or os.path.expanduser("~/.vep")
            vep_cmd.extend(
                ["--dir_cache", cache_dir, "--cache_version", args.vep_cache_version]
            )

            if os.path.exists(cache_dir):
                vep_cmd.append("--offline")
                logging.info(LOG_MESSAGES["vep_using_cache"].format(path=cache_dir))
            else:
                logging.warning(
                    LOG_MESSAGES["vep_no_cache_warn"].format(path=cache_dir)
                )
                vep_cmd.append("--database")

            # Default "everything"
            vep_cmd.append("--everything")

            # Type-specific args (like in run_vep_batch.py)
            if vcf_type in ["sv", "cnv"]:
                vep_cmd.extend(
                    ["--overlaps", "--max_sv_size", "300000000", "--buffer_size", "500"]
                )

            # User args
            if args.vep_args:
                vep_cmd.extend(shlex.split(args.vep_args))

            # 5. Run VEP
            logging.info(LOG_MESSAGES["vep_running"].format(command=" ".join(vep_cmd)))
            run_command(vep_cmd)

            # 6. Post-processing (bgzip/tabix for VCF)
            if args.format == "vcf":
                logging.info(f"Compressing output to {final_output}...")
                run_command(["bgzip", "-f", output_file])
                run_command(["tabix", "-p", "vcf", final_output])

            duration = int(time.time() - start_time)
            batch_stats.append(
                (
                    os.path.basename(current_input),
                    "SUCCESS",
                    str(timedelta(seconds=duration)),
                )
            )
            logging.info(LOG_MESSAGES["vep_complete"].format(path=final_output))

        except Exception as e:
            logging.error(f"VEP failed for {current_input}: {e}")
            batch_stats.append((os.path.basename(current_input), "FAILED", "-"))
        finally:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)
            if (
                args.add_chr
                and "chr_temp_vcf" in locals()
                and os.path.exists(chr_temp_vcf)
            ):
                os.remove(chr_temp_vcf)

    # Final Summary
    if len(input_files) > 1:
        print(f"\n{LOG_MESSAGES['vep_batch_summary']}")
        print("-" * 60)
        for fname, status, dur in batch_stats:
            print(
                LOG_MESSAGES["vep_batch_item"].format(
                    filename=fname, status=status, duration=dur
                )
            )
        print("-" * 60)

    # Exit with error if any task failed
    if any(s == "FAILED" for _, s, _ in batch_stats):
        raise WGSExtractError("One or more VEP tasks failed.")
