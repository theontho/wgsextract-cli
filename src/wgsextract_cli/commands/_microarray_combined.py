import gzip
import logging
import os
import subprocess
import time

from wgsextract_cli.core.utils import (
    WGSExtractError,
    run_command,
)
from wgsextract_cli.core.variant_files import popen


def _write_microarray_combined_kit(
    *,
    args,
    outdir,
    base_name,
    is_vcf,
    out_vcf,
    ref_fasta,
    ref_vcf_tab,
):
    # 2. Extract results to a temporary CombinedKit.txt
    combined_kit_txt = os.path.join(outdir, f"{base_name}_CombinedKit.txt")
    logging.info(f"Extracting genotypes to {combined_kit_txt}...")
    start_ext = time.time()

    try:
        if is_vcf:
            # VCF MODE: We need to fill in reference alleles for missing positions
            # We iterate through the target list and check our 'hits' VCF

            # Load hits into a lookup dict (CHROM, POS) -> Genotype
            variant_calls = {}
            cmd = ["bcftools", "query", "-f", "%CHROM\t%POS\t[%TGT]\n", out_vcf]
            with popen(cmd, stdout=subprocess.PIPE, text=True) as proc:
                if proc.stdout:
                    for line in proc.stdout:
                        parts = line.strip().split("\t")
                        if len(parts) >= 3:
                            variant_calls[(parts[0], parts[1])] = parts[2]

            logging.info(
                f"Found {len(variant_calls)} variants in input VCF matching targets."
            )

            # OPTIMIZATION: Pre-fetch ALL reference alleles for targets in one pass
            logging.info("Pre-fetching reference alleles for target SNPs...")
            ref_alleles = {}
            region_file = os.path.join(outdir, "targets.regions")

            # 1. Detect FASTA chromosome naming to ensure faidx hits
            # Use .fai file if it exists, as it is more portable than samtools faidx -l
            fasta_chroms = set()
            fai_path = ref_fasta + ".fai"
            if os.path.exists(fai_path):
                try:
                    with open(fai_path) as f_fai:
                        fasta_chroms = {line.split("\t")[0] for line in f_fai}
                except Exception as e:
                    logging.warning(f"Failed to read .fai file: {e}")

            if not fasta_chroms:
                res_l = run_command(
                    ["samtools", "faidx", "-l", ref_fasta],
                    capture_output=True,
                )
                if res_l.returncode == 0:
                    fasta_chroms = set(res_l.stdout.splitlines())

            try:
                # 2. Generate region file (chr:pos-pos) with normalization
                # Map FASTA-normalized name back to original SNP-tab name for reverse lookup
                norm_to_orig = {}

                with open(region_file, "w") as f_reg:
                    # Use a stream that handles both compressed and uncompressed
                    if ref_vcf_tab.endswith((".gz", ".bgz")):
                        f_in = gzip.open(ref_vcf_tab, "rt")
                    else:
                        f_in = open(ref_vcf_tab)

                    with f_in:
                        for line in f_in:
                            if line.startswith("#"):
                                continue
                            parts = line.strip().split("\t")
                            if len(parts) < 2:
                                continue
                            c, p = parts[0], parts[1]
                            # Normalize for FASTA
                            fc = c
                            if fc not in fasta_chroms:
                                if fc.startswith("chr") and fc[3:] in fasta_chroms:
                                    fc = fc[3:]
                                elif (
                                    not fc.startswith("chr")
                                    and f"chr{fc}" in fasta_chroms
                                ):
                                    fc = f"chr{fc}"

                            if fc in fasta_chroms:
                                f_reg.write(f"{fc}:{p}-{p}\n")
                                norm_to_orig[f"{fc}:{p}-{p}"] = (c, p)

                # 3. Run samtools faidx --region-file
                faidx_cmd = [
                    "samtools",
                    "faidx",
                    "--region-file",
                    region_file,
                    ref_fasta,
                ]
                with popen(faidx_cmd, stdout=subprocess.PIPE, text=True) as proc_ref:
                    curr_region = None
                    if proc_ref.stdout:
                        for line in proc_ref.stdout:
                            if line.startswith(">"):
                                curr_region = line.strip()[1:]
                            else:
                                if curr_region:
                                    base = line.strip().upper()
                                    # Map back to ORIGINAL chromosome name from the region string
                                    if curr_region in norm_to_orig:
                                        orig_c, orig_p = norm_to_orig[curr_region]
                                        ref_alleles[(orig_c, orig_p)] = base
                                    elif ":" in curr_region:
                                        # Fallback parsing
                                        c_norm, p_range = curr_region.split(":", 1)
                                        p = p_range.split("-")[0]
                                        ref_alleles[(c_norm, p)] = base
                                        # Also store variations to be safe
                                        if not c_norm.startswith("chr"):
                                            ref_alleles[(f"chr{c_norm}", p)] = base
                                        elif c_norm.startswith("chr"):
                                            ref_alleles[(c_norm[3:], p)] = base
                logging.info(f"Pre-fetched {len(ref_alleles)} reference alleles.")
                if os.path.exists(region_file):
                    os.remove(region_file)
            except Exception as e:
                logging.warning(
                    f"Fast reference pre-fetch failed: {e}. Falling back to SNP-tab column or 'N'."
                )
                if os.path.exists(region_file):
                    os.remove(region_file)

            with open(combined_kit_txt, "w") as f_out:
                f_out.write("# RSID\tCHROM\tPOS\tRESULT\n")

                if args.region:
                    # Use popen for tabix streaming
                    proc = popen(
                        ["tabix", ref_vcf_tab, args.region],
                        stdout=subprocess.PIPE,
                        text=True,
                    )
                else:
                    # Use gzip.open for streaming if possible, but popen is easier for unified loop below
                    if ref_vcf_tab.endswith((".gz", ".bgz")):
                        proc = popen(
                            ["gzip", "-dc", ref_vcf_tab],
                            stdout=subprocess.PIPE,
                            text=True,
                        )
                    else:
                        proc = popen(
                            ["cat", ref_vcf_tab], stdout=subprocess.PIPE, text=True
                        )

                with proc:
                    if proc.stdout:
                        ref_col_idx = -1
                        for line in proc.stdout:
                            if line.startswith("#"):
                                header = line.strip().split("\t")
                                if "REF" in header:
                                    ref_col_idx = header.index("REF")
                                continue
                            parts = line.strip().split("\t")
                            if len(parts) < 3:
                                continue
                            chrom, pos, rsid = parts[0], parts[1], parts[2]

                            if (chrom, pos) in variant_calls:
                                tgt = variant_calls[(chrom, pos)]
                                genotype = (
                                    tgt.replace("/", "")
                                    .replace("|", "")
                                    .replace(".", "-")
                                )
                            else:
                                # MISSING in VCF -> Assume Homozygous Reference
                                ref_base = None

                                # 1. Try REF column from TAB file first (FASTEST)
                                if ref_col_idx != -1 and len(parts) > ref_col_idx:
                                    ref_base = parts[ref_col_idx].upper()

                                # 2. Try pre-fetched alleles
                                if not ref_base:
                                    ref_base = ref_alleles.get((chrom, pos))

                                # 3. Fallback to N (NO slow subprocess calls here)
                                if not ref_base:
                                    ref_base = "N"

                                genotype = f"{ref_base}{ref_base}"

                            chrom_norm = chrom.replace("chr", "").replace("M", "MT")
                            f_out.write(f"{rsid}\t{chrom_norm}\t{pos}\t{genotype}\n")

        else:
            # CRAM/BAM MODE: Standard extraction
            with open(combined_kit_txt, "w") as f_out:
                # Add a header for GEDMatch compatibility
                f_out.write("# RSID\tCHROM\tPOS\tRESULT\n")

                # Run bcftools query
                cmd = [
                    "bcftools",
                    "query",
                    "-f",
                    "%ID\t%CHROM\t%POS\t[%TGT]\n",
                    out_vcf,
                ]
                with popen(cmd, stdout=subprocess.PIPE, text=True) as proc:
                    if proc.stdout:
                        for line in proc.stdout:
                            parts = line.strip().split("\t")
                            if len(parts) < 4:
                                continue

                            snp_id, chrom, pos, tgt = (
                                parts[0],
                                parts[1],
                                parts[2],
                                parts[3],
                            )

                            if snp_id == ".":
                                snp_id = f"pos_{chrom}_{pos}"

                            genotype = (
                                tgt.replace("/", "").replace("|", "").replace(".", "-")
                            )
                            if not genotype:
                                genotype = "--"

                            chrom_norm = chrom.replace("chr", "").replace("M", "MT")
                            f_out.write(f"{snp_id}\t{chrom_norm}\t{pos}\t{genotype}\n")

        ext_duration = time.time() - start_ext
        logging.info(f"Extraction took {ext_duration:.2f}s")
    except Exception as e:
        logging.error(f"Failed to generate CombinedKit.txt: {e}")
        raise WGSExtractError("Failed to generate CombinedKit.txt.") from e
    finally:
        # Cleanup intermediate files
        for f in ["hit_vcf", "annotated_vcf"]:
            if f in locals():
                path = locals()[f]
                if path and os.path.exists(path):
                    os.remove(path)
                    for ext in [".tbi", ".csi"]:
                        if os.path.exists(path + ext):
                            os.remove(path + ext)
    return combined_kit_txt
