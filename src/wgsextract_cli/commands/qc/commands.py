import argparse
import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path

from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.genome_library import pair_stem
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.paths import absolute_path, java_local_path
from wgsextract_cli.core.results import current_result
from wgsextract_cli.core.utils import (
    UsageError,
    WGSExtractError,
    get_resource_defaults,
    run_command,
)

from .._extract_helpers import cleanup_files
from ..align import reads_stem
from ..vcf.basic import _select_vcf_input

# The MultiQC modules `qc multiqc` runs: FastQC reports, `bcftools stats` from
# `qc vcf`, and custom content (the WGS Extract metrics).
MULTIQC_MODULES = ("fastqc", "bcftools", "custom_content")


def cmd_fastp(args: argparse.Namespace) -> None:
    if not args.r1:
        raise UsageError("--r1 is required unless --genome resolves FASTQ inputs.")

    from wgsextract_cli.core.variant_files import verify_paths_exist

    paths = {"--r1": args.r1}
    if args.r2:
        paths["--r2"] = args.r2
    verify_paths_exist(paths)

    verify_dependencies(["fastp"])
    log_dependency_info(["fastp"])

    threads, _ = get_resource_defaults(args.threads, None)
    outdir = args.outdir if args.outdir else str(absolute_path(args.r1).parent)

    logging.debug(f"Input file (R1): {str(absolute_path(args.r1))}")
    if args.r2:
        logging.debug(f"Input file (R2): {str(absolute_path(args.r2))}")
    logging.debug(f"Output directory: {str(absolute_path(outdir))}")
    Path(outdir).mkdir(parents=True, exist_ok=True)

    base_name = fastp_stem(args.r1, args.r2)
    out_r1 = str(Path(outdir, f"{base_name}_fp_1.fastq.gz"))
    out_json = str(Path(outdir, f"{base_name}_fastp.json"))
    out_html = str(Path(outdir, f"{base_name}_fastp.html"))

    cmd = [
        "fastp",
        "--thread",
        threads,
        "-i",
        args.r1,
        "-o",
        out_r1,
        "-j",
        out_json,
        "-h",
        out_html,
    ]
    if args.r2:
        out_r2 = str(Path(outdir, f"{base_name}_fp_2.fastq.gz"))
        cmd.extend(["-I", args.r2, "-O", out_r2])

    logging.info(LOG_MESSAGES["running_fastp"].format(input=args.r1))
    outputs = [out_r1, out_json, out_html] + ([out_r2] if args.r2 else [])
    try:
        with cleanup_files(on_failure=outputs):
            run_command(cmd)
            # fastp exits 0 on a file that isn't FASTQ, reporting no reads.
            summary = _fastp_summary(out_json)
            if summary.get("reads_before_filtering") == 0:
                raise WGSExtractError(
                    f"fastp found no reads in {args.r1}: is it a FASTQ file?"
                )
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(f"fastp failed: {e}") from e
    result = current_result()
    result.add_output(out_r1, "trimmed_fastq_r1")
    if args.r2:
        result.add_output(out_r2, "trimmed_fastq_r2")
    result.add_output(out_json, "fastp_report_json")
    result.add_output(out_html, "fastp_report_html")
    result.set_manifest_stem(Path(outdir, f"{base_name}_fastp"))
    result.add_metrics(**summary)


def fastp_stem(r1: str, r2: str | None) -> str:
    """The name qc fastp gives its outputs: the reads' stem, without the mate
    marker when both mates' outputs share it."""
    return pair_stem(reads_stem(r1)) if r2 else reads_stem(r1)


