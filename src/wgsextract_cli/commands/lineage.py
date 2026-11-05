import argparse
import csv
import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from wgsextract_cli.api._cli import add_arguments, cli_handler
from wgsextract_cli.api.lineage import MtHaplogroupParams, YHaplogroupParams
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.genotype_qc import MTDNA, warn_low_depth
from wgsextract_cli.core.haplogrep_trees import (
    DEFAULT_TREE,
    ensure_tree,
    write_config,
)
from wgsextract_cli.core.messages import CLI_HELP, LOG_MESSAGES, SYSTEM_MESSAGES
from wgsextract_cli.core.paths import absolute_path, java_local_path
from wgsextract_cli.core.reference_assets import resolve_reference_library
from wgsextract_cli.core.reference_models import MITO_BY_LENGTH, sequence_lengths
from wgsextract_cli.core.reference_resolver import ReferenceLibrary
from wgsextract_cli.core.results import (
    Scalar,
    current_result,
    human_output,
    json_mode,
    warn,
)
from wgsextract_cli.core.tool_catalog import JAVA_JAR
from wgsextract_cli.core.tool_commands import _tool_command_parts
from wgsextract_cli.core.utils import (
    MissingDependencyError,
    WGSExtractError,
    get_resource_defaults,
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

HAPLOGREP_BANNER = re.compile(r"Haplogrep 3 (\d\S*)")


def _jar_command(path: str, jar_name: str) -> list[str]:
    """How to run a Java tool. Bioconda's wrappers pass their own paths to java
    unquoted, so they fail whenever the install path has a space (the GUI's
    runtime lives in "Application Support"); run the jar beside them instead."""
    if Path(path).is_file():
        parts = [path]
    else:
        parts = _tool_command_parts(path)
    if len(parts) > 1:
        # A resolved command such as `java -jar .../haplogrep3.jar` runs as it is.
        return parts
    jar = parts[0]
    if not jar.lower().endswith(".jar"):
        resolved = shutil.which(jar) or jar
        sibling = Path(resolved).resolve().parent / jar_name
        if not sibling.is_file():
            return tool_command(jar)
        jar = str(sibling)
    verify_dependencies(["java"])
    return [*JAVA_JAR, java_local_path(jar)]


def _haplocheck_path(explicit: str | None) -> str:
    """haplocheck, which only --contamination needs: probed for, so the command
    doesn't publish it as needed on every run."""
    path = explicit or get_tool_path("haplocheck")
    if not path:
        raise MissingDependencyError(
            "--contamination needs haplocheck, which is not installed; install "
            "WGS Extract's bio-tools or pass --haplocheck-path."
        )
    if path == explicit:
        if Path(path).name == path:
            path = get_tool_path(path) or path
        if path == explicit:
            verify_paths_exist({"--haplocheck-path": path})
    return path


def _mutserve_command(haplocheck_cmd: list[str]) -> list[str]:
    """mutserve, the variant caller haplocheck's own workflow runs, ships
    beside haplocheck.jar (Bioconda's haplocheck package)."""
    for index, part in enumerate(haplocheck_cmd):
        if Path(part).name.lower() == "haplocheck.jar":
            mutserve = Path(part).with_name("mutserve.jar")
            if mutserve.is_file():
                return [*haplocheck_cmd[:index], java_local_path(mutserve)]
    raise MissingDependencyError(
        "haplocheck's mutserve.jar was not found beside haplocheck.jar; install "
        "the haplocheck package, or point --haplocheck-path at its haplocheck.jar."
    )


def _banner_version(cmd: list[str], pattern: re.Pattern[str], home: str) -> str | None:
    """The version a Java tool prints in its banner, before parsing arguments.
    A tool that can't start raises ToolLaunchError (a missing dependency)."""
    probe = run_command(cmd, capture_output=True, check=False, cwd=home)
    match = pattern.search(f"{probe.stdout}\n{probe.stderr}")
    return match.group(1) if match else None


def _reference_library(ref: Path | None) -> str:
    return resolve_reference_library(ref if ref is not None and ref.is_dir() else None)


def run_mt_haplogroup(params: MtHaplogroupParams, *, tree_links: bool = False) -> None:
    """``lineage mt-haplogroup``; ``tree_links`` prints a tree link (CLI only)."""
    input_file = str(params.input)
    outdir_arg = str(params.outdir) if params.outdir else None
    verify_paths_exist({"--input": input_file})
    input_lower = input_file.lower()
    is_alignment = input_lower.endswith((".bam", ".cram"))
    if not is_alignment and not input_lower.endswith(VCF_SUFFIXES):
        raise WGSExtractError("--input must be a BAM, CRAM or VCF file.")

    logging.info("Running mtDNA haplogroup analysis...")

    # Check dependencies only after cheap input validation so invalid invocations fail fast.
    haplogrep_path = resolve_lineage_tool(
        params.haplogrep_path, "haplogrep3", "--haplogrep-path"
    )
    tools = ["haplogrep3", "bcftools"]
    haplocheck_cmd: list[str] = []
    if params.contamination:
        haplocheck_cmd = _jar_command(
            _haplocheck_path(params.haplocheck_path), "haplocheck.jar"
        )
        tools.append("haplocheck")
        if is_alignment:
            tools.append("samtools")
    verify_dependencies(["bcftools"] + (["samtools"] if "samtools" in tools else []))
    log_dependency_info(tools)

    cmd = _jar_command(haplogrep_path, "haplogrep3.jar")
    current_result().record_tool_command(["haplogrep3"])

    outdir = outdir_arg or str(absolute_path(input_file).parent)
    Path(outdir).mkdir(parents=True, exist_ok=True)
    logging.debug(f"Input file: {str(absolute_path(input_file))}")
    logging.debug(f"Output directory: {str(absolute_path(outdir))}")
    out_file = str(Path(outdir, "haplogrep_results.txt"))

    reference = None
    contamination: dict[str, str] | None = None
    contamination_outputs: list[tuple[Path, str]] = []

    with tempfile.TemporaryDirectory(prefix=".wgse_haplogrep_", dir=outdir) as tmp:
        version = _banner_version(cmd, HAPLOGREP_BANNER, tmp)
        if version is None:
            raise WGSExtractError(
                f"{haplogrep_path} is not Haplogrep 3; mtDNA haplogroups need "
                "Haplogrep 3 (the haplogrep3 package)."
            )
        temp_vcf = str(Path(tmp, "mtdna.vcf.gz"))
        if is_alignment:
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
            if haplocheck_cmd:
                contamination_vcf = _call_heteroplasmies(
                    haplocheck_cmd, params, input_file, lib.fasta, chr_m, Path(tmp)
                )
            input_file = temp_vcf
        else:
            # Filter VCF to chrM to avoid Haplogrep errors on large VCFs
            chr_m = extract_vcf_chromosome(input_file, "MT", temp_vcf)
            _require_rcrs(temp_vcf, chr_m)
            input_file = temp_vcf
            if haplocheck_cmd:
                _require_heteroplasmy_levels(temp_vcf)
                contamination_vcf = temp_vcf

        tree = ensure_tree(params.tree or DEFAULT_TREE, _reference_library(params.ref))
        # Haplogrep 3 reads its configuration from its working directory.
        write_config(tree, Path(tmp))
        logging.info(LOG_MESSAGES["running_haplogrep"].format(input=input_file))
        try:
            run_command(
                cmd
                + ["classify", "--tree", tree.spec, "--extend-report"]
                + ["--in", java_local_path(absolute_path(input_file))]
                + ["--out", java_local_path(absolute_path(out_file))],
                cwd=tmp,
            )
        except (OSError, subprocess.SubprocessError) as e:
            raise WGSExtractError(f"Haplogrep failed: {e}") from e

        if haplocheck_cmd:
            contamination, contamination_outputs = _run_haplocheck(
                haplocheck_cmd, Path(contamination_vcf).name, Path(outdir), tmp
            )

    _print_haplogrep_result(
        out_file,
        reference,
        tree_links,
        details={
            "haplogrep_version": version,
            "tree": {"id": tree.id, "version": tree.version, "name": tree.name},
        },
        contamination=contamination,
    )
    for path, role in contamination_outputs:
        current_result().add_output(path, role)


cmd_mtdna = cli_handler(MtHaplogroupParams, run_mt_haplogroup, tree_links="tree_links")


def _call_heteroplasmies(
    haplocheck_cmd: list[str],
    params: MtHaplogroupParams,
    alignment: str,
    fasta: str,
    chr_m: str,
    tmp: Path,
) -> str:
    """Call chrM homo- and heteroplasmies with mutserve, as haplocheck's own
    workflow does: bcftools calls have no heteroplasmy levels. Returns the
    calls' file name in ``tmp``."""
    mutserve = _mutserve_command(haplocheck_cmd)
    # mutserve names the sample after the BAM file.
    sample = Path(alignment).name
    for suffix in (".bam", ".cram"):
        sample = sample.removesuffix(suffix)
    chrm_bam = f"{sample}.bam"
    run_command(
        ["samtools", "view", "-b", "-T", fasta, "-o", str(tmp / chrm_bam)]
        + [alignment, chr_m]
    )
    run_command(["samtools", "index", str(tmp / chrm_bam)])
    with (tmp / "chrM.fasta").open("w", encoding="utf-8", newline="\n") as out:
        run_command(["samtools", "faidx", fasta, chr_m], stdout=out)
    threads, _ = get_resource_defaults(params.threads, None)
    calls = "heteroplasmies.vcf.gz"
    logging.info("Calling mtDNA heteroplasmies with mutserve...")
    current_result().record_tool_command(["mutserve"])
    try:
        # mutserve names its text report after the output path up to the
        # path's first dot, so every path it gets is relative to tmp.
        # contract: haplocheck's workflow settings (level 1%, mapQ 30, baseQ 20).
        run_command(
            [*mutserve, "call", "--level", "0.01", "--mapQ", "30", "--baseQ", "20"]
            + ["--deletions", "--no-ansi", "--threads", str(threads)]
            + ["--contig-name", chr_m, "--reference", "chrM.fasta"]
            + ["--output", calls, chrm_bam],
            cwd=str(tmp),
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(f"mutserve failed: {e}") from e
    return calls


def _require_heteroplasmy_levels(vcf: str) -> None:
    header = run_command(["bcftools", "view", "-h", vcf], capture_output=True).stdout
    if "##FORMAT=<ID=AF," not in header:
        raise WGSExtractError(
            "--contamination needs heteroplasmy levels (FORMAT/AF), as mutserve or "
            "GATK Mutect2 write; this VCF has none. Use the BAM or CRAM instead."
        )


# haplocheck's report, its extended (--raw) table and its HTML report.
_HAPLOCHECK_OUTPUTS = (
    ("haplocheck_contamination.txt", "contamination_report"),
    ("haplocheck_contamination.raw.txt", "contamination_details"),
    ("haplocheck_contamination.html", "contamination_html"),
)


def _run_haplocheck(
    haplocheck_cmd: list[str], vcf: str, outdir: Path, tmp: str
) -> tuple[dict[str, str], list[tuple[Path, str]]]:
    """Run haplocheck on ``vcf`` (a file in ``tmp``); its first row and outputs."""
    logging.info("Checking mtDNA contamination with haplocheck...")
    current_result().record_tool_command(["haplocheck"])
    report = _HAPLOCHECK_OUTPUTS[0][0]
    try:
        # Relative paths, as for mutserve: it derives the other outputs' names.
        run_command([*haplocheck_cmd, "--raw", "--out", report, vcf], cwd=tmp)
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(f"haplocheck failed: {e}") from e
    outputs = []
    for name, role in _HAPLOCHECK_OUTPUTS:
        if Path(tmp, name).is_file():
            target = outdir / name
            Path(tmp, name).replace(target)
            outputs.append((target, role))
    rows = _read_tsv(outdir / report, "haplocheck")
    raw = outdir / _HAPLOCHECK_OUTPUTS[1][0]
    if raw.is_file():
        rows = _read_tsv(raw, "haplocheck") or rows
    if not rows:
        raise WGSExtractError(f"haplocheck wrote no result to {outdir / report}.")
    return rows[0], outputs


def _read_tsv(path: str | Path, tool: str) -> list[dict[str, str]]:
    try:
        with open(path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f, delimiter="\t"))
    except OSError as e:
        raise WGSExtractError(f"{tool} did not write {path}: {e}") from e


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


def _scalar(value: str | None) -> Scalar:
    try:
        return float(value) if value else None
    except ValueError:
        return value


# Haplogrep's extended-report columns, too long for the console summary.
_LONG_COLUMNS = {
    "Not_Found_Polys",
    "Found_Polys",
    "Remaining_Polys",
    "AAC_In_Remainings",
    "Input_Sample",
}


def _print_haplogrep_result(
    out_file: str,
    reference: str | None,
    tree_links: bool = False,
    *,
    details: dict[str, Any] | None = None,
    contamination: dict[str, str] | None = None,
) -> None:
    rows = _read_tsv(out_file, "Haplogrep")
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
    row = rows[0]
    result.set_details(
        {"haplogrep": row, **(details or {})}
        | ({"haplocheck": contamination} if contamination else {})
    )
    result.add_metrics(
        haplogroup=row["Haplogroup"].strip(),
        haplogroup_quality=_scalar(row.get("Quality")),
    )
    human_output("\n🧬 Haplogrep Lineage Result:")
    human_output("-" * 30)
    for header, value in row.items():
        if header and value and header not in _LONG_COLUMNS:
            human_output(f"{header:<15} : {value}")
    tree = (details or {}).get("tree")
    if tree:
        human_output(f"{'Tree':<15} : {tree['name']} ({tree['id']}@{tree['version']})")
    if contamination:
        _report_contamination(contamination)
    if tree_links and not json_mode():
        from ._lineage_links import print_tree_links

        print_tree_links(row["Haplogroup"], mitochondrial=True)
    human_output("-" * 30 + "\n")


def _report_contamination(row: dict[str, str]) -> None:
    status = (row.get("Contamination Status") or "").strip()
    level = (row.get("Contamination Level") or "").strip()
    result = current_result()
    result.add_metrics(
        mt_contamination=status or None,
        mt_contamination_level=_scalar(level if level != "ND" else None),
    )
    shown = status + (f" (level {level})" if level and level != "ND" else "")
    human_output(f"{'Contamination':<15} : {shown or 'unknown'} (haplocheck)")
    if status == "YES":
        warn(
            "mt_contamination",
            f"haplocheck found mtDNA contamination (level {level}): a second "
            f"haplogroup, {row.get('Minor Haplogroup') or 'unknown'}, is mixed in "
            f"with {row.get('Major Haplogroup') or 'the major one'}. The "
            "haplogroup call may be unreliable.",
            level=_scalar(level),
            major_haplogroup=row.get("Major Haplogroup"),
            minor_haplogroup=row.get("Minor Haplogroup"),
        )


def register(
    subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",
    base_parser: argparse.ArgumentParser,
) -> None:
    parser = subparsers.add_parser(
        "lineage", help="Native Yleaf or Haplogrep 3 analysis."
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
        "--tree-links",
        action="store_true",
        help="Print YFull and FamilyTreeDNA mtDNA tree links.",
    )
