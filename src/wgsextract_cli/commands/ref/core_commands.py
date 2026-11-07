import argparse
import logging
import os
import shutil
import subprocess
import tempfile
import zipfile
import zlib
from os import fspath
from pathlib import Path

from wgsextract_cli.api._cli import cli_handler
from wgsextract_cli.api.ref import (
    RefCountNsParams,
    RefDownloadParams,
    RefIndexParams,
    RefLibraryListParams,
    RefVerifyParams,
)
from wgsextract_cli.core import bwa_mem2
from wgsextract_cli.core.annotation_resources import get_genome_size
from wgsextract_cli.core.bgzf import open_decompressed
from wgsextract_cli.core.builds import build_from_path, canonical_build
from wgsextract_cli.core.constants import REFERENCE_MODELS
from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.download_progress import redact_urls
from wgsextract_cli.core.gene_map import are_gene_maps_installed
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.paths import absolute_path
from wgsextract_cli.core.ref_library.catalog import (
    get_available_genomes,
    get_genome_status,
)
from wgsextract_cli.core.ref_library.downloads import download_file
from wgsextract_cli.core.reference_assets import (
    fasta_prefix,
    n_count_paths,
    read_fai_lengths,
    reference_dict_path,
    sequence_signature,
    verify_sequence_signature,
)
from wgsextract_cli.core.reference_ncount import write_n_count_files
from wgsextract_cli.core.reference_processing import has_ref_ns
from wgsextract_cli.core.results import (
    current_result,
    file_snapshot,
    human_output,
    record_changed_files,
    warn,
)
from wgsextract_cli.core.utils import (
    UsageError,
    WGSExtractError,
    run_command,
)
from wgsextract_cli.core.variant_files import (
    resolve_reference,
    verify_paths_exist,
)


def run_download(params: RefDownloadParams) -> None:
    out = fspath(params.out)
    if Path(out).is_dir():
        raise WGSExtractError(f"Output path is a directory: {out}")

    url = redact_urls(params.url)
    logging.info(LOG_MESSAGES["ref_downloading"].format(url=url, path=out))
    if not download_file(params.url, out):
        raise WGSExtractError(f"Download failed: {url} -> {out}")
    current_result().add_output(out, "download")


def run_index(params: RefIndexParams) -> None:
    aligner = params.aligner or "bwa"
    verify_dependencies(["samtools", aligner])
    log_dependency_info(["samtools", aligner])
    if not params.ref:
        raise UsageError("--ref is required.")
    ref = fspath(params.ref)

    verify_paths_exist({"--ref": ref})

    logging.debug(f"Resolved reference: {str(absolute_path(ref))}")
    logging.info(LOG_MESSAGES["ref_indexing"].format(path=ref))
    # Only this FASTA's sidecars: its directory may hold other genomes.
    ref_dir, prefix = Path(ref).parent, Path(fasta_prefix(ref)).name
    before = file_snapshot(ref_dir, prefix=prefix, recursive=False)
    try:
        logging.info("Indexing FASTA with samtools faidx...")
        run_command(["samtools", "faidx", ref])

        out_dict = reference_dict_path(ref)
        logging.info(LOG_MESSAGES["ref_creating_dict"].format(path=out_dict))
        run_command(["samtools", "dict", ref, "-o", out_dict])

        if aligner == "bwa-mem2":
            bwa_mem2.ensure_index(ref, download=True)
        else:
            logging.info("Indexing FASTA with bwa index (required for alignment)...")
            from wgsextract_cli.core.bwa_index import ensure_index

            ensure_index(ref, runner=run_command)

        logging.info("Indexing complete.")
    except (OSError, subprocess.SubprocessError, WGSExtractError) as e:
        raise WGSExtractError(f"Indexing failed: {e}") from e
    record_changed_files(
        ref_dir, before, "reference_index", prefix=prefix, recursive=False
    )
    current_result().set_reference(ref)


def run_count_ns(params: RefCountNsParams) -> None:
    """Write <ref>_ncnt.csv / <ref>_nbin.csv next to the FASTA, or into --outdir."""
    if not params.ref:
        raise UsageError("--ref is required.")
    ref = fspath(params.ref)

    verify_paths_exist({"--ref": ref})

    outdir = fspath(params.outdir) if params.outdir else None
    if outdir:
        Path(outdir).mkdir(parents=True, exist_ok=True)
    ncnt_path, nbin_path = n_count_paths(ref, outdir)

    logging.info(LOG_MESSAGES["analyzing_ns"].format(path=ref))
    try:
        totals = write_n_count_files(ref, ncnt_path, nbin_path)
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
    result = current_result()
    result.add_output(ncnt_path, "n_counts")
    result.add_output(nbin_path, "n_bins")
    result.set_reference(ref)
    result.add_metrics(
        sequences=totals.sequences,
        length=totals.length,
        n_count=totals.n_count,
        large_n_runs=totals.large_runs,
        small_n_runs=totals.small_runs,
    )


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
                current_result().add_output(existing, "reference_index")
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


def cmd_count_ns(args: argparse.Namespace) -> None:
    if "outdir" not in getattr(args, "_explicit_dests", set()):
        args.outdir = None  # configured output_directory must not relocate sidecars
    _cli_count_ns(args)


