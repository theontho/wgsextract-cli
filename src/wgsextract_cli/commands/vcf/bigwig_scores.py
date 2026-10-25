"""Annotate a VCF with per-base scores from a bigWig (GERP++, PhyloP)."""

import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from wgsextract_cli.core.bigwig_resources import open_bigwig
from wgsextract_cli.core.utils import WGSExtractError, popen, run_command
from wgsextract_cli.core.variant_files import (
    check_vcf_build,
    match_chromosome_name,
    vcf_index_chromosomes,
)

from .annotation_helpers import annotate_vcf


def _score_rows(vcf: str, bw: Any, label: str) -> Iterator[str]:
    """CHROM POS REF ALT SCORE rows (the VCF's contig names) for each ALT of its
    variant records, scoring each with the maximum score over its REF bases."""
    bw_lengths: dict[str, int] = bw.chroms()
    to_bw = {
        chrom: match_chromosome_name(chrom, bw_lengths)
        for chrom in vcf_index_chromosomes(vcf)
    }
    if not any(to_bw.values()):
        raise WGSExtractError(
            f"None of the VCF's contigs are in the {label} scores; check its genome build."
        )
    with tempfile.TemporaryFile() as err:
        proc = popen(
            ["bcftools", "query", "-f", r"%CHROM\t%POS\t%REF\t%ALT\n", vcf],
            stdout=subprocess.PIPE,
            stderr=err,
            text=True,
        )
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                chrom, pos, ref, alt = line.rstrip("\n").split("\t")
                bw_chrom = to_bw.get(chrom)
                # One row per ALT (annotate_vcf matches biallelic records),
                # skipping gVCF reference blocks and symbolic (structural) alleles.
                alts = [
                    a for a in alt.split(",") if a not in (".", "*") and a[0] != "<"
                ]
                if not bw_chrom or not alts:
                    continue
                start = int(pos) - 1
                end = min(start + len(ref), bw_lengths[bw_chrom])
                if start >= end:
                    continue
                score = bw.stats(bw_chrom, start, end, type="max", exact=True)[0]
                if score is not None:
                    for a in alts:
                        yield f"{chrom}\t{pos}\t{ref}\t{a}\t{score:.4g}\n"
            if proc.wait() != 0:
                err.seek(0)
                raise WGSExtractError(
                    f"bcftools query failed on {vcf}: {err.read().decode().strip()}"
                )
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()


def annotate_from_bigwig(
    vcf_in: str,
    bigwig: str,
    tag: str,
    label: str,
    description: str,
    ann_out: str,
    outdir: str,
    threads: int | str | None = None,
) -> None:
    """Write ann_out: vcf_in with INFO/<tag>, the maximum bigWig score over each
    variant's REF bases."""
    bw = open_bigwig(bigwig)
    try:
        check_vcf_build(vcf_in, bw.chroms(), f"the {label} scores")
        with tempfile.TemporaryDirectory(
            dir=outdir, prefix=f".wgse_{tag.lower()}_"
        ) as work:
            scores = str(Path(work, "scores.tsv.gz"))
            with open(scores, "wb") as out:
                bgzip = popen(
                    ["bgzip", "-c"],
                    stdin=subprocess.PIPE,
                    stdout=out,
                    text=True,
                )
                assert bgzip.stdin is not None
                try:
                    bgzip.stdin.writelines(_score_rows(vcf_in, bw, label))
                finally:
                    bgzip.stdin.close()
                    if bgzip.wait() != 0:
                        raise WGSExtractError(f"bgzip failed writing {label} scores.")
            run_command(["tabix", "-s1", "-b2", "-e2", scores], capture_output=True)
            annotate_vcf(
                vcf_in,
                scores,
                f"CHROM,POS,REF,ALT,INFO/{tag}",
                ann_out,
                work,
                label,
                [
                    f"##INFO=<ID={tag},Number=1,Type=Float,"
                    f'Description="{description} (maximum over the REF bases)">'
                ],
                threads=threads,
            )
    finally:
        bw.close()
