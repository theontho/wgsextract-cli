import argparse
import gzip
import hashlib
import logging
import os
import subprocess

from wgsextract_cli.core.annotation_resources import get_genome_size
from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.gene_map import are_gene_maps_installed
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.ref_library import (
    GENOME_DATA,
    download_file,
    get_available_genomes,
    get_genome_status,
)
from wgsextract_cli.core.reference_processing import has_ref_ns
from wgsextract_cli.core.utils import (
    WGSExtractError,
    run_command,
)
from wgsextract_cli.core.variant_files import (
    resolve_reference,
    verify_paths_exist,
)


def cmd_download(args: argparse.Namespace) -> None:
    if os.path.isdir(args.out):
        raise WGSExtractError(f"Output path is a directory: {args.out}")

    logging.info(LOG_MESSAGES["ref_downloading"].format(url=args.url, path=args.out))
    if not download_file(args.url, args.out):
        raise WGSExtractError(f"Download failed: {args.url} -> {args.out}")


def cmd_index(args: argparse.Namespace) -> None:
    verify_dependencies(["samtools", "bwa"])
    log_dependency_info(["samtools", "bwa"])
    if not args.ref:
        logging.error("--ref is required.")
        return

    if not verify_paths_exist({"--ref": args.ref}):
        return

    logging.debug(f"Resolved reference: {os.path.abspath(args.ref)}")
    logging.info(LOG_MESSAGES["ref_indexing"].format(path=args.ref))
    try:
        # 1. samtools faidx
        logging.info("Indexing FASTA with samtools faidx...")
        run_command(["samtools", "faidx", args.ref])

        # 2. samtools dict
        out_dict = args.ref + ".dict"
        # Also create a version without .fa/.fasta if it's there
        out_dict_short = os.path.splitext(args.ref)[0] + ".dict"
        if out_dict_short.endswith(".fna") or out_dict_short.endswith(".fa"):
            out_dict_short = os.path.splitext(out_dict_short)[0] + ".dict"

        logging.info(LOG_MESSAGES["ref_creating_dict"].format(path=out_dict))
        run_command(["samtools", "dict", args.ref, "-o", out_dict])

        if out_dict != out_dict_short:
            import shutil

            shutil.copy2(out_dict, out_dict_short)

        # 3. bwa index
        logging.info("Indexing FASTA with bwa index (required for alignment)...")
        run_command(["bwa", "index", args.ref])

        logging.info("Indexing complete.")
    except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
        raise WGSExtractError(f"Indexing failed: {e}") from e


def cmd_count_ns(args: argparse.Namespace) -> None:
    if not args.ref:
        logging.error("--ref is required.")
        raise WGSExtractError("Ref library installation failed.")

    if not verify_paths_exist({"--ref": args.ref}):
        raise WGSExtractError("Ref library installation failed.")

    logging.info(LOG_MESSAGES["analyzing_ns"].format(path=args.ref))
    try:
        logging.info(f"Processing {args.ref} for N-base counts...")
        with open(args.ref, "rb") as f:
            is_gzipped = f.read(2) == b"\x1f\x8b"

        opener = gzip.open if is_gzipped else open
        contig = None
        length = 0
        n_count = 0
        total_length = 0
        total_n = 0

        print("contig\tlength\tn_count\tn_percent")
        with opener(args.ref, "rt") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue
                if line.startswith(">"):
                    if contig is not None:
                        pct = (n_count / length * 100) if length else 0.0
                        print(f"{contig}\t{length}\t{n_count}\t{pct:.4f}")
                    contig = line[1:].split()[0]
                    total_length += length
                    total_n += n_count
                    length = 0
                    n_count = 0
                    continue
                seq = line.upper()
                length += len(seq)
                n_count += seq.count("N")

        if contig is not None:
            pct = (n_count / length * 100) if length else 0.0
            print(f"{contig}\t{length}\t{n_count}\t{pct:.4f}")
            total_length += length
            total_n += n_count

        total_pct = (total_n / total_length * 100) if total_length else 0.0
        print(f"TOTAL\t{total_length}\t{total_n}\t{total_pct:.4f}")

    except (OSError, gzip.BadGzipFile) as e:
        raise WGSExtractError(f"N-counting failed: {e}") from e