def run_verify(params: RefVerifyParams) -> None:
    verify_dependencies(["samtools"])
    log_dependency_info(["samtools"])
    if not params.ref:
        raise UsageError("--ref is required.")
    ref = fspath(params.ref)

    # Auto-resolve ref if a directory was provided
    resolved_ref = resolve_reference(ref, "")
    logging.debug(f"Resolved reference: {resolved_ref}")
    if resolved_ref is None or not Path(resolved_ref).is_file():
        raise WGSExtractError(f"Reference file not found: {resolved_ref or ref}")

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
    current_result().set_reference(resolved_ref)
    current_result().add_metrics(contigs=len(lengths), signature=signature)
    if expected:
        logging.info(f"Verifying sequence dictionary signature for {filename}...")
        verify_sequence_signature(filename, lengths, expected)
        logging.info("Sequence dictionary signature: OK")
    else:
        logging.info(f"Sequence dictionary signature: {signature}")

    if not Path(reference_dict_path(resolved_ref)).exists():
        warn(
            "dict_missing",
            f"No sequence dictionary {reference_dict_path(resolved_ref)}; "
            "run 'wgsextract ref index' if GATK tools need it.",
            path=reference_dict_path(resolved_ref),
        )

    logging.info(LOG_MESSAGES["ref_valid"].format(filename=filename))


def _catalog_build(genome: dict[str, str]) -> str | None:
    model = REFERENCE_MODELS.get(genome.get("md5", ""))
    return canonical_build(model[0]) if model else build_from_path(genome["final"])


def _vep_cache(genome: dict[str, str], reflib_dir: str) -> str | None:
    """The listing's VEP label: ``VEP-GRCh37``/``VEP-GRCh38`` when a human
    genome's assembly has a cache in the library, ``VEP-Cache`` for a dog or
    cat one, else None."""
    bn = genome["final"].upper()
    # The catalog's signature says the build when the file name doesn't
    # (human_g1k_v37). VEP caches exist for GRCh37 and GRCh38 only.
    assembly = {"hg38": "GRCh38", "hg19": "GRCh37"}.get(
        _catalog_build(genome) or "", ""
    )
    if assembly:
        species = "homo_sapiens"
    elif "GSD" in bn or "DOG" in bn:
        species = "canis_lupus_familiaris"
    elif "FCA126" in bn or "CAT" in bn:
        species = "felis_catus"
    else:
        return None
    vep_path = Path(reflib_dir, "vep", species)
    if not vep_path.exists():
        return None
    if not assembly:
        return "VEP-Cache"
    # Any [version]_[assembly] folder.
    if vep_path.is_dir() and any(assembly in d for d in os.listdir(vep_path)):
        return f"VEP-{assembly}"
    return None


def _annotation_resources(reflib_dir: str) -> dict[str, dict[str, bool]]:
    ref_dir = Path(reflib_dir, "ref")

    def any_of(stem: str, *exts: str) -> bool:
        return any(Path(ref_dir, f"{stem}{ext}").exists() for ext in exts)

    return {
        build: {
            "clinvar": any_of(f"clinvar_{build}", ".vcf.gz"),
            "revel": any_of(f"revel_{build}", ".tsv.gz"),
            "phylop": any_of(f"phylop_{build}", ".bw", ".tsv.gz"),
            "gnomad": any_of(f"gnomad_{build}", ".vcf.bgz", ".vcf.gz"),
        }
        for build in ("hg19", "hg38")
    }


def run_library_list(params: RefLibraryListParams) -> None:
    """The library's genomes and annotation data, as ``details`` and the CLI's
    table."""
    reflib_dir = str(params.library())
    genomes = []
    vep_labels: list[str | None] = []
    for g in get_available_genomes():
        status = get_genome_status(g["final"], reflib_dir)
        vep_labels.append(_vep_cache(g, reflib_dir))
        genomes.append(
            {
                "code": g["code"],
                "label": g["label"],
                "file": g["final"],
                "status": status,
                "size": get_genome_size(g["final"], reflib_dir),
                # Bootstrap ships every catalog genome's N-counts; they matter
                # once it's here.
                "n_counts": status != "missing" and has_ref_ns(g["final"], reflib_dir),
                "vep_cache": vep_labels[-1] is not None,
            }
        )
    gene_maps = are_gene_maps_installed(reflib_dir)
    builds = _annotation_resources(reflib_dir)
    current_result().set_details(
        {
            "library": reflib_dir,
            "genomes": genomes,
            "resources": {"gene_maps": gene_maps, **builds},
        }
    )

    rule = "-" * 80
    human_output(f"\nREFERENCE LIBRARY: {reflib_dir}")
    human_output(rule)
    human_output(f"{'GENOME':<40} {'STATUS':<15} {'SIZE':<10} {'DETAILS'}")
    human_output(rule)
    for row, vep in zip(genomes, vep_labels, strict=True):
        details = ["N-counts"] if row["n_counts"] else []
        if vep:
            details.append(vep)
        human_output(
            f"{row['label']:<40} {row['status'].upper():<15} {row['size']:<10} "
            f"{', '.join(details)}"
        )
    human_output(rule)

    def installed(flag: bool) -> str:
        return "INSTALLED" if flag else "MISSING"

    human_output(f"{'Gene Maps (hg19/hg38)':<40} {installed(gene_maps)}")
    names = {"clinvar": "ClinVar", "revel": "REVEL", "phylop": "PhyloP"}
    for build, found in builds.items():
        for key, flag in found.items():
            name = f"{names.get(key, 'gnomAD')} ({build})"
            human_output(f"{name:<40} {installed(flag)}")
    human_output(rule)


cmd_download = cli_handler(RefDownloadParams, run_download)
cmd_index = cli_handler(RefIndexParams, run_index)
_cli_count_ns = cli_handler(RefCountNsParams, run_count_ns)
cmd_ref_verify = cli_handler(RefVerifyParams, run_verify)
cmd_library_list = cli_handler(RefLibraryListParams, run_library_list)
