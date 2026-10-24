"""`typing cyp2d6`: CYP2D6 star alleles with Cyrius.

Cyrius (Illumina; PolyForm Strict license, noncommercial use only) calls CYP2D6
diplotypes, including copy-number and CYP2D6/CYP2D7 hybrid alleles, from the
depth and reads at the CYP2D6 locus of a 30x WGS BAM/CRAM.

GRCh38 references with ALT contigs (hs38DH, GRCh38 full analysis set) carry
copies of CYP2D6 on chr22 ALTs: an aligner that isn't ALT-aware (bwa-mem2
without the .alt file) splits the locus's reads between chr22 and the ALTs,
leaving both copies at MAPQ 0 and about half depth, and Cyrius no-calls. The
locus and all chr22 ALT reads are then realigned to primary chr22 only, and
Cyrius runs on those plus its normalization regions as they were.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from wgsextract_cli.core.dependency_checks import verify_dependencies
from wgsextract_cli.core.paths import absolute_path
from wgsextract_cli.core.utils import WGSExtractError, run_command, run_pipeline

from .common import Alignment, env_tool, open_alignment, write_bed, write_outside_calls

LICENSE_NOTE = " (Cyrius's PolyForm Strict license allows noncommercial use only)"
# CYP2D6 through CYP2D7 (GRCh38 chr22:42123192-42145745) with 40 kb either side,
# so reads pair up within it. No Cyrius normalization region is within 0.1 Mb.
RESCUE_WINDOW = (42_080_000, 42_180_000)
# Filters Cyrius reports for a genotype; LowQ_high_CN is a call at 4+ copies.
CALLED_FILTERS = ("PASS", "LowQ_high_CN")


@dataclass(frozen=True)
class CyriusCall:
    genotype: str | None
    filter: str | None
    total_cn: int | None
    total_cn_raw: float | None
    median_depth: float | None

    @property
    def outside_call(self) -> str | None:
        """The genotype to give PharmCAT: only one Cyrius stands behind."""
        if self.genotype and self.filter in CALLED_FILTERS:
            return pharmcat_copy_numbers(self.genotype)
        return None


# PharmCAT (after CPIC) bins 3 or more copies of these alleles as x≥3; others,
# such as *41x3, keep their count.
_BINNED_AT_3 = ("*1", "*2", "*4")


def pharmcat_copy_numbers(genotype: str) -> str:
    """Cyrius's *1/*2x3 as PharmCAT names it: *1/*2x≥3."""

    def name(match: re.Match[str]) -> str:
        allele, copies = match.group(1), int(match.group(2))
        return f"{allele}x≥3" if allele in _BINNED_AT_3 and copies >= 3 else match[0]

    return re.sub(r"(\*\d+)x(\d+)", name, genotype)


def parse_cyrius(json_path: str) -> CyriusCall:
    with open(json_path) as f:
        result = next(iter(json.load(f).values()))
    genotype = result.get("Genotype")
    return CyriusCall(
        genotype=genotype if genotype and genotype != "None" else None,
        filter=result.get("Filter"),
        total_cn=result.get("Total_CN"),
        total_cn_raw=result.get("Total_CN_raw"),
        median_depth=result.get("Median_depth"),
    )


def cyrius_genome(aln: Alignment) -> str:
    """Cyrius's -g: 38, or 19/37 for GRCh37 with/without "chr" names."""
    if aln.build == "hg38":
        if not aln.chr_prefix:
            raise WGSExtractError(
                "Cyrius needs GRCh38 contigs named chr1, chr2, ... (its region "
                "files use them); this input names them 1, 2, ..."
            )
        return "38"
    return "19" if aln.chr_prefix else "37"


def _region_bed(genome: str) -> str:
    python = env_tool("cyrius_python", "cyrius", LICENSE_NOTE)
    locate = (
        "import sys, cyrius; from pathlib import Path; "
        "print(Path(cyrius.__file__).parent / 'data' / "
        "f'CYP2D6_region_{sys.argv[1]}.bed')"
    )
    result = run_command([*python, "-c", locate, genome], capture_output=True)
    path = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    if not Path(path).is_file():
        raise WGSExtractError(f"Cyrius's region file {path or '(none)'} is missing.")
    return path


