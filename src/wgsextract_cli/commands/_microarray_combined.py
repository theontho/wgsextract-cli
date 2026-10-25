import argparse
import contextlib
import gzip
import logging
import os
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import TextIO

from wgsextract_cli.core.utils import (
    WGSExtractError,
    run_command,
)
from wgsextract_cli.core.variant_files import (
    chromosome_aliases,
    match_chromosome_name,
)

_BASES = {"A", "C", "G", "T", "N"}


# A small region's targets can all be homozygous reference; this many can't.
MIN_TARGETS_TO_EXPECT_A_VARIANT = 1000


def _normalize_microarray_genotype(value: str) -> str | None:
    """Return a two-letter SNP genotype (haploid calls doubled), "--", or None."""
    genotype = value.replace("/", "").replace("|", "").replace(".", "-").upper()
    if genotype in {"", "-", "--"}:
        return "--"
    if len(genotype) == 1 and genotype in _BASES:
        return genotype * 2
    if len(genotype) == 2 and set(genotype) <= _BASES:
        return genotype
    return None


def _combined_kit_chrom(chrom: str) -> str:
    return "MT" if chrom in ("chrM", "M", "chrMT") else chrom.removeprefix("chr")


def _command_lines(cmd: list[str], workdir: str) -> Iterator[str]:
    """Run ``cmd`` to completion (raising on failure), then yield its stdout lines."""
    with tempfile.TemporaryFile("w+", dir=workdir) as out:
        run_command(cmd, stdout=out)
        out.seek(0)
        yield from out


def _open_text(path: str) -> TextIO:
    if path.endswith((".gz", ".bgz")):
        return gzip.open(path, "rt")
    return open(path)


def _fasta_chromosomes(ref_fasta: str) -> set[str]:
    fai_path = ref_fasta + ".fai"
    if not Path(fai_path).exists():
        # samtools faidx builds the .fai (and .gzi for bgzipped FASTA).
        run_command(["samtools", "faidx", ref_fasta])
    with open(fai_path) as f_fai:
        return {line.split("\t", 1)[0] for line in f_fai}


def _reference_alleles(
    ref_fasta: str, ref_vcf_tab: str, outdir: str
) -> dict[tuple[str, str], str]:
    """Fetch the reference base of every target SNP in one samtools pass."""
    fasta_chroms = _fasta_chromosomes(ref_fasta)
    region_to_target: dict[str, tuple[str, str]] = {}
    fd, region_file = tempfile.mkstemp(suffix=".regions", dir=outdir)
    try:
        with os.fdopen(fd, "w") as f_reg, _open_text(ref_vcf_tab) as f_in:
            for line in f_in:
                if line.startswith("#"):
                    continue
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 2:
                    continue
                chrom, pos = parts[0], parts[1]
                fasta_chrom = match_chromosome_name(chrom, fasta_chroms)
                if fasta_chrom:
                    region = f"{fasta_chrom}:{pos}-{pos}"
                    f_reg.write(region + "\n")
                    region_to_target[region] = (chrom, pos)

        ref_alleles: dict[tuple[str, str], str] = {}
        current = None
        for line in _command_lines(
            ["samtools", "faidx", "--region-file", region_file, ref_fasta], outdir
        ):
            if line.startswith(">"):
                current = region_to_target.get(line[1:].strip())
            elif current:
                ref_alleles[current] = line.strip().upper()
        return ref_alleles
    finally:
        Path(region_file).unlink()


