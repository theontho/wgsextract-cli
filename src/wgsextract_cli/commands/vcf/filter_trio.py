import argparse
import logging
import subprocess
import tempfile
from pathlib import Path

from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.paths import absolute_path
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
from .trio_alignments import (
    DEFAULT_MIN_DEPTH,
    is_alignment,
    joint_filter,
    joint_trio_vcf,
)


def write_gaps_bed(ref_path: str, bed_path: str) -> bool:
    """
    Convert the reference's Count-Ns ``_nbin.csv`` sidecar to a BED of N runs.

    The sidecar is tab-separated with ``#`` comment/header lines and columns
    SN, BinID, Start (0-based), Size; numbers carry thousands separators.
    Returns False when the sidecar is missing.
    """
    nbin_file = n_count_paths(ref_path)[1]
    if not Path(nbin_file).exists():
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

    logging.debug(f"Input file: {str(absolute_path(input_file))}")

    outdir = args.outdir or str(absolute_path(input_file).parent)
    Path(outdir).mkdir(parents=True, exist_ok=True)
    logging.debug(f"Output directory: {str(absolute_path(outdir))}")
    out_vcf = str(Path(outdir, "filtered.vcf.gz"))

    # Resolve reference if needed for gap filtering or gene resolution
    md5_sig = calculate_bam_md5(input_file, None)
    lib = ReferenceLibrary(args.ref, md5_sig, input_path=input_file)
    logging.debug(f"Resolved reference: {lib.fasta}")

    # Gene-based region resolution
    region = args.region
    if args.gene:
        from wgsextract_cli.core.gene_map import gene_coords

        resolved_region = gene_coords(
            lib.root if lib.root else str(absolute_path(input_file).parent),
            args.gene,
            lib.build or "hg38",
        )
        logging.info(f"Resolved gene {args.gene} to {resolved_region}")
        region = resolved_region

    region_args = ["-r", region] if region else []
    expr_args = ["-i", args.expr] if args.expr else []

    input_vcf = ensure_vcf_prepared(input_file)
    logging.info(LOG_MESSAGES["vcf_filtering"].format(input=input_vcf, output=out_vcf))
    with tempfile.TemporaryDirectory(dir=outdir, prefix=".wgse_filter_") as work:
        exclude_args = []
        if getattr(args, "exclude_near_gaps", False):
            gaps_bed = str(Path(work, "gaps.bed"))
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
            "Proband (--proband or --vcf-input), --mother, and --father (VCFs, or "
            "BAM/CRAMs) are all required."
        )
    verify_paths_exist({"--proband": proband, "--mother": mother, "--father": father})

    outdir = args.outdir or str(absolute_path(proband).parent)
    Path(outdir).mkdir(parents=True, exist_ok=True)
    modes = [args.mode] if args.mode != "all" else list(_TRIO_FILTERS)

    alignments = [is_alignment(p) for p in (proband, mother, father)]
    if all(alignments):
        _trio_from_alignments(args, [proband, mother, father], outdir, modes)
        return
    if any(alignments):
        raise WGSExtractError(
            "Give all three as VCFs or all three as BAM/CRAMs; a trio can't mix them."
        )

    p_vcf = ensure_vcf_prepared(proband)
    m_vcf = ensure_vcf_prepared(mother)
    f_vcf = ensure_vcf_prepared(father)
    p_idx, m_idx, f_idx = trio_sample_indices(p_vcf, m_vcf, f_vcf)
    region_args = ["-r", args.region] if getattr(args, "region", None) else []

    with tempfile.TemporaryDirectory(dir=outdir, prefix=".wgse_trio_") as work:
        # Match parents' contig naming (chr1 vs 1) to the proband before merging.
        target_chroms = vcf_index_chromosomes(p_vcf)
        m_norm = normalize_vcf_chromosomes(m_vcf, target_chroms, work)
        f_norm = normalize_vcf_chromosomes(f_vcf, target_chroms, work)

        merged_vcf = str(Path(work, "merged_trio.vcf.gz"))
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
            out_vcf = str(Path(outdir, f"trio_{mode}.vcf.gz"))
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
            if mode == "denovo":
                _warn_if_parental_refs_unconfident(out_vcf, m_idx, f_idx)


