import json
import os
import re
from collections.abc import Iterable
from fnmatch import fnmatch
from typing import Any

from wgsextract_cli.core.builds import (
    BUILD_PATH_TOKENS,
    build_from_path,
    canonical_build,
)

FASTA_EXTENSIONS = (".fa", ".fasta", ".fna", ".fa.gz", ".fasta.gz", ".fna.gz")
FASTA_SUFFIX_RE = re.compile(r"\.(fa|fasta|fna)(\.gz)?$", re.IGNORECASE)
TARGET_PATTERNS = (
    "All_SNPs*.tab.gz",
    "All_SNPs*.vcf.gz",
    "snps_*.vcf.gz",
    "common_all.vcf.gz",
)


def build_hint(*values: str | None) -> str:
    for value in values:
        if not value:
            continue
        build = build_from_path(value)
        if build:
            return build
    return ""


def is_fasta_path(path: str) -> bool:
    return path.lower().endswith(FASTA_EXTENSIONS)


def fasta_prefix(fasta: str) -> str:
    """Strip the FASTA extension: ref/hg38.fa.gz -> ref/hg38."""
    return FASTA_SUFFIX_RE.sub("", fasta)


def reference_dict_path(fasta: str) -> str:
    """Sequence dictionary path GATK/Picard expect: hg38.fa.gz -> hg38.dict."""
    return fasta_prefix(fasta) + ".dict"


def n_count_paths(fasta: str, outdir: str | None = None) -> tuple[str, str]:
    """Return the (_ncnt.csv, _nbin.csv) sidecar paths for a FASTA."""
    prefix = fasta_prefix(fasta)
    if outdir:
        prefix = os.path.join(outdir, os.path.basename(prefix))
    return prefix + "_ncnt.csv", prefix + "_nbin.csv"


def default_reference_library() -> str:
    from wgsextract_cli.core.runtime import repo_root

    return str(repo_root() / "reference")


def resolve_reference_library(explicit: str | None = None) -> str:
    """Explicit path, else configured reference_library, else <repo>/reference."""
    from wgsextract_cli.core.config import settings

    configured = settings.get("reference_library")
    reflib = explicit or (str(configured) if configured else None)
    return os.path.abspath(os.path.expanduser(reflib or default_reference_library()))


def read_fai_lengths(fasta: str) -> dict[str, int]:
    """Contig lengths from the FASTA's .fai index; empty if it is missing or unreadable."""
    lengths: dict[str, int] = {}
    try:
        with open(fasta + ".fai", encoding="utf-8") as fai:
            for line in fai:
                cols = line.split("\t")
                if len(cols) >= 2:
                    lengths[cols[0]] = int(cols[1])
    except (OSError, ValueError):
        return {}
    return lengths


def sequence_signature(contig_lengths: dict[str, int]) -> str:
    """WGS Extract SN/LN signature, the same value calculate_bam_md5 gives a BAM header."""
    from wgsextract_cli.core.variant_files import calculate_bam_md5

    header = "".join(
        f"@SQ\tSN:{name}\tLN:{length}\n" for name, length in contig_lengths.items()
    )
    return calculate_bam_md5("", header=header)


def fasta_signature(fasta: str) -> str | None:
    lengths = read_fai_lengths(fasta)
    return sequence_signature(lengths) if lengths else None


def find_fasta_by_signature(directories: Iterable[str], signature: str) -> str | None:
    """Find an indexed FASTA whose sequence dictionary matches a BAM/CRAM signature."""
    for directory in directories:
        for candidate in reference_fasta_candidates(directory):
            if fasta_signature(candidate) == signature:
                return candidate
    return None


def find_reference_fasta(root: str, *hints: str | None) -> str:
    candidates = []
    for directory in [root, os.path.join(root, "genomes")]:
        candidates.extend(reference_fasta_candidates(directory))
    return select_reference_fasta(candidates, *hints)


def resolve_input_reference_fasta(input_path: str | None) -> str:
    if not input_path:
        return ""
    directory = os.path.dirname(input_path) or "."
    if not os.path.isdir(directory):
        return ""
    manifest_path = os.path.join(directory, "manifest.json")
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, encoding="utf-8") as manifest_file:
                manifest: dict[str, Any] = json.load(manifest_file)
            ref_file = manifest.get("files", {}).get("ref")
            if ref_file:
                candidate = os.path.join(directory, ref_file)
                if os.path.isfile(candidate):
                    return candidate
        except (OSError, json.JSONDecodeError, TypeError, AttributeError):
            pass
    return select_reference_fasta(reference_fasta_candidates(directory), input_path)


def reference_fasta_candidates(directory: str) -> list[str]:
    if not os.path.isdir(directory):
        return []
    return [
        os.path.join(directory, file_name)
        for file_name in sorted(os.listdir(directory))
        if is_fasta_path(file_name)
        and os.path.isfile(os.path.join(directory, file_name))
    ]


