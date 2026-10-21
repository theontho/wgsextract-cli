"""`typing hla`: HLA alleles with T1K.

T1K (MIT license) genotypes the HLA genes (class I A/B/C, class II DRB1/DQB1/
DPB1, ...) by aligning reads to every IPD-IMGT/HLA allele. Reads aligned to the
MHC, the HLA-* decoys and chromosome 6 ALT contigs (with their unmapped mates)
are extracted first, which keeps a 30x genome to under two minutes.

The allele index is built on first use from the current IPD-IMGT/HLA release
(a 45 MB download) into <reference library>/ref/t1k_hla.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from wgsextract_cli.core.reference_resolver import ReferenceLibrary
from wgsextract_cli.core.utils import WGSExtractError, run_command, run_pipeline

from .common import Alignment, env_tool, open_alignment, write_bed, write_outside_calls

INDEX_DIR = str(Path("ref", "t1k_hla"))
INDEX_FASTA = "hla_dna_seq.fa"
RELEASE_FILE = "release.txt"
# The MHC (GRCh38 chr6:28,510,120-33,480,577, GRCh37 chr6:28,477,797-33,448,354).
MHC = {"hg38": (28_510_119, 33_480_577), "hg19": (28_477_796, 33_448_354)}
_EXTRA_CONTIG = re.compile(r"^(?:HLA-|(?:chr)?6_)")
# Genes PharmCAT takes as outside calls, and those worth logging.
PHARMCAT_GENES = ("HLA-A", "HLA-B")
REPORTED_GENES = ("HLA-A", "HLA-B", "HLA-C", "HLA-DRB1", "HLA-DQA1", "HLA-DQB1",
                  "HLA-DPA1", "HLA-DPB1")  # fmt: skip


@dataclass(frozen=True)
class HlaCall:
    gene: str
    # Each allele T1K called: its equally likely names, and quality.
    alleles: tuple[tuple[tuple[str, ...], int], ...]

    def diplotype(self, resolve: Callable[[str], str] = str) -> str | None:
        """Both alleles when T1K is confident in each and its tied names agree
        once resolved (e.g. to two fields); homozygous when it found one."""
        called = []
        for names, quality in self.alleles:
            resolved = {resolve(name) for name in names}
            if quality <= 0 or len(resolved) != 1:
                return None
            called.append(resolved.pop())
        if not called:
            return None
        return "/".join(called * 2 if len(called) == 1 else called)


def parse_t1k(genotype_tsv: str) -> list[HlaCall]:
    """T1K's genotype TSV: gene, allele count, then (alleles, abundance,
    quality) for up to two alleles, and secondary alleles."""
    calls = []
    with open(genotype_tsv) as f:
        for line in f:
            fields = line.rstrip("\n").split("\t")
            if len(fields) < 8:
                continue
            alleles = tuple(
                (tuple(fields[i].split(",")), int(fields[i + 2]))
                for i in (2, 5)[: int(fields[1])]
            )
            calls.append(HlaCall(fields[0], alleles))
    return calls


def two_field(allele: str) -> str:
    """HLA-A*02:01:01:02 -> *02:01, keeping an expression suffix
    (*01:01:01:02N -> *01:01N), as PharmCAT names HLA alleles."""
    name = allele.split("*", 1)[-1]
    suffix = name[-1] if name[-1].isalpha() else ""
    return "*" + ":".join(name.rstrip(suffix).split(":")[:2]) + suffix


def _build_index(directory: str) -> None:
    build = env_tool("t1k-build.pl", "t1k")
    parent = str(Path(directory).parent)
    Path(parent).mkdir(parents=True, exist_ok=True)
    logging.info(
        "Building the T1K HLA index from the current IPD-IMGT/HLA release "
        "(downloads 45 MB, once)..."
    )
    with tempfile.TemporaryDirectory(dir=parent, prefix=".t1k_hla_") as work:
        try:
            run_command(
                [*build, "-o", work, "--download", "IPD-IMGT/HLA", "--prefix", "hla"],
                capture_output=True,
            )
        except subprocess.CalledProcessError as e:
            raise WGSExtractError(
                f"Building the T1K HLA index failed: {(e.stderr or '').strip()[-500:]}"
            ) from e
        release = "unknown"
        with open(str(Path(work, "hla.dat"))) as f:
            for line in f:
                if "IPD-IMGT/HLA Release Version" in line:
                    release = line.rsplit(" ", 1)[-1].strip()
                    break
        index = str(Path(work, "index"))
        Path(index).mkdir(parents=True)
        shutil.move(str(Path(work, INDEX_FASTA)), index)
        with open(str(Path(index, RELEASE_FILE)), "w") as f:
            f.write(f"IPD-IMGT/HLA {release}\n")
        Path(index).replace(directory)
    logging.info(f"T1K HLA index (IPD-IMGT/HLA {release}): {directory}")


def hla_index(args: argparse.Namespace) -> str:
    """--hla-index, or the library's index (built when missing)."""
    if args.hla_index:
        if not Path(args.hla_index).is_file():
            raise WGSExtractError(f"HLA index not found: {args.hla_index}")
        return str(args.hla_index)
    root = ReferenceLibrary(args.ref, skip_full_search=True).root
    if not root or not Path(root).is_dir():
        raise WGSExtractError(
            "No reference library to keep the T1K HLA index in: set "
            "reference_library in config.toml, or pass --hla-index."
        )
    directory = str(Path(root, INDEX_DIR))
    if not Path(directory, INDEX_FASTA).is_file():
        _build_index(directory)
    return str(Path(directory, INDEX_FASTA))