def rescue_alt_reads(
    aln: Alignment, region_bed: str, alts: list[str], work: str
) -> str:
    """A BAM of Cyrius's normalization regions as aligned plus the CYP2D6 locus
    realigned, with its chr22 ALT reads, to primary chr22."""
    reference = aln.require_reference()
    verify_dependencies(["samtools", "minimap2"])
    chrom = aln.contig("22")
    start, end = RESCUE_WINDOW
    threads = aln.threads

    with open(region_bed) as f:
        norm = [
            (fields[0], int(fields[1]), int(fields[2]))
            for fields in (line.split("\t") for line in f)
            if len(fields) > 4 and fields[4] == "norm"
        ]
    norm_bam = str(Path(work, "norm.bam"))
    run_command(
        ["samtools", "view", "-@", threads, "-b", "-M", "-L",
         write_bed(str(Path(work, "norm.bed")), norm), *aln.ref_opt,
         "-o", norm_bam, aln.path]
    )  # fmt: skip

    chrom_fasta = str(Path(work, f"{chrom}.fa"))
    run_command(["samtools", "faidx", "-o", chrom_fasta, reference, chrom])
    window_bed = write_bed(str(Path(work, "window.bed")), [(chrom, start, end)])
    locus_bam = str(Path(work, "locus.bam"))
    logging.info(
        f"Realigning the CYP2D6 locus and {len(alts)} {chrom} ALT contigs' reads "
        f"to primary {chrom} (the input has ALT contigs)..."
    )
    run_pipeline(
        [
            [
                "samtools",
                "view",
                "-u",
                "-F",
                "0x900",
                *aln.ref_opt,
                aln.path,
                f"{chrom}:{start + 1}-{end}",
                *alts,
            ],
            [
                "samtools",
                "collate",
                "-u",
                "-O",
                "-@",
                threads,
                "-",
                str(Path(work, "collate")),
            ],
            # Interleaved, so minimap2 pairs mates and maps orphans single-ended.
            ["samtools", "fastq", "-n", "-"],
            ["minimap2", "-t", threads, "-ax", "sr", chrom_fasta, "-"],
            ["samtools", "fixmate", "-u", "-m", "-", "-"],
            [
                "samtools",
                "sort",
                "-u",
                "-@",
                threads,
                "-T",
                str(Path(work, "sort")),
                "-",
            ],
            ["samtools", "markdup", "-u", "-", "-"],
            # Reads realigned outside the locus (other chr22 ALT genes) are dropped.
            ["samtools", "view", "-b", "-L", window_bed, "-o", locus_bam, "-"],
        ],  # fmt: skip
        description="CYP2D6 ALT rescue",
    )
    mini_bam = str(Path(work, "cyp2d6.bam"))
    run_command(["samtools", "merge", "-f", "-@", threads, "-o", mini_bam,
                 norm_bam, locus_bam])  # fmt: skip
    run_command(["samtools", "index", mini_bam])
    return mini_bam


def cmd_cyp2d6(args: argparse.Namespace) -> None:
    aln = open_alignment(args)
    genome = cyrius_genome(aln)
    cyrius = env_tool("cyrius", "cyrius", LICENSE_NOTE)
    region_bed = _region_bed(genome)
    alts = aln.alt_contigs("22")
    if alts:
        aln.require_reference()

    prefix = f"{aln.stem}.cyrius"
    with tempfile.TemporaryDirectory(dir=aln.outdir, prefix=".wgse_cyp2d6_") as work:
        reads = rescue_alt_reads(aln, region_bed, alts, work) if alts else aln.path
        manifest = str(Path(work, "manifest.txt"))
        with open(manifest, "w") as f:
            f.write(str(absolute_path(reads)) + "\n")
        command = [*cyrius, "-m", manifest, "-g", genome, "-o", work, "-p", prefix]
        if reads.lower().endswith(".cram"):
            command += ["-r", aln.require_reference()]
        logging.info("Calling CYP2D6 with Cyrius...")
        try:
            run_command(command)
        except subprocess.CalledProcessError as e:
            raise WGSExtractError(f"Cyrius failed (exit {e.returncode}).") from e
        for suffix in (".tsv", ".json"):
            if not Path(work, prefix + suffix).is_file():
                raise WGSExtractError(
                    f"Cyrius finished without writing {prefix}{suffix}."
                )
            shutil.move(
                str(Path(work, prefix + suffix)),
                str(Path(aln.outdir, prefix + suffix)),
            )

    call = parse_cyrius(str(Path(aln.outdir, f"{prefix}.json")))
    depth = f"{call.median_depth}x" if call.median_depth is not None else "unknown"
    logging.info(
        f"CYP2D6: {call.genotype or 'no call'} (filter {call.filter}, "
        f"{call.total_cn if call.total_cn is not None else '?'} copies; raw "
        f"{call.total_cn_raw}; median depth {depth})"
    )
    if call.genotype and not call.outside_call:
        logging.warning(
            f"Cyrius couldn't settle on one genotype ({call.filter}); no PharmCAT "
            "outside call was written."
        )
    elif call.filter == "LowQ_high_CN":
        logging.warning(
            "The call has 4 or more copies of CYP2D6; it's lower confidence."
        )
    elif not call.genotype:
        logging.warning(
            "Cyrius made no call. It needs about 30x whole-genome data; check the "
            f"median depth ({depth}) and that the input is WGS, not exome."
        )
    write_outside_calls(
        str(Path(aln.outdir, f"{aln.stem}.cyp2d6.pharmcat.tsv")),
        [("CYP2D6", call.outside_call)] if call.outside_call else [],
    )
    logging.info(f"Cyrius results: {str(Path(aln.outdir, prefix + '.tsv'))}")
