import argparse
import logging
import os
import subprocess
import tempfile

from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.reference_assets import n_count_paths
from wgsextract_cli.core.reference_resolver import ReferenceLibrary
from wgsextract_cli.core.utils import (
    WGSExtractError,
    run_command,
)
from wgsextract_cli.core.variant_files import (
    calculate_bam_md5,
    ensure_vcf_indexed,
    ensure_vcf_prepared,
    get_vcf_samples,
    normalize_vcf_chromosomes,
    vcf_index_chromosomes,
    verify_paths_exist,
)

from .basic import require_vcf_input


def write_gaps_bed(ref_path: str, bed_path: str) -> bool:
    """
    Convert the reference's Count-Ns ``_nbin.csv`` sidecar to a BED of N runs.

    The sidecar is tab-separated with ``#`` comment/header lines and columns
    SN, BinID, Start (0-based), Size; numbers carry thousands separators.
    Returns False when the sidecar is missing.
    """
    nbin_file = n_count_paths(ref_path)[1]
    if not os.path.exists(nbin_file):
        return False
    with open(nbin_file, encoding="utf-8") as f_in, open(bed_path, "w") as f_out:
        for line in f_in:
            parts = line.rstrip("\n").split("\t")
            if line.startswith("#") or len(parts) < 4:
                continue
            try:
                start = int(parts[2].replace(",", ""))
                size = int(parts[3].replace(",", ""))
            except ValueError:
                continue
            f_out.write(f"{parts[0]}\t{start}\t{start + size}\n")
    return True


def cmd_filter(args: argparse.Namespace) -> None:
    input_file = require_vcf_input(args)
    verify_paths_exist({"--input": input_file})

    verify_dependencies(["bcftools", "tabix"])
    log_dependency_info(["bcftools", "tabix"])

    logging.debug(f"Input file: {os.path.abspath(input_file)}")

    outdir = args.outdir or os.path.dirname(os.path.abspath(input_file))
    os.makedirs(outdir, exist_ok=True)
    logging.debug(f"Output directory: {os.path.abspath(outdir)}")
    out_vcf = os.path.join(outdir, "filtered.vcf.gz")

    # Resolve reference if needed for gap filtering or gene resolution
    md5_sig = calculate_bam_md5(input_file, None)
    lib = ReferenceLibrary(args.ref, md5_sig, input_path=input_file)
    logging.debug(f"Resolved reference: {lib.fasta}")

    # Gene-based region resolution
    region = args.region
    if args.gene:
        from wgsextract_cli.core.gene_map import GeneMap

        gm = GeneMap(
            lib.root if lib.root else os.path.dirname(os.path.abspath(input_file))
        )
        resolved_region = gm.get_coords(args.gene, lib.build or "hg38")

        if not resolved_region:
            raise WGSExtractError(f"Could not resolve gene name: {args.gene}")
        logging.info(f"Resolved gene {args.gene} to {resolved_region}")
        region = resolved_region

    region_args = ["-r", region] if region else []
    expr_args = ["-i", args.expr] if args.expr else []

    input_vcf = ensure_vcf_prepared(input_file)
    logging.info(LOG_MESSAGES["vcf_filtering"].format(input=input_vcf, output=out_vcf))
    with tempfile.TemporaryDirectory(dir=outdir, prefix=".wgse_filter_") as work:
        exclude_args = []
        if getattr(args, "exclude_near_gaps", False):
            gaps_bed = os.path.join(work, "gaps.bed")
            if not lib.fasta:
                logging.warning(
                    "Gap exclusion requested but reference genome not resolved."
                )
            elif write_gaps_bed(lib.fasta, gaps_bed):
                logging.info(f"Excluding variants in reference N runs ({lib.fasta})")
                exclude_args = ["-T", f"^{gaps_bed}"]
            else:
                logging.warning(
                    "Gap exclusion requested but Count Ns output (_nbin.csv) not found."
                )
        try:
            run_command(
                ["bcftools", "view"]
                + region_args
                + expr_args
                + exclude_args
                + ["-Oz", "-o", out_vcf, input_vcf]
            )
            ensure_vcf_indexed(out_vcf)
        except (OSError, subprocess.SubprocessError, RuntimeError) as e:
            raise WGSExtractError(f"VCF filtering failed: {e}") from e


_TRIO_FILTERS = {
    # Child het, both parents ref or missing.
    "denovo": '{p}="het" && ({m}="ref" || {m}=".") && ({f}="ref" || {f}=".")',
    # Child hom-alt, both parents het.
    "recessive": '{p}="hom" && {m}="het" && {f}="het"',
    # Simplified: child het, exactly one parent het, the other ref/missing.
    "comphet": (
        '{p}="het" && (({m}="het" && ({f}="ref" || {f}=".")) '
        '|| (({m}="ref" || {m}=".") && {f}="het"))'
    ),
}