def _write_vcf_mode_kit(
    f_out: TextIO,
    *,
    args: argparse.Namespace,
    outdir: str,
    out_vcf: str,
    ref_fasta: str,
    ref_vcf_tab: str,
) -> None:
    # Positions absent from the input VCF are assumed homozygous reference.
    variant_calls: dict[tuple[str, str], str] = {}
    for line in _command_lines(
        ["bcftools", "query", "-f", "%CHROM\t%POS\t[%TGT]\n", out_vcf], outdir
    ):
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 3:
            continue
        genotype = _normalize_microarray_genotype(parts[2])
        if genotype is None:
            continue
        for alias in chromosome_aliases(parts[0]):
            variant_calls[(alias, parts[1])] = genotype
    logging.info(f"Found {len(variant_calls)} variants in input VCF matching targets.")
    # Absent targets read as the reference only on chromosomes the VCF has
    # calls on: a woman's VCF has no chrY, which mustn't become Y genotypes.
    called_contigs = {alias for alias, _pos in variant_calls}

    logging.info("Pre-fetching reference alleles for target SNPs...")
    ref_alleles = _reference_alleles(ref_fasta, ref_vcf_tab, outdir)
    logging.info(f"Pre-fetched {len(ref_alleles)} reference alleles.")

    with contextlib.ExitStack() as stack:
        targets: Iterator[str] = (
            _command_lines(
                ["tabix", ref_vcf_tab, *_in_targets_naming(args.region, ref_vcf_tab)],
                outdir,
            )
            if args.region
            else stack.enter_context(_open_text(ref_vcf_tab))
        )
        ref_col_idx = -1
        written = matched = 0
        for line in targets:
            if line.startswith("#"):
                header = line.strip().split("\t")
                if "REF" in header:
                    ref_col_idx = header.index("REF")
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 3:
                continue
            chrom, pos, rsid = parts[0], parts[1], parts[2]

            genotype = variant_calls.get((chrom, pos))
            written += 1
            matched += genotype is not None
            if genotype is None and chrom not in called_contigs:
                genotype = "--"
            elif genotype is None:
                ref_base = ""
                if ref_col_idx != -1 and len(parts) > ref_col_idx:
                    ref_base = parts[ref_col_idx].upper()
                if not ref_base:
                    ref_base = ref_alleles.get((chrom, pos), "")
                if ref_base not in _BASES:
                    ref_base = "N"
                genotype = ref_base * 2

            f_out.write(f"{rsid}\t{_combined_kit_chrom(chrom)}\t{pos}\t{genotype}\n")
    if written >= MIN_TARGETS_TO_EXPECT_A_VARIANT and not matched:
        # Every target was written as homozygous reference: a plausible-looking but
        # meaningless kit.
        logging.warning(
            f"None of {out_vcf}'s variants are at the kit's {written:,} target "
            "positions, so every genotype is written as the reference allele. Check "
            f"that the VCF's genome build matches the targets ({ref_vcf_tab})."
        )


def _in_targets_naming(region: str, targets: str) -> list[str]:
    """The region's contigs as the targets file names them (1 vs chr1)."""
    named = set(
        run_command(["tabix", "-l", targets], capture_output=True).stdout.split()
    )
    regions = []
    for part in (r.strip() for r in region.split(",") if r.strip()):
        contig, sep, span = part.partition(":")
        contig = next((a for a in chromosome_aliases(contig) if a in named), contig)
        regions.append(f"{contig}{sep}{span}")
    return regions


def _write_alignment_mode_kit(f_out: TextIO, *, outdir: str, out_vcf: str) -> None:
    rows = called = 0
    for line in _command_lines(
        ["bcftools", "query", "-f", "%ID\t%CHROM\t%POS\t[%TGT]\n", out_vcf], outdir
    ):
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 4:
            continue
        snp_id, chrom, pos, tgt = parts[:4]
        if snp_id == ".":
            snp_id = f"pos_{chrom}_{pos}"
        genotype = _normalize_microarray_genotype(tgt) or "--"
        f_out.write(f"{snp_id}\t{_combined_kit_chrom(chrom)}\t{pos}\t{genotype}\n")
        rows += 1
        called += genotype != "--"
    if not called:
        # mpileup writes no records at all when no read overlaps any target.
        found = (
            f"All {rows:,} target genotypes are no-calls (--)"
            if rows
            else ("No target genotypes were produced, so the kit is empty")
        )
        logging.warning(
            f"{found}: the input has no reads at the kit's positions, or the "
            "reference doesn't match it."
        )


def _write_microarray_combined_kit(
    *,
    args: argparse.Namespace,
    outdir: str,
    base_name: str,
    is_vcf: bool,
    out_vcf: str,
    ref_fasta: str,
    ref_vcf_tab: str,
) -> str:
    combined_kit_txt = str(Path(outdir, f"{base_name}_CombinedKit.txt"))
    logging.info(f"Extracting genotypes to {combined_kit_txt}...")
    start_ext = time.time()

    tmp_txt = combined_kit_txt + ".tmp"
    try:
        with open(tmp_txt, "w") as f_out:
            f_out.write("# RSID\tCHROM\tPOS\tRESULT\n")
            if is_vcf:
                _write_vcf_mode_kit(
                    f_out,
                    args=args,
                    outdir=outdir,
                    out_vcf=out_vcf,
                    ref_fasta=ref_fasta,
                    ref_vcf_tab=ref_vcf_tab,
                )
            else:
                _write_alignment_mode_kit(f_out, outdir=outdir, out_vcf=out_vcf)
        Path(tmp_txt).replace(combined_kit_txt)
    except (OSError, gzip.BadGzipFile, subprocess.SubprocessError, ValueError) as e:
        raise WGSExtractError(f"Failed to generate CombinedKit.txt: {e}") from e
    finally:
        if Path(tmp_txt).exists():
            Path(tmp_txt).unlink()

    logging.info(f"Extraction took {time.time() - start_ext:.2f}s")
    return combined_kit_txt