def _fastp_summary(report: str) -> dict[str, int | float]:
    """Read counts and rates from a fastp JSON report; empty if unreadable."""
    try:
        with open(report) as f:
            document = json.load(f)
        before = document["summary"]["before_filtering"]
        after = document["summary"]["after_filtering"]
        summary: dict[str, int | float] = {
            "reads_before_filtering": int(before["total_reads"]),
            "reads_after_filtering": int(after["total_reads"]),
            "q30_rate_after_filtering": float(after["q30_rate"]),
        }
        duplication = document.get("duplication", {}).get("rate")
        if duplication is not None:
            summary["duplication_rate"] = float(duplication)
        return summary
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def cmd_fastqc(args: argparse.Namespace) -> None:
    if not args.input:
        raise UsageError(LOG_MESSAGES["input_required"])

    from wgsextract_cli.core.variant_files import verify_paths_exist

    verify_paths_exist({"--input": args.input})

    verify_dependencies(["fastqc"])
    log_dependency_info(["fastqc"])

    threads, _ = get_resource_defaults(args.threads, None)
    outdir = args.outdir if args.outdir else str(absolute_path(args.input).parent)

    logging.debug(f"Input file: {str(absolute_path(args.input))}")
    logging.debug(f"Output directory: {str(absolute_path(outdir))}")
    Path(outdir).mkdir(parents=True, exist_ok=True)

    logging.info(LOG_MESSAGES["running_fastqc"].format(input=args.input))
    try:
        run_command(
            [
                "fastqc",
                "-t",
                threads,
                "--outdir",
                java_local_path(outdir),
                java_local_path(args.input),
            ]
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(f"FastQC failed: {e}") from e
    stem = Path(outdir, fastqc_stem(args.input))
    report_html = stem.with_name(stem.name + ".html")
    report_zip = stem.with_name(stem.name + ".zip")
    result = current_result()
    result.add_output(report_html, "fastqc_report_html")
    result.add_output(report_zip, "fastqc_report_zip")
    result.add_metrics(**_fastqc_summary(report_zip))


def fastqc_stem(path: str) -> str:
    """FastQC's report name for an input: its name minus the extensions FastQC
    strips, plus ``_fastqc``."""
    name = Path(path).name
    for ext in (".gz", ".bz2", ".txt", ".fastq", ".fq", ".csfastq", ".sam", ".bam"):
        name = name.removesuffix(ext)
    return name + "_fastqc"


def _fastqc_summary(report_zip: Path) -> dict[str, int]:
    """Module PASS/WARN/FAIL counts and total sequences from a FastQC zip."""
    import zipfile

    try:
        with zipfile.ZipFile(report_zip) as archive:
            folder = report_zip.stem
            summary = archive.read(f"{folder}/summary.txt").decode()
            data = archive.read(f"{folder}/fastqc_data.txt").decode()
    except (OSError, KeyError, zipfile.BadZipFile):
        return {}
    statuses = [line.split("\t", 1)[0] for line in summary.splitlines() if line]
    metrics = {
        f"modules_{status.lower()}": statuses.count(status)
        for status in ("PASS", "WARN", "FAIL")
    }
    for line in data.splitlines():
        if line.startswith("Total Sequences\t"):
            metrics["total_sequences"] = int(line.split("\t")[1])
            break
    return metrics


def cmd_multiqc_metrics(args: argparse.Namespace) -> None:
    """Write MultiQC custom-content files from result manifests (no MultiQC
    needed)."""
    from wgsextract_cli.core import multiqc
    from wgsextract_cli.core.results import human_output

    if not args.input:
        raise UsageError(
            "--input is required: choose result manifests or a directory of them."
        )
    source = absolute_path(args.input)
    if not source.is_file() and not source.is_dir():
        raise WGSExtractError(
            f"--input must be an existing file or directory: {source}."
        )
    outdir = (
        absolute_path(args.outdir)
        if args.outdir
        else (source if source.is_dir() else source.parent)
    )
    found = multiqc.find_documents(source)
    if not found:
        raise WGSExtractError(
            f"No WGS Extract result manifests (*.manifest.json or saved --json "
            f"output) found in {source}."
        )
    files = multiqc.custom_content(document for _path, document in found)
    if not files:
        raise WGSExtractError(
            f"None of the {len(found)} result manifests in {source} has metrics "
            "MultiQC can show (info, lineage, microarray or variant calling)."
        )
    try:
        written = multiqc.write_custom_content(files, outdir)
    except OSError as e:
        raise WGSExtractError(f"Could not write MultiQC metrics: {e}") from e

    result = current_result()
    result.set_manifest_stem(outdir / "wgsextract_mqc")
    for path, _document in found:
        result.add_input(path, "result_manifest")
    for path in written:
        result.add_output(path, "multiqc_custom_content")
    samples = {
        sample for content in files.values() for sample in content.get("data", {})
    }
    result.add_metrics(manifests=len(found), samples=len(samples), files=len(written))
    for path in written:
        human_output(path)
    logging.info(
        f"Wrote {len(written)} MultiQC custom-content files for {len(samples)} "
        f"samples from {len(found)} result manifests."
    )


def cmd_multiqc(args: argparse.Namespace) -> None:
    if not args.input:
        raise UsageError(
            "--input is required: choose FastQC reports, bcftools stats or result "
            "manifests."
        )

    source = absolute_path(args.input)
    if not source.is_file() and not source.is_dir():
        raise WGSExtractError(
            f"--input must be an existing file or directory: {source}."
        )
    outdir = (
        absolute_path(args.outdir)
        if args.outdir
        else (source if source.is_dir() else source.parent)
    )
    report = outdir / "multiqc_report.html"
    exists = report.exists() or report.is_symlink()
    if exists and not args.force:
        raise WGSExtractError(
            f"Report already exists: {report}. Choose another --outdir or use --force."
        )
    if exists and not report.is_file():
        raise WGSExtractError(f"Report path is not a file: {report}.")

    verify_dependencies(["multiqc"])
    log_dependency_info(["multiqc"])
    from wgsextract_cli.core import multiqc

    documents = [document for _path, document in multiqc.find_documents(source)]
    try:
        outdir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".multiqc_", dir=outdir) as work:
            metrics = Path(work, "wgsextract_metrics")
            multiqc.write_custom_content(multiqc.custom_content(documents), metrics)
            # Home-directory MultiQC configuration can otherwise enable AI uploads.
            config = json.dumps(
                {
                    "ai_summary": False,
                    "ai_summary_full": False,
                    "output_fn_name": report.name,
                }
            )
            run_command(
                [
                    "multiqc",
                    *(
                        arg
                        for module in MULTIQC_MODULES
                        for arg in ("--module", module)
                    ),
                    "--no-data-dir",
                    "--no-megaqc-upload",
                    "--no-version-check",
                    "--no-ai",
                    "--cl-config",
                    config,
                    "--filename",
                    report.name,
                    "--outdir",
                    work,
                    str(source),
                    str(metrics),
                ],
                cwd=work,
                env={**os.environ, "TMPDIR": work},
            )
            staged = Path(work, report.name)
            if not staged.is_file() or staged.stat().st_size == 0:
                raise WGSExtractError(
                    f"MultiQC failed: it produced no report, finding no FastQC "
                    f"reports, bcftools stats or WGS Extract result manifests in "
                    f"{source}."
                )
            if args.force:
                os.replace(staged, report)
            else:
                try:
                    os.link(staged, report)
                except FileExistsError as e:
                    raise WGSExtractError(
                        f"Report already exists: {report}. Choose another --outdir or use --force."
                    ) from e
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(f"MultiQC failed: {e}") from e
    result = current_result()
    result.add_output(report, "multiqc_report")
    result.add_metrics(manifests=len(documents))
    logging.info("MultiQC report: %s", report)