def trio_sample_indices(p_vcf: str, m_vcf: str, f_vcf: str) -> tuple[int, int, int]:
    """
    Column of each role's (first) sample in `bcftools merge p m f` output.

    merge concatenates samples in input-file order, and --force-samples renames
    duplicates (e.g. "2:sample"), so positions are reliable while names are not.
    """
    counts = []
    for path in (p_vcf, m_vcf, f_vcf):
        n = len(get_vcf_samples(path))
        if n == 0:
            raise WGSExtractError(f"Trio VCF has no samples: {path}")
        counts.append(n)
    return 0, counts[0], counts[0] + counts[1]


def _summarize_trio_output(mode: str, out_vcf: str) -> None:
    try:
        total = run_command(["bcftools", "view", "-H", out_vcf], capture_output=True)
        summary = (
            f"{mode.upper()} results: {total.stdout.count(chr(10))} total variants"
        )
        header = run_command(["bcftools", "view", "-h", out_vcf], capture_output=True)
        if "ID=CSQ," in header.stdout:
            high = run_command(
                ["bcftools", "view", "-H", "-i", 'CSQ~"HIGH"', out_vcf],
                capture_output=True,
            )
            summary += f", {high.stdout.count(chr(10))} HIGH impact"
        logging.info(summary)
    except (OSError, subprocess.SubprocessError) as e:
        logging.debug("Could not summarize trio filter output %s: %s", out_vcf, e)


def cmd_trio(args: argparse.Namespace) -> None:
    verify_dependencies(["bcftools", "tabix"])

    proband = args.proband or args.vcf_input
    mother = args.mother
    father = args.father
    if not proband or not mother or not father:
        raise WGSExtractError(
            "Proband (--proband or --vcf-input), --mother, and --father VCFs are "
            "all required."
        )
    verify_paths_exist({"--proband": proband, "--mother": mother, "--father": father})

    outdir = args.outdir or os.path.dirname(os.path.abspath(proband))
    os.makedirs(outdir, exist_ok=True)

    p_vcf = ensure_vcf_prepared(proband)
    m_vcf = ensure_vcf_prepared(mother)
    f_vcf = ensure_vcf_prepared(father)
    p_idx, m_idx, f_idx = trio_sample_indices(p_vcf, m_vcf, f_vcf)
    region_args = ["-r", args.region] if getattr(args, "region", None) else []
    modes = [args.mode] if args.mode != "all" else list(_TRIO_FILTERS)

    with tempfile.TemporaryDirectory(dir=outdir, prefix=".wgse_trio_") as work:
        # Match parents' contig naming (chr1 vs 1) to the proband before merging.
        target_chroms = vcf_index_chromosomes(p_vcf)
        m_norm = normalize_vcf_chromosomes(m_vcf, target_chroms, work)
        f_norm = normalize_vcf_chromosomes(f_vcf, target_chroms, work)

        merged_vcf = os.path.join(work, "merged_trio.vcf.gz")
        try:
            run_command(
                ["bcftools", "merge", "--force-samples", "-Oz", "-o", merged_vcf]
                + region_args
                + [p_vcf, m_norm, f_norm],
                capture_output=True,
            )
            ensure_vcf_indexed(merged_vcf)
        except (OSError, subprocess.SubprocessError) as e:
            raise WGSExtractError(f"VCF trio merge failed: {e}") from e

        for mode in modes:
            out_vcf = os.path.join(outdir, f"trio_{mode}.vcf.gz")
            logging.info(
                LOG_MESSAGES["vcf_trio_analysis"].format(mode=mode, output=out_vcf)
            )
            filter_expr = _TRIO_FILTERS[mode].format(
                p=f"GT[{p_idx}]", m=f"GT[{m_idx}]", f=f"GT[{f_idx}]"
            )
            try:
                run_command(
                    ["bcftools", "view", "-i", filter_expr, "-Oz", "-o", out_vcf]
                    + [merged_vcf],
                    capture_output=True,
                )
                ensure_vcf_indexed(out_vcf)
            except (OSError, subprocess.SubprocessError) as e:
                raise WGSExtractError(
                    f"VCF trio filtering failed for {mode}: {e}"
                ) from e
            logging.info(LOG_MESSAGES["vcf_trio_complete"].format(output=out_vcf))
            _summarize_trio_output(mode, out_vcf)
