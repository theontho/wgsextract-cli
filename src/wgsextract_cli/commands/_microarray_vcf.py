import gzip
import logging
import os
import subprocess
import time

from wgsextract_cli.core.utils import (
    WGSExtractError,
    run_command,
)
from wgsextract_cli.core.variant_files import (
    ensure_vcf_indexed,
    popen,
)


def split_snps_by_chrom(ref_vcf_tab, outdir):
    """Splits the master SNP list into per-chromosome files for parallel processing."""
    chrom_files = {}

    # We use tabix to list chromosomes efficiently if possible
    try:
        # Get list of chromosomes from the index
        res = run_command(["tabix", "-l", ref_vcf_tab], capture_output=True)
        chroms = res.stdout.strip().splitlines()

        for chrom in chroms:
            chrom_out = os.path.join(outdir, f"snps_{chrom}.tab")
            # tabix extraction is very fast
            with open(chrom_out, "w") as f:
                run_command(["tabix", ref_vcf_tab, chrom], stdout=f)
            chrom_files[chrom] = chrom_out

    except Exception as e:
        logging.warning(
            f"Failed to split SNPs using tabix: {e}. Falling back to linear scan."
        )
        # Fallback: slow linear scan if tabix fails
        # Use gzip.open if compressed, else regular open
        if ref_vcf_tab.endswith((".gz", ".bgz")):
            f_in = gzip.open(ref_vcf_tab, "rt")
        else:
            f_in = open(ref_vcf_tab)

        with f_in:
            for line in f_in:
                if line.startswith("#"):
                    continue
                parts = line.split("\t", 2)
                if len(parts) < 2:
                    continue
                chrom = parts[0]
                if chrom not in chrom_files:
                    chrom_files[chrom] = os.path.join(outdir, f"snps_{chrom}.tab")
                with open(chrom_files[chrom], "a") as f:
                    f.write(line)

    return chrom_files