def cmd_vcf_qc(args: argparse.Namespace) -> None:
    input_file = _select_vcf_input(args)
    if not input_file:
        raise UsageError("--input is required.")

    from wgsextract_cli.core.variant_files import verify_paths_exist

    verify_paths_exist({"--input": input_file})

    verify_dependencies(["bcftools"])
    log_dependency_info(["bcftools"])

    logging.debug(f"Input file: {str(absolute_path(input_file))}")

    outdir = args.outdir if args.outdir else str(absolute_path(input_file).parent)
    logging.debug(f"Output directory: {str(absolute_path(outdir))}")
    Path(outdir).mkdir(parents=True, exist_ok=True)
    base_name = Path(input_file).name
    out_stats = str(Path(outdir, f"{base_name}.vcfstats.txt"))

    logging.info(LOG_MESSAGES["vcf_stats"].format(input=input_file, output=out_stats))
    try:
        with (
            cleanup_files(on_failure=[out_stats]),
            open(out_stats, "w", newline="\n") as f,
        ):
            run_command(["bcftools", "stats", input_file], stdout=f)
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(f"VCF stats failed: {e}") from e
    stats = bcftools_stats_summary(out_stats)
    current_result().add_output(out_stats, "vcf_stats", metrics=stats)
    current_result().add_metrics(**stats)


# bcftools stats "SN" keys -> metric names.
_STATS_KEYS = {
    "number of samples:": "samples",
    "number of records:": "records",
    "number of SNPs:": "snps",
    "number of MNPs:": "mnps",
    "number of indels:": "indels",
    "number of multiallelic sites:": "multiallelic_sites",
}


def bcftools_stats_summary(path: str) -> dict[str, int | float]:
    """Counts and Ts/Tv for all records (id 0) from a bcftools stats file."""
    metrics: dict[str, int | float] = {}
    with open(path) as f:
        for line in f:
            fields = line.rstrip("\n").split("\t")
            if fields[0] == "SN" and len(fields) >= 4 and fields[1] == "0":
                if key := _STATS_KEYS.get(fields[2]):
                    metrics[key] = int(fields[3])
            elif fields[0] == "TSTV" and len(fields) >= 5 and fields[1] == "0":
                metrics["ts_tv"] = float(fields[4])
    return metrics
