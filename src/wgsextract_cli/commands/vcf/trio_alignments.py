"""Joint trio genotyping from the proband's and parents' BAM/CRAMs.

Separate VCFs can't say that a parent lacks a variant: a variants-only VCF has
no record there, and a gVCF reference block (often GQ 0, from another provider)
is no evidence. Genotyping all three from their reads at the same sites gives
each parent's depth and allele counts wherever the child has a variant.
"""

import logging
import subprocess
import tempfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from wgsextract_cli.core.paths import append_suffix
from wgsextract_cli.core.utils import WGSExtractError, run_command, run_pipeline
from wgsextract_cli.core.variant_files import ensure_vcf_indexed

ALIGNMENT_SUFFIXES = (".bam", ".cram")
TRIO_SAMPLES = ("PROBAND", "MOTHER", "FATHER")
# Genotyping evidence each site needs (overridable with --min-depth).
DEFAULT_MIN_DEPTH = 15
MIN_ALT_READS = 8
MIN_ALT_FRACTION = 0.3
# True de novo SNVs are ~1 per 50 Mb; candidates this close to another are
# almost always misaligned reads in a repeat (or, rarely, one multi-base event).
CLUSTER_DISTANCE = 1000

# Indices 0, 1, 2 are the proband, mother and father in the joint VCF, whose
# multiallelic records are split first so GT and AD[:1] name the same allele.
# Genotypes may be haploid: a male's chrX outside the PARs.
_DEPTH = "FMT/DP[0]>={d} && FMT/DP[1]>={d} && FMT/DP[2]>={d}"
JOINT_TRIO_FILTERS = {
    # Child het (or hemizygous alt) with real support; both parents ref with no
    # alt read at all.
    "denovo": (
        '(GT[0]="het" || GT[0]="A") && FMT/AD[0:1]>={a} && '
        'FMT/AD[0:1]>={f}*FMT/DP[0] && GT[1]="ref" && GT[2]="ref" && '
        "FMT/AD[1:1]=0 && FMT/AD[2:1]=0 && " + _DEPTH
    ),
    # Autosomal recessive, or X-linked in a son (from a carrier mother).
    "recessive": (
        '((GT[0]="AA" && GT[1]="het" && GT[2]="het") || '
        '(GT[0]="A" && GT[1]="het" && GT[2]="ref")) && ' + _DEPTH
    ),
    "comphet": (
        'GT[0]="het" && ((GT[1]="het" && GT[2]="ref") || (GT[1]="ref" && '
        'GT[2]="het")) && ' + _DEPTH
    ),
}


def is_alignment(path: str) -> bool:
    return path.lower().endswith(ALIGNMENT_SUFFIXES)


def joint_filter(mode: str, min_depth: int) -> str:
    return JOINT_TRIO_FILTERS[mode].format(
        d=min_depth, a=MIN_ALT_READS, f=MIN_ALT_FRACTION
    )


def _primary_contigs(alignment: str, ref: str) -> list[str]:
    """The proband's chromosomes 1-22 and X, in header order. chrY and chrM are
    each inherited from one parent, so trio inheritance doesn't apply to them."""
    header = run_command(
        ["samtools", "view", "-H", "-T", ref, alignment], capture_output=True
    ).stdout
    primary = {str(n) for n in range(1, 23)} | {"X"}
    contigs = []
    for line in header.splitlines():
        if line.startswith("@SQ"):
            name = next(f[3:] for f in line.split("\t") if f.startswith("SN:"))
            if name.removeprefix("chr") in primary:
                contigs.append(name)
    return contigs


def _call_region(
    region: str,
    alignments: list[str],
    ref: str,
    read_groups: str,
    samples: str,
    ploidy_args: list[str],
    out_vcf: str,
) -> str:
    """Joint-call one region of the three alignments. Module level for pickling."""
    run_pipeline(
        [
            ["bcftools", "mpileup", "-r", region, "-f", ref, "-a", "AD,DP"]
            + ["-q", "20", "-Q", "20", "-G", read_groups, "-Ou", *alignments],
            ["bcftools", "call", "-m", "-v", "--samples-file", samples]
            + ploidy_args
            + ["-Oz", "-o", out_vcf],
        ],
        stdout=subprocess.DEVNULL,
        description=f"Joint trio calling of {region}",
    )
    return out_vcf


