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

from wgsextract_cli.core.microarray_identity import identity_policy
from wgsextract_cli.core.microarray_quality import MicroarrayQuality, quality_policy
from wgsextract_cli.core.microarray_utils import sort_alleles
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
    """Return a two-letter SNP genotype (haploid calls doubled, heterozygous
    alleles in alphabetical order), "--", or None."""
    genotype = value.replace("/", "").replace("|", "").replace(".", "-").upper()
    if genotype in {"", "-", "--"}:
        return "--"
    if len(genotype) == 1 and genotype in _BASES:
        return genotype * 2
    if len(genotype) == 2 and set(genotype) <= _BASES:
        return sort_alleles(genotype)
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
        with os.fdopen(fd, "w", newline="\n") as f_reg, _open_text(ref_vcf_tab) as f_in:
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


def _validate_quality_sample(
    policy: MicroarrayQuality,
    out_vcf: str,
    outdir: str,
    *,
    identity_enabled: bool = False,
) -> None:
    if policy.needs_fields or identity_enabled:
        samples = [
            line
            for line in _command_lines(["bcftools", "query", "-l", out_vcf], outdir)
            if line.strip()
        ]
        if len(samples) != 1:
            raise WGSExtractError(
                "Microarray call-policy options require a single-sample VCF; "
                "select one sample with bcftools view -s first."
            )


