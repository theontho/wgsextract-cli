import argparse
import logging
import os
import shutil
import subprocess
import tempfile
import zipfile
import zlib
from pathlib import Path

from wgsextract_cli.core import bwa_mem2
from wgsextract_cli.core.annotation_resources import get_genome_size
from wgsextract_cli.core.bgzf import open_decompressed
from wgsextract_cli.core.builds import build_from_path, canonical_build
from wgsextract_cli.core.constants import REFERENCE_MODELS
from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.gene_map import are_gene_maps_installed
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.paths import absolute_path
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
    if Path(args.out).is_dir():
        raise WGSExtractError(f"Output path is a directory: {args.out}")

    logging.info(LOG_MESSAGES["ref_downloading"].format(url=args.url, path=args.out))
    if not download_file(args.url, args.out):
        raise WGSExtractError(f"Download failed: {args.url} -> {args.out}")


def cmd_index(args: argparse.Namespace) -> None:
    aligner = getattr(args, "aligner", None) or "bwa"
    verify_dependencies(["samtools", aligner])
    log_dependency_info(["samtools", aligner])
    if not args.ref:
        raise WGSExtractError("--ref is required.")

    verify_paths_exist({"--ref": args.ref})

    logging.debug(f"Resolved reference: {str(absolute_path(args.ref))}")
    logging.info(LOG_MESSAGES["ref_indexing"].format(path=args.ref))
    try:
        logging.info("Indexing FASTA with samtools faidx...")
        run_command(["samtools", "faidx", args.ref])

        out_dict = reference_dict_path(args.ref)
        logging.info(LOG_MESSAGES["ref_creating_dict"].format(path=out_dict))
        run_command(["samtools", "dict", args.ref, "-o", out_dict])

        if aligner == "bwa-mem2":
            bwa_mem2.ensure_index(args.ref, download=True)
        else:
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
        Path(outdir).mkdir(parents=True, exist_ok=True)
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


def _check_fasta_index(ref: str) -> dict[str, int]:
    """Index the FASTA into a temporary .fai (and .gzi) and compare them with the
    existing ones, which are written if missing. Returns the contig lengths."""
    sidecars = {".fai": "--fai-idx"} | (
        {".gzi": "--gzi-idx"} if ref.endswith(".gz") else {}
    )
    with tempfile.TemporaryDirectory(
        dir=Path(ref).parent, prefix=".wgse_verify_"
    ) as tmp:
        fresh = {ext: Path(tmp, "ref" + ext) for ext in sidecars}
        cmd = ["samtools", "faidx"]
        for ext, flag in sidecars.items():
            cmd += [flag, str(fresh[ext])]
        try:
            run_command([*cmd, ref], capture_output=True)
        except (OSError, subprocess.SubprocessError) as e:
            raise WGSExtractError(f"Samtools faidx check failed: {e}") from e
        for ext, new in fresh.items():
            existing = Path(ref + ext)
            if not existing.exists():
                shutil.copyfile(new, existing)
            elif existing.read_bytes() != new.read_bytes():
                raise WGSExtractError(
                    f"{existing} doesn't match {Path(ref).name}: it was made for "
                    f"another version of the file. Delete it and run "
                    f"'wgsextract ref index --ref {ref}'."
                )
    lengths = read_fai_lengths(ref)
    if not lengths:
        raise WGSExtractError(f"samtools faidx did not produce {ref}.fai")
    return lengths


def _check_dict_matches(ref: str, lengths: dict[str, int]) -> None:
    """The sequence dictionary, if there is one, must list the FASTA's contigs."""
    dict_path = Path(reference_dict_path(ref))
    if not dict_path.exists():
        return
    listed = {}
    for line in dict_path.read_text().splitlines():
        if line.startswith("@SQ"):
            tags = dict(f.split(":", 1) for f in line.split("\t")[1:] if ":" in f)
            listed[tags.get("SN", "")] = int(tags.get("LN", 0))
    if listed != lengths:
        raise WGSExtractError(
            f"{dict_path} doesn't match {Path(ref).name}'s contigs. Delete it and "
            f"run 'wgsextract ref index --ref {ref}'."
        )


