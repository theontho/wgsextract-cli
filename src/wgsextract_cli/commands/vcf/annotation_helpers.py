import argparse
import logging
import subprocess
import tempfile
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from wgsextract_cli.core.annotation_resources import is_bgzf
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.output_checks import warn_if_nothing_annotated
from wgsextract_cli.core.paths import absolute_path
from wgsextract_cli.core.reference_resolver import ReferenceLibrary
from wgsextract_cli.core.utils import (
    WGSExtractError,
    get_resource_defaults,
    run_command,
)
from wgsextract_cli.core.variant_files import (
    calculate_bam_md5,
    ensure_vcf_indexed,
    normalize_vcf_chromosomes,
    vcf_index_chromosomes,
)

from .basic import require_vcf_input

VCF_SUFFIXES = (".vcf", ".vcf.gz", ".vcf.bgz")


def annotation_context(args: argparse.Namespace) -> tuple[str, str, ReferenceLibrary]:
    input_file = require_vcf_input(args)
    outdir = getattr(args, "outdir", None) or str(absolute_path(input_file).parent)
    Path(outdir).mkdir(parents=True, exist_ok=True)
    md5_sig = (
        calculate_bam_md5(input_file, None)
        if input_file.lower().endswith((".bam", ".cram"))
        else None
    )
    lib = ReferenceLibrary(getattr(args, "ref", None), md5_sig, input_path=input_file)
    return input_file, outdir, lib


def require_annotation_file(file_path: str | None, message_key: str, ann: str) -> str:
    """Return the resolved annotation resource or raise with a fix-it hint."""
    if not file_path:
        msg = LOG_MESSAGES.get(message_key, f"Missing data file for {ann}")
        raise WGSExtractError(
            f"REQUIRED DATA MISSING: {msg}\nTo fix this, run: wgsextract ref {ann}"
        )
    return str(file_path)


def prepare_tabix_annotation(
    annotation_path: str,
    label: str,
    seq_col: str = "1",
    begin_col: str = "2",
    end_col: str = "2",
) -> str:
    if annotation_path.lower().endswith(VCF_SUFFIXES):
        from wgsextract_cli.core.variant_files import ensure_vcf_prepared

        return str(ensure_vcf_prepared(annotation_path))

    prepared_path = annotation_path
    if not annotation_path.lower().endswith((".gz", ".bgz")):
        prepared_path = annotation_path + ".gz"
        if (
            not Path(prepared_path).exists()
            or Path(prepared_path).stat().st_mtime
            <= Path(annotation_path).stat().st_mtime
        ):
            bgzip = get_tool_path("bgzip")
            logging.info(
                "Compressing %s annotation resource: %s", label, annotation_path
            )
            with open(prepared_path, "wb") as f_out:
                run_command([bgzip, "-c", annotation_path], stdout=f_out)

    if not is_bgzf(prepared_path):
        raise WGSExtractError(
            f"{label} resource {prepared_path} is plain gzip, not BGZF, so it cannot "
            f"be indexed. Recompress it with 'gzip -dc FILE | bgzip -c > FILE.bgz' "
            f"or re-download it with 'wgsextract ref'."
        )
    index_path = prepared_path + ".tbi"
    needs_index = not Path(index_path).exists() or (
        Path(index_path).stat().st_mtime <= Path(prepared_path).stat().st_mtime
    )
    if needs_index:
        tabix = get_tool_path("tabix")
        logging.info("Indexing %s annotation resource: %s", label, prepared_path)
        try:
            run_command(
                [tabix, "-f", "-s", seq_col, "-b", begin_col, "-e", end_col]
                + [prepared_path],
                capture_output=True,
            )
        except (OSError, subprocess.SubprocessError) as e:
            raise WGSExtractError(f"Indexing {prepared_path} failed: {e}") from e
    return prepared_path


def annotation_chromosomes(annotation_path: str) -> list[str]:
    """Contigs in an indexed annotation resource (VCF or tabix-indexed TSV)."""
    if annotation_path.lower().endswith(VCF_SUFFIXES):
        return vcf_index_chromosomes(annotation_path)
    try:
        res = run_command(["tabix", "-l", annotation_path], capture_output=True)
    except (OSError, subprocess.SubprocessError) as e:
        logging.debug(f"Could not list contigs of {annotation_path}: {e}")
        return []
    return [line for line in (res.stdout or "").splitlines() if line.strip()]


def _contigs_by_size(vcf_path: str) -> list[str]:
    """Indexed contigs that hold records, longest first (for load balancing)."""
    try:
        res = run_command(["bcftools", "index", "-s", vcf_path], capture_output=True)
    except (OSError, subprocess.SubprocessError):
        return []
    rows = [line.split("\t") for line in (res.stdout or "").splitlines() if line]
    rows.sort(key=lambda r: -int(r[1]) if r[1].isdigit() else 0)
    return [r[0] for r in rows]


