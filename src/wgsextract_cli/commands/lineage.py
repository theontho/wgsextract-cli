import argparse
import csv
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from wgsextract_cli.api._cli import add_arguments, cli_handler
from wgsextract_cli.api.lineage import MtHaplogroupParams, YHaplogroupParams
from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.genotype_qc import MTDNA, warn_low_depth
from wgsextract_cli.core.messages import CLI_HELP, LOG_MESSAGES, SYSTEM_MESSAGES
from wgsextract_cli.core.paths import absolute_path, java_local_path
from wgsextract_cli.core.reference_models import MITO_BY_LENGTH, sequence_lengths
from wgsextract_cli.core.reference_resolver import ReferenceLibrary
from wgsextract_cli.core.results import Scalar, current_result, human_output, json_mode
from wgsextract_cli.core.tool_catalog import JAVA_JAR
from wgsextract_cli.core.utils import (
    WGSExtractError,
    run_command,
    run_pipeline,
)
from wgsextract_cli.core.variant_files import (
    calculate_bam_md5,
    get_bam_header,
    get_chr_name,
    verify_paths_exist,
)

from ._lineage_ydna import (
    VCF_SUFFIXES,
    cmd_ydna,
    extract_vcf_chromosome,
    resolve_lineage_tool,
    tool_command,
)
from ._lineage_ystr import add_ystr_parser


def _haplogrep_command(haplogrep_path: str) -> list[str]:
    """How to run Haplogrep. Bioconda's haplogrep 2.4 wrapper passes its own
    path to java unquoted, so it fails whenever the install path has a space
    (the GUI's runtime lives in "Application Support"); run its jar instead."""
    jar = haplogrep_path
    # A resolved command such as `java -jar .../haplogrep.jar` runs as it is.
    if not (jar.endswith(".jar") and Path(jar).is_file()):
        resolved = shutil.which(haplogrep_path) or haplogrep_path
        sibling = str(Path(resolved).resolve().parent / "haplogrep.jar")
        if not Path(sibling).is_file():
            return tool_command(haplogrep_path)
        jar = sibling
    verify_dependencies(["java"])
    return [*JAVA_JAR, java_local_path(jar)]


def run_mt_haplogroup(params: MtHaplogroupParams, *, tree_links: bool = False) -> None:
    """``lineage mt-haplogroup``; ``tree_links`` prints a tree link (CLI only)."""
    input_file = str(params.input)
    outdir_arg = str(params.outdir) if params.outdir else None
    verify_paths_exist({"--input": input_file})

    logging.info("Running mtDNA haplogroup analysis...")

    # Check dependencies only after cheap input validation so invalid invocations fail fast.
    haplogrep_path = resolve_lineage_tool(
        params.haplogrep_path, "haplogrep", "--haplogrep-path"
    )
    verify_dependencies(["bcftools"])
    log_dependency_info(["haplogrep", "bcftools"])

    cmd = _haplogrep_command(haplogrep_path)
    current_result().record_tool_command(["haplogrep"])

    outdir = outdir_arg or str(absolute_path(input_file).parent)
    Path(outdir).mkdir(parents=True, exist_ok=True)
    logging.debug(f"Input file: {str(absolute_path(input_file))}")
    logging.debug(f"Output directory: {str(absolute_path(outdir))}")
    out_file = str(Path(outdir, "haplogrep_results.txt"))

    input_lower = input_file.lower()
    reference = None

    with tempfile.TemporaryDirectory(prefix=".wgse_haplogrep_", dir=outdir) as tmp:
        temp_vcf = str(Path(tmp, "mtdna.vcf.gz"))
        if input_lower.endswith((".bam", ".cram")):
            logging.info(
                "Input is alignment. Extracting mitochondrial variants first..."
            )
            lib = ReferenceLibrary(
                str(params.ref) if params.ref else None,
                calculate_bam_md5(input_file, None),
                input_path=input_file,
            )
            if not lib.fasta:
                raise WGSExtractError(
                    "Reference genome required to call mitochondrial variants; pass --ref."
                )
            logging.debug(f"Resolved reference: {lib.fasta}")
            reference = lib.fasta
            current_result().set_reference(lib.fasta, lib.build)

            cram_opt = ["-T", lib.fasta] if input_lower.endswith(".cram") else []
            chr_m = get_chr_name(input_file, "MT", cram_opt)
            _require_rcrs(input_file, chr_m, cram_opt)
            warn_low_depth(
                str(params.input),
                {MTDNA},
                outdir=outdir_arg,
                ref=lib.fasta,
                min_depth=params.qc_min_depth,
            )
            run_pipeline(
                [
                    ["bcftools", "mpileup", "-Ou", "-r", chr_m, "-f", lib.fasta]
                    + [input_file],
                    ["bcftools", "call", "--ploidy", "1", "-mv", "-Oz", "-o"]
                    + [temp_vcf],
                ],
                stdout=subprocess.DEVNULL,
                description="Mitochondrial variant calling",
            )
            input_file = temp_vcf
        elif input_lower.endswith(VCF_SUFFIXES):
            # Filter VCF to chrM to avoid Haplogrep errors on large VCFs
            chr_m = extract_vcf_chromosome(input_file, "MT", temp_vcf)
            _require_rcrs(temp_vcf, chr_m)
            input_file = temp_vcf

        logging.info(LOG_MESSAGES["running_haplogrep"].format(input=input_file))
        try:
            run_command(
                cmd
                + ["classify", "--format", "vcf", "--in", java_local_path(input_file)]
                + ["--out", java_local_path(out_file)]
            )
        except (OSError, subprocess.SubprocessError) as e:
            raise WGSExtractError(f"Haplogrep failed: {e}") from e

    _print_haplogrep_result(out_file, reference, tree_links)