def cmd_ref_verify(args: argparse.Namespace) -> None:
    verify_dependencies(["samtools"])
    log_dependency_info(["samtools"])
    if not args.ref:
        raise WGSExtractError("--ref is required.")

    # Auto-resolve ref if a directory was provided
    resolved_ref = resolve_reference(args.ref, "")
    logging.debug(f"Resolved reference: {resolved_ref}")
    if resolved_ref is None or not Path(resolved_ref).is_file():
        raise WGSExtractError(f"Reference file not found: {resolved_ref or args.ref}")

    logging.info(LOG_MESSAGES["ref_verifying"].format(path=resolved_ref))

    # 1. Compression integrity
    if resolved_ref.endswith(".gz"):
        logging.info(LOG_MESSAGES["ref_gzip_test"])
        try:
            # Reading it all checks every member's CRC and length, as gzip -t.
            with open_decompressed(resolved_ref) as f:
                while f.read(1 << 20):
                    pass
        except (OSError, EOFError, zlib.error, zipfile.BadZipFile) as e:
            raise WGSExtractError(f"Gzip integrity check failed: {e}") from e
        logging.info(LOG_MESSAGES["ref_gzip_ok"])

    # 2. FASTA index: index afresh beside the reference and compare, so a stale
    # .fai/.gzi (from another version of the file) fails, not just a broken one.
    logging.info(LOG_MESSAGES["ref_faidx_check"])
    lengths = _check_fasta_index(resolved_ref)
    logging.info(LOG_MESSAGES["ref_faidx_ok"])
    _check_dict_matches(resolved_ref, lengths)

    # 3. Sequence dictionary signature against the library catalog (if known)
    filename = Path(resolved_ref).name
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

    if not Path(reference_dict_path(resolved_ref)).exists():
        logging.warning(
            f"No sequence dictionary {reference_dict_path(resolved_ref)}; "
            "run 'wgsextract ref index' if GATK tools need it."
        )

    logging.info(LOG_MESSAGES["ref_valid"].format(filename=filename))


def _catalog_build(genome: dict[str, str]) -> str | None:
    model = REFERENCE_MODELS.get(genome.get("md5", ""))
    return canonical_build(model[0]) if model else build_from_path(genome["final"])


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
        # Bootstrap ships every catalog genome's N-counts; they matter once it's here.
        if status != "MISSING" and has_ref_ns(g["final"], reflib_dir):
            details.append("N-counts")

        # Check for VEP cache
        bn = g["final"].upper()
        species = ""
        # The catalog's signature says the build when the file name doesn't
        # (human_g1k_v37). VEP caches exist for GRCh37 and GRCh38 only.
        assembly = {"hg38": "GRCh38", "hg19": "GRCh37"}.get(_catalog_build(g) or "", "")
        if assembly:
            species = "homo_sapiens"
        elif "GSD" in bn or "DOG" in bn:
            species = "canis_lupus_familiaris"
        elif "FCA126" in bn or "CAT" in bn:
            species = "felis_catus"

        if species:
            vep_path = str(Path(reflib_dir, "vep", species))
            if Path(vep_path).exists():
                # If human, check for specific assembly
                if species == "homo_sapiens" and assembly:
                    # Look for any directory starting with [version]_[assembly]
                    found_asm = False
                    if Path(vep_path).is_dir():
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
    ref_dir = str(Path(reflib_dir, "ref"))
    for build in ["hg19", "hg38"]:
        cv = (
            "INSTALLED"
            if Path(ref_dir, f"clinvar_{build}.vcf.gz").exists()
            else "MISSING"
        )
        print(f"{f'ClinVar ({build})':<40} {cv}")
        rv = (
            "INSTALLED"
            if Path(ref_dir, f"revel_{build}.tsv.gz").exists()
            else "MISSING"
        )
        print(f"{f'REVEL ({build})':<40} {rv}")
        ph = (
            "INSTALLED"
            if any(
                Path(ref_dir, f"phylop_{build}{ext}").exists()
                for ext in (".bw", ".tsv.gz")
            )
            else "MISSING"
        )
        print(f"{f'PhyloP ({build})':<40} {ph}")
        gn = (
            "INSTALLED"
            if any(
                Path(ref_dir, f"gnomad_{build}{ext}").exists()
                for ext in [".vcf.bgz", ".vcf.gz"]
            )
            else "MISSING"
        )
        print(f"{f'gnomAD ({build})':<40} {gn}")

    print("-" * 80)