def _index_release(index: str) -> str | None:
    try:
        with (Path(index).parent / RELEASE_FILE).open() as f:
            return f.read().strip()
    except OSError:
        return None


def extract_reads(aln: Alignment, work: str) -> tuple[str, str]:
    """Read pairs from the MHC, HLA-* decoys and chr6 ALTs as FASTQ."""
    if aln.build not in MHC:
        raise WGSExtractError(f"No MHC coordinates for {aln.build}.")
    start, end = MHC[aln.build]
    regions = [(aln.contig("6"), start, end)] + [
        (name, 0, length)
        for name, length in aln.lengths.items()
        if _EXTRA_CONTIG.match(name)
    ]
    bed = write_bed(str(Path(work, "mhc.bed")), regions)
    r1, r2 = str(Path(work, "r1.fq")), str(Path(work, "r2.fq"))
    logging.info(
        f"Extracting reads from the MHC and {len(regions) - 1} HLA/chr6 ALT contigs..."
    )
    run_pipeline(
        [
            [
                "samtools",
                "view",
                "-u",
                "-F",
                "0x900",
                "-M",
                "--fetch-pairs",  # mates mapped outside the regions, too
                "-L",
                bed,
                *aln.ref_opt,
                "-@",
                aln.threads,
                aln.path,
            ],
            [
                "samtools",
                "collate",
                "-u",
                "-O",
                "-@",
                aln.threads,
                "-",
                str(Path(work, "collate")),
            ],
            [
                "samtools",
                "fastq",
                "-n",
                "-1",
                r1,
                "-2",
                r2,
                "-0",
                os.devnull,
                "-s",
                os.devnull,
                "-",
            ],
        ],  # fmt: skip
        description="HLA read extraction",
    )
    return r1, r2


def cmd_hla(args: argparse.Namespace) -> None:
    aln = open_alignment(args)
    run_t1k = env_tool("run-t1k", "t1k")
    index = hla_index(args)

    outputs = {
        "_genotype.tsv": f"{aln.stem}.t1k_genotype.tsv",
        "_allele.tsv": f"{aln.stem}.t1k_allele.tsv",
        "_allele.vcf": f"{aln.stem}.t1k_allele.vcf",
    }
    with tempfile.TemporaryDirectory(dir=aln.outdir, prefix=".wgse_hla_") as work:
        r1, r2 = extract_reads(aln, work)
        logging.info("Genotyping HLA genes with T1K...")
        try:
            run_command(
                [*run_t1k, "-1", r1, "-2", r2, "-f", index, "--preset", "hla-wgs",
                 "-t", aln.threads, "-o", "t1k", "--od", work],
                capture_output=not getattr(args, "debug", False),
            )  # fmt: skip
        except subprocess.CalledProcessError as e:
            detail = (e.stderr or "").strip()[-500:]
            raise WGSExtractError(f"T1K failed (exit {e.returncode}). {detail}") from e
        for suffix, name in outputs.items():
            source = str(Path(work, "t1k" + suffix))
            if Path(source).exists():
                shutil.move(source, str(Path(aln.outdir, name)))

    genotype_tsv = str(Path(aln.outdir, outputs["_genotype.tsv"]))
    calls = {call.gene: call for call in parse_t1k(genotype_tsv)}
    release = _index_release(index)
    logging.info(f"HLA alleles{f' ({release})' if release else ''}:")
    for gene in REPORTED_GENES:
        call = calls.get(gene)
        diplotype = (
            call.diplotype(lambda a: "*" + a.split("*", 1)[-1]) if call else None
        )
        logging.info(f"  {gene:<9} {diplotype or 'no confident call'}")

    pharmcat = []
    for gene in PHARMCAT_GENES:
        diplotype = calls[gene].diplotype(two_field) if gene in calls else None
        if diplotype:
            pharmcat.append((gene, diplotype))
        else:
            logging.warning(f"No confident {gene} call; it's left out for PharmCAT.")
    write_outside_calls(str(Path(aln.outdir, f"{aln.stem}.hla.pharmcat.tsv")), pharmcat)
    logging.info(f"T1K genotypes: {genotype_tsv}")