def process_chrom(
    chrom, snp_file, chrom_tmp_dir, ref_fasta, ref_vcf_tab, ploidy_args, input_path
):
    """Worker function for parallel chromosome processing. Must be at module level for pickling."""
    chrom_vcf = os.path.join(chrom_tmp_dir, f"{chrom}.vcf.gz")
    # We combine mpileup | call | annotate into a single pipeline
    try:
        logging.debug(f"Processing chromosome {chrom}...")
        p1 = popen(
            [
                "bcftools",
                "mpileup",
                "-r",
                chrom,
                "-B",
                "-I",
                "-C",
                "50",
                "-f",
                ref_fasta,
                "--targets-file",
                snp_file,
                input_path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        p2 = popen(
            ["bcftools", "call"]
            + ploidy_args
            + ["-m", "-V", "indels", "-Oz", "-o", chrom_vcf],
            stdin=p1.stdout,
            stderr=subprocess.PIPE,
        )

        if p1.stdout:
            p1.stdout.close()

        _, stderr2 = p2.communicate()
        stderr1 = p1.stderr.read() if p1.stderr else b""
        p1_returncode = p1.wait()

        if p1_returncode != 0:
            logging.error(f"bcftools mpileup failed for {chrom}: {stderr1.decode()}")
            raise RuntimeError(f"bcftools processing failed for {chrom}")

        if p2.returncode != 0:
            logging.error(f"bcftools call failed for {chrom}: {stderr2.decode()}")
            raise RuntimeError(f"bcftools processing failed for {chrom}")

        if not os.path.exists(chrom_vcf) or os.path.getsize(chrom_vcf) < 100:
            logging.warning(f"No variants called for {chrom}, skipping.")
            return None

        ensure_vcf_indexed(chrom_vcf)

        # Annotate RSIDs immediately
        annotated_chrom_vcf = os.path.join(chrom_tmp_dir, f"{chrom}_ann.vcf.gz")
        ann_res = run_command(
            [
                "bcftools",
                "annotate",
                "-a",
                ref_vcf_tab,
                "-c",
                "CHROM,POS,ID",
                "-Oz",
                "-o",
                annotated_chrom_vcf,
                chrom_vcf,
            ],
            capture_output=True,
            check=False,
        )

        if ann_res.returncode != 0:
            logging.error(
                f"bcftools annotate failed for {chrom}: {ann_res.stderr.decode()}"
            )
            raise RuntimeError(f"bcftools processing failed for {chrom}")

        os.remove(chrom_vcf)
        if os.path.exists(chrom_vcf + ".tbi"):
            os.remove(chrom_vcf + ".tbi")
        return annotated_chrom_vcf
    except Exception as e:
        logging.error(f"Error processing {chrom}: {e}")
        raise


def _prepare_microarray_vcf(
    *,
    args,
    outdir,
    base_name,
    is_vcf,
    ref_vcf_tab,
    region_args,
    ploidy_args,
    ref_fasta,
    threads,
    start_vcf,
    out_vcf,
):
    if is_vcf:
        logging.info(f"VCF input detected. Extracting target SNPs from {args.input}...")
        # For VCF input, we intersect our targets with the input VCF.
        # Any missing targets are assumed to be homozygous reference.
        try:
            # Step 1: Get actual variants from the input VCF that match our targets
            hit_vcf = os.path.join(outdir, f"{base_name}_hits.vcf.gz")
            # Use long-form --targets-file to avoid version conflicts
            run_command(
                ["bcftools", "view", "--targets-file", ref_vcf_tab]
                + region_args
                + ["-Oz", "-o", hit_vcf, args.input],
                check=True,
            )
            ensure_vcf_indexed(hit_vcf)

            # Step 2: Since we need a complete file for the templates, we'll
            # handle the "Reference Filling" during the extraction to CombinedKit.txt
            # to save the overhead of building a massive reference-filled VCF.
            out_vcf = hit_vcf  # We use the hits for extraction
            vcf_duration = time.time() - start_vcf
            logging.info(f"VCF extraction took {vcf_duration:.2f}s")

            # For VCF input, annotation is usually already present, but we run it
            # to ensure RSIDs match our master tab file
            annotated_vcf = os.path.join(outdir, f"{base_name}_annotated.vcf.gz")
            run_command(
                [
                    "bcftools",
                    "annotate",
                    "-a",
                    ref_vcf_tab,
                    "-c",
                    "CHROM,POS,ID",
                    "-Oz",
                    "-o",
                    annotated_vcf,
                    out_vcf,
                ],
                check=True,
            )
            out_vcf = annotated_vcf
            ensure_vcf_indexed(out_vcf)

        except Exception as e:
            logging.error(f"VCF extraction failed: {e}")
            raise WGSExtractError("Microarray VCF extraction failed.") from e

    elif args.parallel and not args.region:
        from concurrent.futures import ProcessPoolExecutor

        logging.info(
            f"Enabling parallel variant calling across chromosomes using {threads} threads..."
        )

        # Create a temp dir for chromosome chunks
        chrom_tmp_dir = os.path.join(outdir, "chrom_chunks")
        os.makedirs(chrom_tmp_dir, exist_ok=True)

        # Split SNPs by chrom
        chrom_snps = split_snps_by_chrom(ref_vcf_tab, chrom_tmp_dir)

        vcf_chunks = []
        with ProcessPoolExecutor(max_workers=int(threads)) as executor:
            futures = {
                executor.submit(
                    process_chrom,
                    chrom,
                    snp_file,
                    chrom_tmp_dir,
                    ref_fasta,
                    ref_vcf_tab,
                    ploidy_args,
                    args.input,
                ): chrom
                for chrom, snp_file in chrom_snps.items()
            }
            for future in futures:
                res = future.result()
                if res:
                    vcf_chunks.append(res)

        # Merge results
        if vcf_chunks:
            logging.info(f"Merging {len(vcf_chunks)} chromosome VCFs...")
            run_command(
                ["bcftools", "concat", "-Oz", "-o", out_vcf] + sorted(vcf_chunks),
                check=True,
            )
            ensure_vcf_indexed(out_vcf)

            # Use the merged VCF as the "annotated" source
            annotated_vcf = out_vcf

            # Cleanup chunks
            import shutil

            shutil.rmtree(chrom_tmp_dir)
        else:
            logging.error("No VCF chunks were generated.")
            raise WGSExtractError("No VCF chunks were generated.")

        vcf_duration = time.time() - start_vcf
        logging.info(f"Parallel VCF generation and annotation took {vcf_duration:.2f}s")

    else:
        # ORIGINAL SEQUENTIAL LOGIC
        try:
            # mpileup restricted to target SNPs
            mpileup_cmd = (
                ["bcftools", "mpileup"]
                + region_args
                + [
                    "-B",
                    "-I",
                    "-C",
                    "50",
                    "-f",
                    ref_fasta,
                    "--targets-file",
                    ref_vcf_tab,
                    args.input,
                ]
            )

            p1 = popen(
                mpileup_cmd,
                stdout=subprocess.PIPE,
            )
            p2 = popen(
                ["bcftools", "call"]
                + ploidy_args
                + ["-m", "-V", "indels", "-Oz", "-o", out_vcf],
                stdin=p1.stdout,
            )
            if p1.stdout:
                p1.stdout.close()
            p2.communicate()
            p1_returncode = p1.wait()
            if p1_returncode != 0 or p2.returncode != 0:
                raise RuntimeError(
                    f"bcftools variant calling failed: mpileup={p1_returncode}, call={p2.returncode}"
                )

            ensure_vcf_indexed(out_vcf)
            vcf_duration = time.time() - start_vcf
            logging.info(f"VCF generation took {vcf_duration:.2f}s")

            # 1.5 Annotate the VCF with RSIDs from the reference tab file
            annotated_vcf = os.path.join(outdir, f"{base_name}_annotated.vcf.gz")
            logging.info(f"Annotating VCF with RSIDs from {ref_vcf_tab}...")
            start_ann = time.time()
            try:
                # -a: annotation file, -c: columns to use (CHROM, POS, ID)
                run_command(
                    [
                        "bcftools",
                        "annotate",
                        "-a",
                        ref_vcf_tab,
                        "-c",
                        "CHROM,POS,ID",
                        "-Oz",
                        "-o",
                        annotated_vcf,
                        out_vcf,
                    ],
                    check=True,
                )
                # Use the annotated VCF for subsequent steps
                out_vcf = annotated_vcf
                ensure_vcf_indexed(out_vcf)
                ann_duration = time.time() - start_ann
                logging.info(f"Annotation took {ann_duration:.2f}s")
            except Exception as e:
                logging.warning(f"VCF annotation failed, IDs may be missing: {e}")

        except Exception as e:
            logging.error(f"Variant calling failed: {e}")
            raise WGSExtractError("Microarray variant calling failed.") from e
    return out_vcf