def select_reference_fasta(candidates: list[str], *hints: str | None) -> str:
    if not candidates:
        return ""
    build = build_hint(*hints)
    if build:
        for alias in BUILD_PATH_TOKENS.get(build, (build,)):
            for candidate in candidates:
                if alias in os.path.basename(candidate).lower():
                    return candidate
    if len(candidates) == 1:
        return candidates[0]
    return ""


def find_annotation_vcf(reflib: str, build: str) -> str:
    directories = [
        reflib,
        os.path.join(reflib, "ref"),
        os.path.join(reflib, "microarray"),
        os.path.join(reflib, "genomes", "microarray"),
    ]
    names = annotation_vcf_names(build)
    return first_existing_named_file(directories, names)


def annotation_vcf_names(build: str) -> list[str]:
    """Candidate SNP target file names; with a known build, only that build's files."""
    generic = ["All_SNPs.vcf.gz", "common_all.vcf.gz"]
    build_specific = [
        "snps_hg19.vcf.gz",
        "snps_hg38.vcf.gz",
        "snps_grch37.vcf.gz",
        "snps_grch38.vcf.gz",
        "All_SNPs_hg19_ref.tab.gz",
        "All_SNPs_hg38_ref.tab.gz",
        "All_SNPs_HG19_ref.tab.gz",
        "All_SNPs_HG38_ref.tab.gz",
        "All_SNPs_GRCh37_ref.tab.gz",
        "All_SNPs_GRCh38_ref.tab.gz",
        "All_SNPs_grch37_ref.tab.gz",
        "All_SNPs_grch38_ref.tab.gz",
    ]
    if not build:
        return generic + build_specific
    # A file for another build would silently give wrong-build coordinates.
    tokens = {"hg19": ("hg19", "grch37"), "hg38": ("hg38", "grch38")}.get(
        canonical_build(build) or "", ()
    )
    matching = [n for n in build_specific if any(t in n.lower() for t in tokens)]
    return matching + generic


def find_annotation_resource(
    reflib: str, prefix: str, build: str, extensions: list[str]
) -> str:
    directories = [reflib, os.path.join(reflib, "ref")]
    patterns: list[str] = []
    if build:
        patterns.extend(f"{prefix}*{build}*{extension}" for extension in extensions)
    patterns.extend(f"{prefix}*{extension}" for extension in extensions)
    return first_matching_file(directories, patterns)


def resolve_input_target_tab(input_path: str | None) -> str:
    if not input_path:
        return ""
    directory = os.path.dirname(input_path) or "."
    if not os.path.isdir(directory):
        return ""
    stem = input_stem(input_path)
    exact = first_existing_file(
        [
            os.path.join(directory, f"{stem}.targets.tab.gz"),
            os.path.join(directory, f"{stem}.target.tab.gz"),
            os.path.join(directory, f"{stem}.snps.tab.gz"),
        ]
    )
    if exact:
        return exact
    return first_matching_file(
        [directory],
        ["*.targets.tab.gz", "*.target.tab.gz", "*.snps.tab.gz", *TARGET_PATTERNS],
    )


def find_reference_target_tab(reflib: str, build: str) -> str:
    directories = [
        reflib,
        os.path.join(reflib, "ref"),
        os.path.join(reflib, "microarray"),
        os.path.join(reflib, "genomes", "microarray"),
    ]
    names = annotation_vcf_names(build)
    # Genome-wide All_SNPs tabs before snps_*.vcf.gz (a Y-SNP set in the bootstrap).
    names.sort(key=lambda name: not name.endswith("_ref.tab.gz"))
    named = first_existing_named_file(directories, names)
    if named or build:
        return named
    return first_matching_file(directories, list(TARGET_PATTERNS))


def input_stem(path: str) -> str:
    name = os.path.basename(path)
    for extension in (".vcf.gz", ".bam", ".cram", ".vcf", ".bcf"):
        if name.lower().endswith(extension):
            return name[: -len(extension)]
    return os.path.splitext(name)[0]


def first_existing_file(paths: list[str]) -> str:
    for path in paths:
        if os.path.isfile(path):
            return path
    return ""


def first_existing_named_file(directories: list[str], names: list[str]) -> str:
    for directory in directories:
        if not os.path.isdir(directory):
            continue
        for name in names:
            path = os.path.join(directory, name)
            if os.path.isfile(path):
                return path
    return ""


def first_matching_file(directories: list[str], patterns: list[str]) -> str:
    for directory in directories:
        if not os.path.isdir(directory):
            continue
        for file_name in sorted(os.listdir(directory)):
            for pattern in patterns:
                if fnmatch(file_name.lower(), pattern.lower()):
                    path = os.path.join(directory, file_name)
                    if os.path.isfile(path):
                        return path
    return ""
