"""Joint trio genotyping from the proband's and parents' BAM/CRAMs.

Separate VCFs can't say that a parent lacks a variant: a variants-only VCF has
no record there, and a gVCF reference block (often GQ 0, from another provider)
is no evidence. Genotyping all three from their reads at the same sites gives
each parent's depth and allele counts wherever the child has a variant.

Every trio filter needs the child to carry a variant, so the child is called
alone first and the three are genotyped together only at the child's variant
positions (with no alleles imposed, so a parent's other allele at a repeat is
still seen). On a real chr20 that is ~1.5x faster than calling all three at
every position and gives the same de novo and recessive calls; it misses only
weak child hets that the parents' reads would have lifted.
"""

import logging
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
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
# Chromosomes are called in pieces of at most this, so chr1 isn't the last
# worker still running.
CHUNK_BP = 50_000_000
_MPILEUP_ARGS = ["-a", "AD,DP", "-q", "20", "-Q", "20"]

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


def trio_contig_lengths(alignment: str, region: str | None = None) -> dict[str, int]:
    """Name -> length of the chromosomes a trio is called on, in header order:
    1-22 and X (chrY and chrM are each inherited from one parent, so trio
    inheritance doesn't apply), or the contigs --region names (a comma-separated
    list, as bcftools takes)."""
    header = run_command(["samtools", "view", "-H", alignment], capture_output=True)
    wanted = {str(n) for n in range(1, 23)} | {"X"}
    region_contigs = (
        {part.split(":")[0] for part in region.split(",") if part} if region else None
    )
    lengths = {}
    for line in header.stdout.splitlines():
        if not line.startswith("@SQ"):
            continue
        fields = dict(f.split(":", 1) for f in line.split("\t")[1:] if ":" in f)
        name = fields.get("SN", "")
        keep = (
            name in region_contigs
            if region_contigs
            else name.removeprefix("chr") in wanted
        )
        if keep and fields.get("LN", "").isdigit():
            lengths[name] = int(fields["LN"])
    return lengths


def trio_regions(contig_lengths: dict[str, int]) -> list[str]:
    """The chromosomes as regions of at most CHUNK_BP, in order."""
    return [
        f"{name}:{start}-{min(start + CHUNK_BP - 1, length)}"
        for name, length in contig_lengths.items()
        for start in range(1, length + 1, CHUNK_BP)
    ]


def _call_region(
    region: str,
    alignments: list[str],
    ref: str,
    read_groups: str,
    samples: tuple[str, str],
    ploidy_args: list[str],
    out_vcf: str,
) -> str | None:
    """Call the proband alone over one region, then the trio jointly at the
    proband's variant positions; None when the proband has none there."""
    sites = out_vcf.removesuffix(".vcf.gz") + "_sites.tsv"
    with open(sites, "wb") as out:
        run_pipeline(
            [
                ["bcftools", "mpileup", "-r", region, "-f", ref, *_MPILEUP_ARGS]
                + ["-G", read_groups, "-Ou", alignments[0]],
                ["bcftools", "call", "-m", "-v", "--samples-file", samples[0]]
                + ploidy_args
                + ["-Ou"],
                ["bcftools", "query", "-f", "%CHROM\t%POS\n"],
            ],
            stdout=out,
            description=f"Calling the proband over {region}",
        )
    if Path(sites).stat().st_size == 0:
        return None  # mpileup can't take an empty targets file
    run_pipeline(
        [
            ["bcftools", "mpileup", "-r", region, "-T", sites, "-f", ref]
            + [*_MPILEUP_ARGS, "-G", read_groups, "-Ou", *alignments],
            ["bcftools", "call", "-m", "-v", "--samples-file", samples[1]]
            + ploidy_args
            + ["-Oz", "-o", out_vcf],
        ],
        stdout=subprocess.DEVNULL,
        description=f"Joint trio genotyping over {region}",
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
    reads (samples PROBAND, MOTHER, FATHER) wherever the proband has a variant,
    one region per worker."""
    regions = [region] if region else trio_regions(trio_contig_lengths(alignments[0]))
    if not regions:
        raise WGSExtractError(f"No chromosomes 1-22 or X found in {alignments[0]}.")
    workdir = str(Path(out_vcf).parent)
    with tempfile.TemporaryDirectory(dir=workdir, prefix=".wgse_trio_") as work:
        # Name samples by file: WGS Extract v4 wrote SM:WGSE in every alignment,
        # and bcftools would merge same-named samples into one.
        read_groups = str(Path(work, "read_groups.txt"))
        Path(read_groups).write_text(
            "".join(
                f"*\t{_read_group_field(a)}\t{s}\n"
                for a, s in zip(alignments, TRIO_SAMPLES, strict=True)
            ),
            newline="\n",
        )
        sex_lines = [f"{s}\t{x}\n" for s, x in zip(TRIO_SAMPLES, sexes, strict=True)]
        samples = (str(Path(work, "proband.txt")), str(Path(work, "trio.txt")))
        Path(samples[0]).write_text(sex_lines[0], newline="\n")
        Path(samples[1]).write_text("".join(sex_lines), newline="\n")
        logging.info(
            f"Genotyping the trio from their alignments over {len(regions)} "
            f"region(s) with {threads} worker(s); a 30x whole genome takes about an hour with 8."
        )
        chunks = [str(Path(work, f"{i:03d}.vcf.gz")) for i in range(len(regions))]
        # Threads, not processes: each worker only waits on bcftools, and threads
        # keep the selected --config (and its tool paths) that new processes
        # would lose on Windows and macOS.
        with ThreadPoolExecutor(max_workers=max(1, threads)) as pool:
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
            called = [c for c in (f.result() for f in futures) if c]
        if not called:
            raise WGSExtractError(
                f"The proband has no variants in {region or 'chromosomes 1-22, X'} "
                "to genotype the parents at."
            )
        remove_index(out_vcf)
        run_command(["bcftools", "concat", "-Oz", "-o", out_vcf, *called])
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


def _read_group_field(path: str) -> str:
    """A path for bcftools' --read-groups file, which takes a backslash as an
    escape and splits fields at whitespace (Windows paths, names with spaces)."""
    return re.sub(r"([\\\s])", r"\\\1", path)


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
    if not isolated:  # bcftools can't read an empty targets file
        run_command(["bcftools", "view", "-h", "-Oz", "-o", out_vcf, denovo_vcf])
        ensure_vcf_indexed(out_vcf)
        return 0, len(sites)
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
