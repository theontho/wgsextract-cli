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

from wgsextract_cli.core.utils import WGSExtractError, run_command, run_pipeline
from wgsextract_cli.core.variant_files import ensure_vcf_indexed

ALIGNMENT_SUFFIXES = (".bam", ".cram")
TRIO_SAMPLES = ("PROBAND", "MOTHER", "FATHER")
# Genotyping evidence each site needs (overridable with --min-depth).
DEFAULT_MIN_DEPTH = 15
MIN_ALT_READS = 8
MIN_ALT_FRACTION = 0.3

# Indices 0, 1, 2 are the proband, mother and father in the joint VCF.
_DEPTH = "FMT/DP[0]>={d} && FMT/DP[1]>={d} && FMT/DP[2]>={d}"
JOINT_TRIO_FILTERS = {
    # Child het with real support; both parents ref with no alt read at all.
    "denovo": (
        'GT[0]="het" && FMT/AD[0:1]>={a} && FMT/AD[0:1]>={f}*FMT/DP[0] '
        '&& GT[1]="ref" && GT[2]="ref" && FMT/AD[1:1]=0 && FMT/AD[2:1]=0 && ' + _DEPTH
    ),
    "recessive": 'GT[0]="AA" && GT[1]="het" && GT[2]="het" && ' + _DEPTH,
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
    """The proband's chromosomes 1-22, X, Y and M, in header order."""
    header = run_command(
        ["samtools", "view", "-H", "-T", ref, alignment], capture_output=True
    ).stdout
    primary = {str(n) for n in range(1, 23)} | {"X", "Y", "M", "MT"}
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
        raise WGSExtractError(f"No chromosomes 1-22/X/Y/M found in {alignments[0]}.")
    workdir = str(Path(out_vcf).parent)
    with tempfile.TemporaryDirectory(dir=workdir, prefix=".wgse_trio_") as work:
        # Name samples by file: WGS Extract v4 wrote SM:WGSE in every alignment,
        # and bcftools would merge same-named samples into one.
        read_groups = str(Path(work, "read_groups.txt"))
        Path(read_groups).write_text(
            "".join(
                f"*\t{a}\t{s}\n" for a, s in zip(alignments, TRIO_SAMPLES, strict=True)
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
        run_command(["bcftools", "concat", "-Oz", "-o", out_vcf, *chunks])
    ensure_vcf_indexed(out_vcf)
    return out_vcf