def _warn_if_parental_refs_unconfident(out_vcf: str, m_idx: int, f_idx: int) -> None:
    """De novo calls from VCFs rest on the parents' 0/0 calls; from gVCF reference
    blocks (often GQ 0, from another pipeline) those say nothing, and the de novo
    list fills with inherited variants."""
    try:
        total = int(
            run_command(
                ["bcftools", "index", "--nrecords", out_vcf], capture_output=True
            ).stdout.strip()
        )
        weak = run_command(
            ["bcftools", "view", "-H", "-i", f"GQ[{m_idx}]<20 || GQ[{f_idx}]<20"]
            + [out_vcf],
            capture_output=True,
        ).stdout.count("\n")
    except (OSError, subprocess.SubprocessError, ValueError):
        return  # no GQ to judge by
    if total >= 100 and weak > total // 2:
        logging.warning(
            f"{weak:,} of these {total:,} de novo calls rest on a parent's reference "
            "call with GQ under 20, which doesn't show the parent lacks the variant. "
            "For real de novo calls, give the three BAM/CRAMs instead (--proband, "
            "--mother, --father): they're genotyped jointly from the reads."
        )


def _trio_from_alignments(
    args: argparse.Namespace, alignments: list[str], outdir: str, modes: list[str]
) -> None:
    from wgsextract_cli.core.builds import (
        is_hg37_build,
        is_hg38_build,
        ploidy_for_build,
    )
    from wgsextract_cli.core.utils import get_resource_defaults

    from ..info.metrics import sample_sex

    verify_dependencies(["bcftools", "tabix", "samtools"])
    proband = alignments[0]
    lib = ReferenceLibrary(
        args.ref, calculate_bam_md5(proband, None), input_path=proband
    )
    if not lib.fasta:
        raise WGSExtractError(
            "Reference genome required to genotype the trio's alignments; pass --ref."
        )
    if lib.ploidy_file:
        ploidy_args = ["--ploidy-file", lib.ploidy_file]
    elif lib.build and (is_hg37_build(lib.build) or is_hg38_build(lib.build)):
        ploidy_args = ["--ploidy", ploidy_for_build(lib.build)]
    else:
        ploidy_args = []
    # The parents' sexes are their roles; the proband's comes from chrX/chrY.
    proband_sex = "M" if sample_sex(proband, outdir) == "Male" else "F"
    threads = int(get_resource_defaults(getattr(args, "threads", None), None)[0])
    joint = joint_trio_vcf(
        alignments,
        [proband_sex, "F", "M"],
        lib.fasta,
        ploidy_args,
        str(Path(outdir, "trio_joint.vcf.gz")),
        getattr(args, "region", None),
        threads,
    )
    logging.info(f"Joint trio genotypes: {joint}")
    min_depth = getattr(args, "min_depth", None) or DEFAULT_MIN_DEPTH
    for mode in modes:
        out_vcf = str(Path(outdir, f"trio_{mode}.vcf.gz"))
        logging.info(
            LOG_MESSAGES["vcf_trio_analysis"].format(mode=mode, output=out_vcf)
        )
        try:
            run_command(
                ["bcftools", "view", "-i", joint_filter(mode, min_depth)]
                + ["-Oz", "-o", out_vcf, joint],
                capture_output=True,
            )
            ensure_vcf_indexed(out_vcf)
        except (OSError, subprocess.SubprocessError) as e:
            raise WGSExtractError(f"VCF trio filtering failed for {mode}: {e}") from e
        logging.info(LOG_MESSAGES["vcf_trio_complete"].format(output=out_vcf))
        _summarize_trio_output(mode, out_vcf)