def _write_vcf_mode_kit(
    f_out: TextIO,
    *,
    args: argparse.Namespace,
    outdir: str,
    out_vcf: str,
    ref_fasta: str,
    ref_vcf_tab: str,
    reasons: dict[tuple[str, str], str] | None = None,
    restrict_reference_fill: bool = False,
) -> None:
    policy = quality_policy(args)
    identity = identity_policy(args)
    fields = policy.needs_fields or identity.needs_alleles
    _validate_quality_sample(policy, out_vcf, outdir, identity_enabled=identity.enabled)
    variant_calls: dict[tuple[str, str], str] = {}
    variant_reasons: dict[tuple[str, str], str] = {}
    strand_masked: set[tuple[str, str]] = set()
    query = "%CHROM\t%POS\t[%TGT]\n"
    if fields:
        query = "%CHROM\t%POS\t[%TGT\t%DP\t%GQ]\t%REF\t%ALT\t%FILTER\n"
    for line in _command_lines(
        [
            "bcftools",
            "query",
            *(["-u"] if fields else []),
            "-f",
            query,
            out_vcf,
        ],
        outdir,
    ):
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 3:
            continue
        genotype = _normalize_microarray_genotype(parts[2])
        reason = "missing_gt" if genotype == "--" else "called"
        identity_rejection = None
        if fields and len(parts) >= 8:
            rejection = policy.rejection(parts[3], parts[4], parts[-1])
            if rejection:
                genotype, reason = "--", rejection
            rejection = identity.rejection("", parts[5], parts[6])
            if rejection:
                genotype, reason = "--", rejection
                identity_rejection = rejection
        if genotype is None:
            if not (policy.min_dp or policy.min_gq or policy.exclude_filtered):
                continue
            genotype, reason = "--", "unsupported_allele"
        for alias in chromosome_aliases(parts[0]):
            key = (alias, parts[1])
            if identity_rejection == "strand_ambiguous":
                strand_masked.add(key)
            # A later duplicate must not restore a deliberately masked site.
            masked = key in strand_masked
            variant_calls[key] = "--" if masked else genotype
            if reasons is not None:
                variant_reasons[key] = "strand_ambiguous" if masked else reason
    logging.info(f"Found {len(variant_calls)} variants in input VCF matching targets.")
    # Absent targets read as the reference only on chromosomes the VCF has
    # calls on: a woman's VCF has no chrY, which mustn't become Y genotypes.
    called_contigs = {alias for alias, _pos in variant_calls}
    if restrict_reference_fill:
        # Extra panel hits must not make legacy-only targets appear covered.
        called_contigs = set()
        for line in _target_lines(args, ref_vcf_tab, outdir):
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 2 and (parts[0], parts[1]) in variant_calls:
                called_contigs.add(parts[0])

    logging.info("Pre-fetching reference alleles for target SNPs...")
    ref_alleles = (
        _reference_alleles(ref_fasta, ref_vcf_tab, outdir)
        if policy.assume_hom_ref and not (policy.min_dp or policy.min_gq)
        else {}
    )
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
            reason = variant_reasons.get((chrom, pos), "called")
            written += 1
            matched += genotype is not None
            if genotype is None and chrom not in called_contigs:
                genotype = "--"
                reason = "absent_contig"
            elif genotype is None and not policy.assume_hom_ref:
                genotype, reason = "--", "absent_from_vcf"
            elif genotype is None and (policy.min_dp or policy.min_gq):
                genotype, reason = (
                    "--",
                    policy.rejection(".", ".", "PASS") or "missing_gt",
                )
            elif genotype is None:
                ref_base = ""
                if ref_col_idx != -1 and len(parts) > ref_col_idx:
                    ref_base = parts[ref_col_idx].upper()
                if not ref_base:
                    ref_base = ref_alleles.get((chrom, pos), "")
                if ref_base not in _BASES:
                    ref_base = "N"
                genotype = ref_base * 2
                reason = "assumed_hom_ref"

            rejection = identity.rejection(rsid)
            if rejection:
                genotype, reason = "--", rejection
            f_out.write(f"{rsid}\t{_combined_kit_chrom(chrom)}\t{pos}\t{genotype}\n")
            if reasons is not None:
                reasons[(_combined_kit_chrom(chrom), pos)] = reason
    if written >= MIN_TARGETS_TO_EXPECT_A_VARIANT and not matched:
        if identity.enabled:
            logging.warning(
                f"None of {out_vcf}'s variants match the kit's {written:,} targets. "
                "Check the input build and selected marker/call policies."
            )
            return
        # Every target was written as homozygous reference: a plausible-looking but
        # meaningless kit.
        logging.warning(
            f"None of {out_vcf}'s variants are at the kit's {written:,} target "
            "positions, so "
            + (
                "every genotype is written as the reference allele. Check "
                if policy.assume_hom_ref
                else "absent markers are written as no-calls. Check "
            )
            + f"that the VCF's genome build matches the targets ({ref_vcf_tab})."
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


def _write_alignment_mode_kit(
    f_out: TextIO,
    *,
    outdir: str,
    out_vcf: str,
    policy: MicroarrayQuality | None = None,
    args: argparse.Namespace | None = None,
    reasons: dict[tuple[str, str], str] | None = None,
) -> None:
    policy = policy or MicroarrayQuality()
    identity = identity_policy(args or argparse.Namespace())
    fields = policy.needs_fields or identity.needs_alleles
    _validate_quality_sample(policy, out_vcf, outdir, identity_enabled=identity.enabled)
    rows = called = 0
    query = "%ID\t%CHROM\t%POS\t[%TGT]\n"
    if fields:
        query = "%ID\t%CHROM\t%POS\t[%TGT\t%DP\t%GQ]\t%REF\t%ALT\t%FILTER\n"
    for line in _command_lines(
        [
            "bcftools",
            "query",
            *(["-u"] if fields else []),
            "-f",
            query,
            out_vcf,
        ],
        outdir,
    ):
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 4:
            continue
        snp_id, chrom, pos, tgt = parts[:4]
        if snp_id == ".":
            snp_id = f"pos_{chrom}_{pos}"
        normalized = _normalize_microarray_genotype(tgt)
        genotype = normalized or "--"
        reason = (
            "unsupported_allele"
            if normalized is None
            else ("missing_gt" if genotype == "--" else "called")
        )
        if fields and len(parts) >= 9:
            rejection = policy.rejection(parts[4], parts[5], parts[-1])
            if rejection:
                genotype, reason = "--", rejection
            rejection = identity.rejection(snp_id, parts[6], parts[7])
        else:
            rejection = identity.rejection(snp_id)
        if rejection:
            genotype, reason = "--", rejection
        f_out.write(f"{snp_id}\t{_combined_kit_chrom(chrom)}\t{pos}\t{genotype}\n")
        if reasons is not None and reason != "called":
            reasons[(_combined_kit_chrom(chrom), pos)] = reason
        rows += 1
        called += genotype != "--"
    if not called:
        # mpileup writes no records at all when no read overlaps any target.
        found = (
            f"All {rows:,} target genotypes are no-calls (--)"
            if rows
            else ("No target genotypes were produced, so the kit is empty")
        )
        if rows and (
            policy.min_dp
            or policy.min_gq
            or policy.exclude_filtered
            or identity.enabled
        ):
            gates = (
                "configured marker-identity/quality gates"
                if identity.enabled
                else "configured quality/FILTER gates"
            )
            logging.warning(
                f"{found}: no genotype passed the {gates}. "
                "Check the thresholds and input evidence."
            )
        else:
            logging.warning(
                f"{found}: the input has no reads at the kit's positions, or the "
                "reference doesn't match it."
            )


def _target_lines(args: argparse.Namespace, targets: str, outdir: str) -> Iterator[str]:
    if args.region:
        yield from _command_lines(
            ["tabix", targets, *_in_targets_naming(args.region, targets)], outdir
        )
    else:
        with _open_text(targets) as source:
            yield from source


def _read_evidence(
    args: argparse.Namespace, targets: str, outdir: str, ref_fasta: str
) -> dict[tuple[str, str], str]:
    fd, bed = tempfile.mkstemp(suffix=".call-reasons.bed", dir=outdir)
    evidence: dict[tuple[str, str], str] = {}
    try:
        with os.fdopen(fd, "w", newline="\n") as destination:
            for line in _target_lines(args, targets, outdir):
                if line.startswith("#") or not line.strip():
                    continue
                chrom, pos, *_ = line.rstrip("\n").split("\t")
                if pos.isdigit() and int(pos) > 0:
                    destination.write(f"{chrom}\t{int(pos) - 1}\t{pos}\n")
        regions = _in_targets_naming(args.region, targets) if args.region else [None]
        for mapq in (0, 10):
            for region in regions:
                for line in _command_lines(
                    [
                        "samtools",
                        "depth",
                        "--reference",
                        ref_fasta,
                        *(["-r", region] if region else []),
                        "-b",
                        bed,
                        "-q",
                        "0",
                        "-Q",
                        str(mapq),
                        args.input,
                    ],
                    outdir,
                ):
                    fields = line.rstrip("\n").split("\t")
                    if len(fields) >= 3 and int(fields[2]) > 0:
                        evidence[(_combined_kit_chrom(fields[0]), fields[1])] = (
                            "low_mapq" if mapq == 0 else "missing_gt"
                        )
        return evidence
    finally:
        Path(bed).unlink(missing_ok=True)


def _write_reason_report(
    combined: str,
    args: argparse.Namespace,
    targets: str,
    outdir: str,
    is_vcf: bool,
    reasons: dict[tuple[str, str], str],
    ref_fasta: str,
) -> None:
    calls = {}
    with open(combined) as source:
        for line in source:
            if not line.startswith("#"):
                fields = line.rstrip("\n").split("\t")
                if len(fields) >= 4:
                    calls[(fields[1], fields[2])] = fields[3]
    evidence = {} if is_vcf else _read_evidence(args, targets, outdir, ref_fasta)
    report = str(Path(combined).with_name(Path(combined).stem + "_reasons.tsv"))
    staged = report + ".tmp"
    try:
        with open(staged, "w", newline="\n") as destination:
            destination.write("# RSID\tCHROM\tPOS\tRESULT\tCALL_REASON\n")
            for line in _target_lines(args, targets, outdir):
                if line.startswith("#") or not line.strip():
                    continue
                chrom, pos, rsid, *_ = line.rstrip("\n").split("\t")
                key = (_combined_kit_chrom(chrom), pos)
                genotype = calls.get(key, "--")
                reason = reasons.get(
                    key, "called" if genotype != "--" else "missing_gt"
                )
                if not is_vcf and genotype == "--" and reason == "missing_gt":
                    reason = evidence.get(key, "no_reads")
                destination.write(f"{rsid}\t{key[0]}\t{pos}\t{genotype}\t{reason}\n")
        Path(staged).replace(report)
    finally:
        Path(staged).unlink(missing_ok=True)


def _write_microarray_combined_kit(
    *,
    args: argparse.Namespace,
    outdir: str,
    base_name: str,
    is_vcf: bool,
    out_vcf: str,
    ref_fasta: str,
    ref_vcf_tab: str,
    restrict_reference_fill: bool = False,
) -> str:
    combined_kit_txt = str(Path(outdir, f"{base_name}_CombinedKit.txt"))
    logging.info(f"Extracting genotypes to {combined_kit_txt}...")
    start_ext = time.time()
    policy = quality_policy(args)
    reasons: dict[tuple[str, str], str] | None = {} if policy.call_reasons else None

    tmp_txt = combined_kit_txt + ".tmp"
    try:
        with open(tmp_txt, "w", newline="\n") as f_out:
            f_out.write("# RSID\tCHROM\tPOS\tRESULT\n")
            if is_vcf:
                _write_vcf_mode_kit(
                    f_out,
                    args=args,
                    outdir=outdir,
                    out_vcf=out_vcf,
                    ref_fasta=ref_fasta,
                    ref_vcf_tab=ref_vcf_tab,
                    reasons=reasons,
                    restrict_reference_fill=restrict_reference_fill,
                )
            else:
                _write_alignment_mode_kit(
                    f_out,
                    outdir=outdir,
                    out_vcf=out_vcf,
                    policy=policy,
                    args=args,
                    reasons=reasons,
                )
        Path(tmp_txt).replace(combined_kit_txt)
        if reasons is not None:
            _write_reason_report(
                combined_kit_txt, args, ref_vcf_tab, outdir, is_vcf, reasons, ref_fasta
            )
    except (OSError, gzip.BadGzipFile, subprocess.SubprocessError, ValueError) as e:
        raise WGSExtractError(f"Failed to generate CombinedKit.txt: {e}") from e
    finally:
        if Path(tmp_txt).exists():
            Path(tmp_txt).unlink()

    logging.info(f"Extraction took {time.time() - start_ext:.2f}s")
    return combined_kit_txt