def _run_annotate(cmd: list[str], vcf: str, out: str, work: str, threads: int) -> None:
    """Run bcftools annotate, splitting by contig across threads when it helps."""
    contigs = _contigs_by_size(vcf) if threads > 1 else []
    if len(contigs) < 2:
        run_command([*cmd, "-Oz", "-o", out, vcf], capture_output=True)
        return
    # Each contig streams its own slice of the resource, so whole genomes scale
    # with cores; --no-version keeps the part headers identical for --naive.
    parts = {c: str(Path(work, f"part{i}.vcf.gz")) for i, c in enumerate(contigs)}
    # A regions file, as `-r` can't parse contig names with ':' (hs38DH's HLA-*).
    regions = {}
    for c, part in parts.items():
        regions[c] = part[: -len(".vcf.gz")] + ".regions.txt"
        with open(regions[c], "w") as f:
            f.write(f"{c}\t1\t2147483647\n")
    with ThreadPoolExecutor(max_workers=threads) as pool:
        futures = [
            pool.submit(
                run_command,
                [*cmd, "--no-version", "-R", regions[c], "-Oz", "-o", part, vcf],
                capture_output=True,
            )
            for c, part in parts.items()
        ]
        for future in futures:
            future.result()
    in_order = [parts[c] for c in vcf_index_chromosomes(vcf)]
    list_file = str(Path(work, "parts.txt"))
    with open(list_file, "w") as f:
        f.writelines(p + "\n" for p in in_order)
    run_command(
        ["bcftools", "concat", "--naive", "-f", list_file, "-Oz", "-o", out],
        capture_output=True,
    )
    for path in (*in_order, *regions.values()):
        Path(path).unlink()


def annotate_vcf(
    input_vcf: str,
    annotation: str,
    columns: str,
    ann_out: str,
    outdir: str,
    label: str,
    header_lines: Sequence[str] = (),
    threads: int | str | None = None,
) -> None:
    """Run bcftools annotate after matching the input's contig naming to the resource."""
    n_threads = int(get_resource_defaults(threads)[0])
    with tempfile.TemporaryDirectory(dir=outdir, prefix=".wgse_annotate_") as work:
        normalized = normalize_vcf_chromosomes(
            input_vcf, annotation_chromosomes(annotation), work
        )
        cmd = ["bcftools", "annotate", "-a", annotation, "-c", columns]
        if {"REF", "ALT"} <= set(columns.split(",")):
            # Allele-matched scores can never apply to gVCF reference blocks, and
            # looking up every resource line under their END spans makes whole
            # genomes ~25x slower; pass those records through untouched.
            cmd += ["-e", 'TYPE="ref"', "-k"]
        if header_lines:
            header_path = str(Path(work, "annotation.hdr"))
            with open(header_path, "w") as f:
                f.writelines(line + "\n" for line in header_lines)
            cmd += ["-h", header_path]
        annotated = (
            ann_out if normalized == input_vcf else str(Path(work, "annotated.vcf.gz"))
        )
        try:
            _run_annotate(cmd, normalized, annotated, work, n_threads)
            ensure_vcf_indexed(annotated)
            if annotated != ann_out:
                # Back to the input's contig names (chr1 vs 1), so a chain of
                # annotations keeps them whatever each resource uses.
                restored = normalize_vcf_chromosomes(
                    annotated, vcf_index_chromosomes(input_vcf), work
                )
                Path(restored).replace(ann_out)
                for index in (ann_out + ".tbi", ann_out + ".csi"):
                    if Path(index).exists():
                        Path(index).unlink()
                ensure_vcf_indexed(ann_out)
        except (OSError, subprocess.SubprocessError) as e:
            stderr = getattr(e, "stderr", None)
            detail = f"{e}\n{stderr.strip()}" if stderr else str(e)
            raise WGSExtractError(f"{label} annotation failed: {detail}") from e
    warn_if_nothing_annotated(ann_out, columns, label)


def run_filter(ann_out: str, out_vcf: str, expr: str, label: str) -> None:
    try:
        run_command(
            ["bcftools", "filter", "-i", expr, "-Oz", "-o", out_vcf, ann_out],
            capture_output=True,
        )
        ensure_vcf_indexed(out_vcf)
    except (OSError, subprocess.SubprocessError) as e:
        raise WGSExtractError(f"{label} filtering failed: {e}") from e


def run_min_score_filter(
    ann_out: str,
    outdir: str,
    min_score: float | None,
    info_field: str,
    output_prefix: str,
    label: str,
    log_done_key: str,
) -> None:
    if min_score is None:
        logging.info(LOG_MESSAGES[log_done_key].format(output=ann_out))
        return

    path_out = str(Path(outdir, f"{output_prefix}_gt_{min_score}.vcf.gz"))
    logging.info(
        "Filtering for variants with %s score >= %s to %s", label, min_score, path_out
    )
    run_filter(ann_out, path_out, f"{info_field} >= {min_score}", label)
    logging.info(LOG_MESSAGES[log_done_key].format(output=path_out))
