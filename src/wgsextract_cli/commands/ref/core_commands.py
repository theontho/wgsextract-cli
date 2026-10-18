import argparse
import logging
import os
import subprocess

from wgsextract_cli.core.annotation_resources import get_genome_size
from wgsextract_cli.core.builds import build_from_path
from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.gene_map import are_gene_maps_installed
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.ref_library.catalog import (
    get_available_genomes,
    get_genome_status,
)
from wgsextract_cli.core.ref_library.downloads import download_file
from wgsextract_cli.core.reference_assets import (
    n_count_paths,
    read_fai_lengths,
    reference_dict_path,
    resolve_reference_library,
    sequence_signature,
)
from wgsextract_cli.core.reference_ncount import write_n_count_files
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
        raise WGSExtractError("--ref is required.")

    verify_paths_exist({"--ref": args.ref})

    logging.debug(f"Resolved reference: {os.path.abspath(args.ref)}")
    logging.info(LOG_MESSAGES["ref_indexing"].format(path=args.ref))
    try:
        logging.info("Indexing FASTA with samtools faidx...")
        run_command(["samtools", "faidx", args.ref])

        out_dict = reference_dict_path(args.ref)
        logging.info(LOG_MESSAGES["ref_creating_dict"].format(path=out_dict))
        run_command(["samtools", "dict", args.ref, "-o", out_dict])

        logging.info("Indexing FASTA with bwa index (required for alignment)...")
        run_command(["bwa", "index", args.ref])

        logging.info("Indexing complete.")
    except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
        raise WGSExtractError(f"Indexing failed: {e}") from e


def cmd_count_ns(args: argparse.Namespace) -> None:
    """Write <ref>_ncnt.csv / <ref>_nbin.csv next to the FASTA, or into --outdir."""
    if not args.ref:
        raise WGSExtractError("--ref is required.")

    verify_paths_exist({"--ref": args.ref})

    explicit_dests = getattr(args, "_explicit_dests", None)
    outdir = getattr(args, "outdir", None)
    if explicit_dests is not None and "outdir" not in explicit_dests:
        outdir = None  # configured output_directory must not relocate sidecars
    if outdir:
        os.makedirs(outdir, exist_ok=True)
    ncnt_path, nbin_path = n_count_paths(args.ref, outdir)

    logging.info(LOG_MESSAGES["analyzing_ns"].format(path=args.ref))
    try:
        totals = write_n_count_files(args.ref, ncnt_path, nbin_path)
    except (OSError, EOFError, ValueError) as e:
        hint = "" if outdir else " Pass --outdir to write the N-count files elsewhere."
        raise WGSExtractError(f"N-counting failed: {e}.{hint}") from e

    pct = totals.n_count / totals.length * 100 if totals.length else 0.0
    logging.info(
        f"{totals.sequences} sequences, {totals.length:,} bp, "
        f"{totals.n_count:,} N ({pct:.2f}%) in {totals.large_runs} runs "
        f">300 bp and {totals.small_runs} shorter runs."
    )
    logging.info(f"Wrote {ncnt_path}")
    logging.info(f"Wrote {nbin_path}")


def cmd_ref_verify(args: argparse.Namespace) -> None:
    verify_dependencies(["gzip", "samtools"])
    log_dependency_info(["gzip", "samtools"])
    if not args.ref:
        raise WGSExtractError("--ref is required.")

    # Auto-resolve ref if a directory was provided
    resolved_ref = resolve_reference(args.ref, "")
    logging.debug(f"Resolved reference: {resolved_ref}")
    if resolved_ref is None or not os.path.isfile(resolved_ref):
        raise WGSExtractError(f"Reference file not found: {resolved_ref or args.ref}")

    logging.info(LOG_MESSAGES["ref_verifying"].format(path=resolved_ref))

    # 1. Compression integrity
    if resolved_ref.endswith(".gz"):
        logging.info(LOG_MESSAGES["ref_gzip_test"])
        try:
            run_command(["gzip", "-t", resolved_ref])
        except (OSError, subprocess.SubprocessError) as e:
            raise WGSExtractError(f"Gzip integrity check failed: {e}") from e
        logging.info(LOG_MESSAGES["ref_gzip_ok"])

    # 2. FASTA index: query an existing index (read-only), else build one.
    logging.info(LOG_MESSAGES["ref_faidx_check"])
    lengths = read_fai_lengths(resolved_ref)
    faidx_cmd = ["samtools", "faidx", resolved_ref]
    if lengths:
        faidx_cmd.append(f"{next(iter(lengths))}:1-100")
    try:
        run_command(faidx_cmd, capture_output=True)
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(f"Samtools faidx check failed: {e}") from e
    lengths = lengths or read_fai_lengths(resolved_ref)
    if not lengths:
        raise WGSExtractError(f"samtools faidx did not produce {resolved_ref}.fai")
    logging.info(LOG_MESSAGES["ref_faidx_ok"])

    # 3. Sequence dictionary signature against the library catalog (if known)
    filename = os.path.basename(resolved_ref)
    signature = sequence_signature(lengths)
    expected = next(
        (g.get("md5") for g in get_available_genomes() if g["final"] == filename),
        None,
    )
    if expected:
        logging.info(f"Verifying sequence dictionary signature for {filename}...")
        if signature != expected:
            raise WGSExtractError(
                f"{filename} does not match the catalog sequence dictionary "
                f"(expected signature {expected}, found {signature}). The file is "
                "a different build or patch level than the catalog entry."
            )
        logging.info("Sequence dictionary signature: OK")
    else:
        logging.info(f"Sequence dictionary signature: {signature}")

    if not os.path.exists(reference_dict_path(resolved_ref)):
        logging.warning(
            f"No sequence dictionary {reference_dict_path(resolved_ref)}; "
            "run 'wgsextract ref index' if GATK tools need it."
        )

    logging.info(LOG_MESSAGES["ref_valid"].format(filename=filename))


def cmd_library_list(args: argparse.Namespace) -> None:
    """Non-interactive library status list."""
    reflib_dir = resolve_reference_library(args.ref)
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
        assembly = {"hg38": "GRCh38", "hg19": "GRCh37"}.get(
            build_from_path(g["final"]) or "", ""
        )
        if assembly:
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