cmd_mtdna = cli_handler(MtHaplogroupParams, run_mt_haplogroup, tree_links="tree_links")


def _require_rcrs(path: str, chr_m: str, cram_opt: list[str] | None = None) -> None:
    """Refuse an mtDNA sequence that isn't the rCRS Haplogrep's positions assume."""
    length = sequence_lengths(get_bam_header(path, cram_opt)).get(chr_m)
    if length is None or MITO_BY_LENGTH.get(length) == "rCRS":
        return
    if MITO_BY_LENGTH.get(length) == "Yoruba":
        raise WGSExtractError(SYSTEM_MESSAGES["YorubaWarning"])
    raise WGSExtractError(
        f"{chr_m} is {length} bp, not the 16569 bp rCRS that Haplogrep's "
        "positions refer to."
    )


def _print_haplogrep_result(
    out_file: str, reference: str | None, tree_links: bool = False
) -> None:
    try:
        with open(out_file, newline="") as f:
            rows = list(csv.DictReader(f, delimiter="\t"))
    except OSError as e:
        raise WGSExtractError(f"Haplogrep did not write {out_file}: {e}") from e
    # Haplogrep writes a header alone when the input has no mtDNA variants, as
    # when the alignment was called against a reference of another build.
    if not rows or not (rows[0].get("Haplogroup") or "").strip():
        hint = (
            f"Check that the reference used ({reference}) is the one the input "
            "was aligned to; pass it with --ref."
            if reference
            else "Check that the VCF has mtDNA (chrM/MT) variant calls."
        )
        raise WGSExtractError(
            f"Haplogrep could not predict an mtDNA haplogroup: it found no "
            f"mtDNA variants. {hint}"
        )
    result = current_result()
    result.add_output(out_file, "haplogroup_report")
    result.set_details({"haplogrep": rows[0]})
    quality = rows[0].get("Quality")
    try:
        quality_value: Scalar = float(quality) if quality else None
    except ValueError:
        quality_value = quality
    result.add_metrics(
        haplogroup=rows[0]["Haplogroup"].strip(), haplogroup_quality=quality_value
    )
    human_output("\n🧬 Haplogrep Lineage Result:")
    human_output("-" * 30)
    for header, value in rows[0].items():
        if header and value:
            human_output(f"{header:<15} : {value}")
    if tree_links and not json_mode():
        from ._lineage_links import print_tree_links

        print_tree_links(rows[0]["Haplogroup"], mitochondrial=True)
    human_output("-" * 30 + "\n")


def register(
    subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",
    base_parser: argparse.ArgumentParser,
) -> None:
    parser = subparsers.add_parser(
        "lineage", help="Native Yleaf or Haplogrep analysis."
    )
    lin_subs = parser.add_subparsers(dest="lin_cmd", required=True)

    ydna_parser = lin_subs.add_parser(
        "y-haplogroup", parents=[base_parser], help=CLI_HELP["cmd_lineage-y-haplogroup"]
    )
    add_arguments(ydna_parser, YHaplogroupParams)
    ydna_parser.set_defaults(func=cmd_ydna)
    ydna_parser.add_argument(
        "--tree-links", action="store_true", help="Print YFull and FTDNA tree links."
    )

    add_ystr_parser(lin_subs, base_parser, CLI_HELP["cmd_lineage-y-str"])

    mtdna_parser = lin_subs.add_parser(
        "mt-haplogroup",
        parents=[base_parser],
        help=CLI_HELP["cmd_lineage-mt-haplogroup"],
    )
    add_arguments(mtdna_parser, MtHaplogroupParams)
    mtdna_parser.set_defaults(func=cmd_mtdna)
    mtdna_parser.add_argument(
        "--tree-links", action="store_true", help="Print a YFull mtDNA tree link."
    )