def cmd_ref_verify(args: argparse.Namespace) -> None:
    verify_dependencies(["gzip", "samtools"])
    log_dependency_info(["gzip", "samtools"])
    if not args.ref:
        raise WGSExtractError("--ref is required.")

    # Auto-resolve ref if a directory was provided
    resolved_ref = resolve_reference(args.ref, "")
    logging.debug(f"Resolved reference: {resolved_ref}")
    if resolved_ref is None:
        raise WGSExtractError(f"Reference file not found: {args.ref}")
    if not os.path.exists(resolved_ref):
        raise WGSExtractError(f"Reference file not found: {resolved_ref}")

    logging.info(LOG_MESSAGES["ref_verifying"].format(path=resolved_ref))

    # 1. MD5 Checksum (if known)
    filename = os.path.basename(resolved_ref)
    expected_md5 = None
    for g in GENOME_DATA:
        if g["final"] == filename:
            expected_md5 = g.get("md5")
            break

    if expected_md5:
        logging.info(LOG_MESSAGES["ref_md5_verifying"].format(filename=filename))
        hash_md5 = hashlib.md5()
        try:
            with open(resolved_ref, "rb") as f:
                for chunk in iter(lambda: f.read(4096), b""):
                    hash_md5.update(chunk)
            calculated = hash_md5.hexdigest()
            if calculated == expected_md5:
                logging.info(LOG_MESSAGES["ref_md5_ok"])
            else:
                logging.error(LOG_MESSAGES["ref_md5_failed"])
                logging.error(f"Expected: {expected_md5}")
                logging.error(f"Calculated: {calculated}")
                # We continue to show other potential errors (like gzip eof)
        except OSError as e:
            logging.error(f"MD5 verification failed to run: {e}")

    # 2. Check gzip integrity
    if resolved_ref.endswith(".gz"):
        logging.info(LOG_MESSAGES["ref_gzip_test"])
        try:
            run_command(["gzip", "-t", resolved_ref])
            logging.info(LOG_MESSAGES["ref_gzip_ok"])
        except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
            logging.error(f"Gzip integrity FAILED: {e}")
            raise WGSExtractError("Gzip integrity check failed.") from e

    # 2. Check samtools faidx
    logging.info(LOG_MESSAGES["ref_faidx_check"])
    try:
        # Check if index exists, if not try to create/verify
        res = run_command(["samtools", "faidx", resolved_ref], capture_output=True)
        if res.returncode == 0:
            logging.info(LOG_MESSAGES["ref_faidx_ok"])
        else:
            logging.error(f"Samtools faidx FAILED: {res.stderr}")
            raise WGSExtractError("Samtools faidx check failed.")
    except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
        logging.error(f"Samtools faidx check failed: {e}")
        raise WGSExtractError("Samtools faidx check failed.") from e

    logging.info(
        LOG_MESSAGES["ref_valid"].format(filename=os.path.basename(resolved_ref))
    )


def cmd_library_list(args: argparse.Namespace) -> None:
    """Non-interactive library status list."""
    from wgsextract_cli.core.config import settings

    reflib_dir = args.ref
    if not reflib_dir:
        reflib_dir = settings.get("reference_library")
    if not reflib_dir:
        prog_root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "../../../..")
        )
        reflib_dir = os.path.join(prog_root, "reference")

    reflib_dir = os.path.abspath(reflib_dir)

    genomes = get_available_genomes()

    print(f"\nREFERENCE LIBRARY: {reflib_dir}")
    print("-" * 80)
    print(f"{'GENOME':<40} {'STATUS':<15} {'SIZE':<10} {'DETAILS'}")
    print("-" * 80)

    for g in genomes:
        status = get_genome_status(g["final"], reflib_dir).upper()
        size = get_genome_size(g["final"], reflib_dir)

        details = []
        if has_ref_ns(g["final"], reflib_dir):
            details.append("N-counts")

        # Check for VEP cache
        bn = g["final"].upper()
        species = ""
        assembly = ""
        if "38" in bn or "HG38" in bn or "GRCH38" in bn:
            assembly = "GRCh38"
            species = "homo_sapiens"
        elif "37" in bn or "HG19" in bn or "GRCH37" in bn:
            assembly = "GRCh37"
            species = "homo_sapiens"
        elif "GSD" in bn or "DOG" in bn:
            species = "canis_lupus_familiaris"
        elif "FCA126" in bn or "CAT" in bn:
            species = "felis_catus"

        if species:
            vep_path = os.path.join(reflib_dir, "vep", species)
            if os.path.exists(vep_path):
                # If human, check for specific assembly
                if species == "homo_sapiens" and assembly:
                    # Look for any directory starting with [version]_[assembly]
                    found_asm = False
                    if os.path.isdir(vep_path):
                        for d in os.listdir(vep_path):
                            if assembly in d:
                                found_asm = True
                                break
                    if found_asm:
                        details.append(f"VEP-{assembly}")
                else:
                    details.append("VEP-Cache")

        details_str = ", ".join(details)
        print(f"{g['label']:<40} {status:<15} {size:<10} {details_str}")

    print("-" * 80)
    gm_installed = "INSTALLED" if are_gene_maps_installed(reflib_dir) else "MISSING"
    print(f"{'Gene Maps (hg19/hg38)':<40} {gm_installed}")

    # Check for ClinVar, REVEL, gnomAD
    ref_dir = os.path.join(reflib_dir, "ref")
    for build in ["hg19", "hg38"]:
        cv = (
            "INSTALLED"
            if os.path.exists(os.path.join(ref_dir, f"clinvar_{build}.vcf.gz"))
            else "MISSING"
        )
        print(f"{f'ClinVar ({build})':<40} {cv}")
        rv = (
            "INSTALLED"
            if os.path.exists(os.path.join(ref_dir, f"revel_{build}.tsv.gz"))
            else "MISSING"
        )
        print(f"{f'REVEL ({build})':<40} {rv}")
        ph = (
            "INSTALLED"
            if os.path.exists(os.path.join(ref_dir, f"phylop_{build}.tsv.gz"))
            else "MISSING"
        )
        print(f"{f'PhyloP ({build})':<40} {ph}")
        gn = (
            "INSTALLED"
            if any(
                os.path.exists(os.path.join(ref_dir, f"gnomad_{build}{ext}"))
                for ext in [".vcf.bgz", ".vcf.gz"]
            )
            else "MISSING"
        )
        print(f"{f'gnomAD ({build})':<40} {gn}")

    print("-" * 80)
