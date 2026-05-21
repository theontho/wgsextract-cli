import json
import os
from fnmatch import fnmatch
from typing import Any

from wgsextract_cli.core.builds import build_from_path

FASTA_EXTENSIONS = (".fa", ".fasta", ".fna", ".fa.gz", ".fasta.gz", ".fna.gz")
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


def find_reference_fasta(root: str, *hints: str | None) -> str:
    candidates = []
    for directory in [root, os.path.join(root, "genomes")]:
        candidates.extend(reference_fasta_candidates(directory))
    return select_reference_fasta(candidates, *hints)


def resolve_input_reference_fasta(input_path: str | None) -> str:
    if not input_path:
        return ""
    directory = os.path.dirname(input_path)
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
        preferred = _build_aliases(build)
        for alias in preferred:
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
    names = [
        "All_SNPs.vcf.gz",
        "common_all.vcf.gz",
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
    preferred: list[str] = []
    if build == "hg19":
        preferred = [
            name for name in names if "hg19" in name.lower() or "grch37" in name.lower()
        ]
    elif build == "hg38":
        preferred = [
            name for name in names if "hg38" in name.lower() or "grch38" in name.lower()
        ]
    return preferred + [name for name in names if name not in preferred]


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
    directory = os.path.dirname(input_path)
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
    named = first_existing_named_file(directories, annotation_vcf_names(build))
    if named:
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


def _build_aliases(build: str) -> list[str]:
    if build == "hg19":
        return ["hg19", "hg37", "grch37", "hs37"]
    if build == "hg38":
        return ["hg38", "grch38", "hs38"]
    if build == "t2t":
        return ["t2t", "chm13"]
    return [build]