def joint_trio_vcf(
    alignments: list[str],
    sexes: list[str],
    ref: str,
    ploidy_args: list[str],
    out_vcf: str,
    region: str | None,
    threads: int,
) -> str:
    """Write out_vcf: the proband, mother and father jointly genotyped from their
    reads (samples PROBAND, MOTHER, FATHER), one chromosome per worker."""
    regions = [region] if region else _primary_contigs(alignments[0], ref)
    if not regions:
        raise WGSExtractError(f"No chromosomes 1-22 or X found in {alignments[0]}.")
    workdir = str(Path(out_vcf).parent)
    with tempfile.TemporaryDirectory(dir=workdir, prefix=".wgse_trio_") as work:
        # Name samples by file: WGS Extract v4 wrote SM:WGSE in every alignment,
        # and bcftools would merge same-named samples into one.
        read_groups = str(Path(work, "read_groups.txt"))
        Path(read_groups).write_text(
            "".join(
                f"*\t{_tool_side_path(a)}\t{s}\n"
                for a, s in zip(alignments, TRIO_SAMPLES, strict=True)
            )
        )
        samples = str(Path(work, "samples.txt"))
        Path(samples).write_text(
            "".join(f"{s}\t{x}\n" for s, x in zip(TRIO_SAMPLES, sexes, strict=True))
        )
        logging.info(
            f"Jointly genotyping the trio from their alignments over {len(regions)} "
            f"region(s) with {threads} worker(s); a whole genome takes hours."
        )
        chunks = [str(Path(work, f"{i:03d}.vcf.gz")) for i in range(len(regions))]
        with ProcessPoolExecutor(max_workers=max(1, threads)) as pool:
            futures = [
                pool.submit(
                    _call_region,
                    r,
                    alignments,
                    ref,
                    read_groups,
                    samples,
                    ploidy_args,
                    chunk,
                )
                for r, chunk in zip(regions, chunks, strict=True)
            ]
            for future in futures:
                future.result()
        remove_index(out_vcf)
        run_command(["bcftools", "concat", "-Oz", "-o", out_vcf, *chunks])
    ensure_vcf_indexed(out_vcf)
    return out_vcf


def remove_index(vcf: str) -> None:
    """Drop a VCF's old index before rewriting it: ensure_vcf_indexed keeps any
    existing index, which would then describe the previous file."""
    for suffix in (".tbi", ".csi"):
        append_suffix(vcf, suffix).unlink(missing_ok=True)


def split_multiallelic(vcf: str, out_vcf: str) -> str:
    """One record per ALT, so genotype classes and AD[:1] refer to one allele."""
    remove_index(out_vcf)
    run_command(["bcftools", "norm", "-m", "-any", "-Oz", "-o", out_vcf, vcf])
    ensure_vcf_indexed(out_vcf)
    return out_vcf


def _tool_side_path(path: str) -> str:
    """A path as bcftools sees it: the read-group file's keys must match its
    arguments, which the WSL backend translates."""
    from wgsextract_cli.core import runtime
    from wgsextract_cli.core.dependencies import get_tool_path
    from wgsextract_cli.core.runtime_paths import translate_wsl_arg

    if runtime.is_wsl_tool_command(get_tool_path("bcftools") or ""):
        return translate_wsl_arg(path)
    return path


def write_isolated_denovo(denovo_vcf: str, out_vcf: str) -> tuple[int, int]:
    """Write the de novo candidates with no other within CLUSTER_DISTANCE bp;
    return (isolated, total)."""
    rows = run_command(
        ["bcftools", "query", "-f", "%CHROM\t%POS\n", denovo_vcf], capture_output=True
    ).stdout.split("\n")
    sites = [(c, int(p)) for c, p in (r.split("\t") for r in rows if r)]
    remove_index(out_vcf)
    isolated = [
        site
        for i, site in enumerate(sites)
        if all(
            not (other[0] == site[0] and abs(other[1] - site[1]) <= CLUSTER_DISTANCE)
            for other in sites[max(0, i - 1) : i] + sites[i + 1 : i + 2]
        )
    ]
    with tempfile.NamedTemporaryFile(
        "w", suffix=".tsv", dir=str(Path(out_vcf).parent), delete=False
    ) as targets:
        targets.writelines(f"{c}\t{p}\n" for c, p in isolated)
    try:
        run_command(
            ["bcftools", "view", "-T", targets.name, "-Oz", "-o", out_vcf, denovo_vcf],
            capture_output=True,
        )
    finally:
        Path(targets.name).unlink(missing_ok=True)
    ensure_vcf_indexed(out_vcf)
    return len(isolated), len(sites)
