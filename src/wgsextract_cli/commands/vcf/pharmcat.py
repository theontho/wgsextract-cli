"""`vcf pharmcat`: star-allele pharmacogenomics with PharmCAT.

PharmCAT (https://pharmcat.org) calls diplotypes and metabolizer phenotypes
(CYP2C19 *17/*17, Ultrarapid Metabolizer, ...) for the CPIC/DPWG genes from a
GRCh38 VCF and reports the matching prescribing guidance. It isn't packaged for
conda, so the user extracts its release ("pharmcat-pipeline-<version>.tar.gz")
into <reference library>/pharmcat, or points pharmcat_directory / --pharmcat-dir
at it; the `pharmcat` Pixi environment supplies its Python, Java and bcftools.

PharmCAT takes neither gVCF reference blocks nor every FORMAT field a caller
writes, so the input is first cut to PharmCAT's gene regions, given its contig
names, expanded from blocks to positions, and reduced to genotypes.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import subprocess
import tempfile

from wgsextract_cli.core.dependencies import pixi_env_tool
from wgsextract_cli.core.dependency_checks import verify_dependencies
from wgsextract_cli.core.reference_resolver import ReferenceLibrary
from wgsextract_cli.core.utils import WGSExtractError, run_command
from wgsextract_cli.core.variant_files import (
    chromosome_rename_mapping,
    ensure_vcf_indexed,
    vcf_index_chromosomes,
)

from .basic import require_vcf_input

RELEASES_URL = "https://github.com/PharmGKB/PharmCAT/releases"
PIPELINE_FILES = ("pharmcat_pipeline", "pharmcat.jar", "pharmcat_regions.bed")
# Shipped with the release's preprocessor; fetched by PharmCAT on first use.
REFERENCE_FASTA = "reference.fna.bgz"


def pharmcat_directory(args: argparse.Namespace, lib: ReferenceLibrary) -> str:
    """The extracted PharmCAT pipeline: --pharmcat-dir, pharmcat_directory, or
    <reference library>/pharmcat."""
    directory = args.pharmcat_dir or (
        os.path.join(lib.root, "pharmcat") if lib.root else None
    )
    missing = [
        name
        for name in PIPELINE_FILES
        if not directory or not os.path.isfile(os.path.join(directory, name))
    ]
    if missing:
        raise WGSExtractError(
            f"PharmCAT not found in {directory or '(no reference library)'} "
            f"(missing {', '.join(missing)}). Download pharmcat-pipeline-<version>"
            f".tar.gz from {RELEASES_URL}, extract it there (or set "
            "pharmcat_directory in config.toml, or pass --pharmcat-dir)."
        )
    return directory  # type: ignore[return-value]


def _python(directory: str) -> list[str]:
    command = pixi_env_tool("pharmcat_python")
    if not command:
        raise WGSExtractError(
            "PharmCAT's environment isn't available (it runs on macOS and Linux): "
            "pixi install -e pharmcat"
        )
    return shlex.split(command)


def _ensure_reference(python: list[str], directory: str) -> str:
    reference = os.path.join(directory, REFERENCE_FASTA)
    if not os.path.isfile(reference):
        logging.info("Fetching PharmCAT's GRCh38 reference (about 1 GB, once)...")
        fetch = (
            "import sys; sys.path.insert(0, sys.argv[1]); import pcat; "
            "pcat.download_reference_fasta_and_index(sys.argv[1])"
        )
        run_command([*python, "-c", fetch, directory])
    return reference


def prepare_input(input_vcf: str, directory: str, reference: str, work: str) -> str:
    """input_vcf cut to PharmCAT's regions, in its contig names, with gVCF
    blocks expanded to positions and only GT kept."""
    regions = os.path.join(directory, "pharmcat_regions.bed")
    with open(regions) as f:
        bed_lines = [line.split("\t") for line in f if line.strip()]
    pharmcat_chroms = sorted({fields[0] for fields in bed_lines})
    input_chroms = vcf_index_chromosomes(input_vcf)

    # The regions, in the input's names.
    to_input = dict(chromosome_rename_mapping(pharmcat_chroms, input_chroms))
    local_bed = os.path.join(work, "regions.bed")
    with open(local_bed, "w") as f:
        f.writelines(
            "\t".join([to_input.get(fields[0], fields[0]), *fields[1:]])
            for fields in bed_lines
        )
    subset = os.path.join(work, "regions.vcf.gz")
    run_command(["bcftools", "view", "-R", local_bed, "-Oz", "-o", subset, input_vcf])
    ensure_vcf_indexed(subset)

    renamed = subset
    to_pharmcat = chromosome_rename_mapping(
        vcf_index_chromosomes(subset), pharmcat_chroms
    )
    if to_pharmcat:
        names = os.path.join(work, "names.txt")
        with open(names, "w") as f:
            f.writelines(f"{source} {target}\n" for source, target in to_pharmcat)
        renamed = os.path.join(work, "renamed.vcf.gz")
        run_command(["bcftools", "annotate", "--rename-chrs", names, "-Oz", "-o",
                     renamed, subset])  # fmt: skip
        ensure_vcf_indexed(renamed)

    prepared = os.path.join(work, "prepared.vcf.gz")
    expanded = os.path.join(work, "expanded.vcf.gz")
    run_command(["bcftools", "convert", "--gvcf2vcf", "-f", reference, "-Oz", "-o",
                 expanded, renamed])  # fmt: skip
    run_command(["bcftools", "annotate", "-x", "INFO,^FORMAT/GT", "-Oz", "-o",
                 prepared, expanded])  # fmt: skip
    ensure_vcf_indexed(prepared)
    return prepared


def summarize(phenotype_json: str) -> list[tuple[str, str, str]]:
    """(gene, diplotype, phenotype) for each gene PharmCAT called."""
    with open(phenotype_json) as f:
        reports = json.load(f).get("geneReports", {})
    rows = []
    for gene, report in sorted(reports.items()):
        for diplotype in report.get("recommendationDiplotypes") or []:
            alleles = [
                (diplotype.get(side) or {}).get("name") or "?"
                for side in ("allele1", "allele2")
            ]
            if "Unknown" in alleles:
                continue
            phenotype = ", ".join(diplotype.get("phenotypes") or []) or "-"
            rows.append((gene, "/".join(alleles), phenotype))
    return rows


def cmd_pharmcat(args: argparse.Namespace) -> None:
    verify_dependencies(["bcftools", "tabix"])
    input_vcf = require_vcf_input(args)
    outdir = args.outdir or os.path.dirname(os.path.abspath(input_vcf))
    os.makedirs(outdir, exist_ok=True)
    lib = ReferenceLibrary(getattr(args, "ref", None), None, input_path=input_vcf)
    if lib.build and "38" not in lib.build:
        raise WGSExtractError(
            f"PharmCAT needs a GRCh38 VCF; {input_vcf} is {lib.build}."
        )
    directory = pharmcat_directory(args, lib)
    python = _python(directory)
    ensure_vcf_indexed(input_vcf)
    reference = _ensure_reference(python, directory)
    stem = os.path.basename(input_vcf).split(".")[0]

    with tempfile.TemporaryDirectory(dir=outdir, prefix=".wgse_pharmcat_") as work:
        logging.info("Preparing the VCF for PharmCAT (its gene regions only)...")
        prepared = prepare_input(input_vcf, directory, reference, work)
        command = [
            *python, os.path.join(directory, "pharmcat_pipeline"), prepared,
            "-o", outdir, "-bf", stem, "-reporterHtml", "-reporterJson",
        ]  # fmt: skip
        if args.absent_to_ref:
            command.append("--absent-to-ref")
        logging.info("Running PharmCAT...")
        try:
            run_command(command)
        except subprocess.CalledProcessError as e:
            raise WGSExtractError(f"PharmCAT failed (exit {e.returncode}).") from e

    report = os.path.join(outdir, f"{stem}.report.html")
    for gene, diplotype, phenotype in summarize(
        os.path.join(outdir, f"{stem}.phenotype.json")
    ):
        logging.info(f"  {gene:<9} {diplotype:<22} {phenotype}")
    logging.info(
        f"PharmCAT report: {report} (CYP2D6 and HLA genes need calls from "
        "dedicated tools; PharmCAT can't call them from a VCF)"
    )
