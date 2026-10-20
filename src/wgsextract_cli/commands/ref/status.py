import argparse
import contextlib
import json
import os
import re
from typing import Any

from wgsextract_cli.core.config import settings
from wgsextract_cli.core.gene_map import are_gene_maps_installed
from wgsextract_cli.core.ref_library.catalog import get_genome_status
from wgsextract_cli.core.reference_assets import (
    build_hint,
    default_reference_library,
    find_annotation_resource,
    find_annotation_vcf,
    first_existing_file,
    is_fasta_path,
)
from wgsextract_cli.core.reference_processing import bootstrap_has_support_assets
from wgsextract_cli.core.reference_resolver import ReferenceLibrary

# Annotation data the ref commands download into <library>/ref, as
# (key, label, file extensions, builds): <key>_<build><ext>, tabix-indexed.
ANNOTATION_DATA = [
    ("clinvar", "ClinVar", (".vcf.gz",), ("hg19", "hg38")),
    ("gnomad", "gnomAD", (".vcf.bgz", ".vcf.gz"), ("hg19", "hg38")),
    ("revel", "REVEL", (".tsv.gz",), ("hg19", "hg38")),
    ("phylop", "PhyloP", (".tsv.gz",), ("hg19", "hg38")),
    ("spliceai", "SpliceAI", (".vcf.gz", ".vcf.bgz"), ("hg19", "hg38")),
    ("alphamissense", "AlphaMissense", (".tsv.gz",), ("hg19", "hg38")),
    ("pharmgkb", "PharmGKB", (".tsv.gz", ".vcf.gz"), ("hg38",)),
]


def cmd_ref_status(args: argparse.Namespace) -> None:
    status = build_ref_status(args)
    if getattr(args, "values", False):
        print(json.dumps({"values": ref_status_values(status)}, sort_keys=True))
        return
    if getattr(args, "json", False):
        print(json.dumps(status, sort_keys=True))
        return
    print_ref_status(status)


def build_ref_status(args: argparse.Namespace) -> dict[str, Any]:
    reflib = _resolve_reference_library(args)
    genome_library = _resolve_genome_library(args, reflib)
    input_path = getattr(args, "input", None)
    annotation_vcf = getattr(args, "annotation_vcf", None) or ""
    lib = ReferenceLibrary(reflib, input_path=input_path)
    build = build_hint(lib.build, input_path, reflib)
    annotation_file = lib.ref_vcf_tab or find_annotation_vcf(reflib, build)
    spliceai_file = lib.spliceai_vcf or find_annotation_resource(
        reflib, "spliceai", build, [".vcf.gz", ".vcf.bgz"]
    )
    alphamissense_file = lib.alphamissense_vcf or find_annotation_resource(
        reflib, "alphamissense", build, [".tsv.gz", ".vcf.gz", ".vcf.bgz"]
    )
    pharmgkb_file = lib.pharmgkb_vcf or find_annotation_resource(
        reflib, "pharmgkb", build, [".vcf.gz", ".vcf.bgz", ".tsv.gz"]
    )
    annotation_argument = annotation_vcf or annotation_file
    test_genome = _test_genome_status(genome_library)

    return {
        "referenceLibrary": {
            "path": reflib,
            "exists": os.path.isdir(reflib),
        },
        "genomeLibrary": {
            "path": genome_library,
            "exists": os.path.isdir(genome_library),
        },
        "build": build,
        "bootstrap": {
            "installed": bootstrap_has_support_assets(reflib),
        },
        "geneMap": {
            "installed": are_gene_maps_installed(reflib),
        },
        "annotationVcf": {
            "installed": bool(annotation_file),
            "file": annotation_file,
            "argument": annotation_argument,
            "ready": bool(annotation_argument),
        },
        "spliceai": {
            "installed": bool(spliceai_file),
            "file": spliceai_file,
        },
        "alphamissense": {
            "installed": bool(alphamissense_file),
            "file": alphamissense_file,
        },
        "pharmgkb": {
            "installed": bool(pharmgkb_file),
            "file": pharmgkb_file,
        },
        "annotations": _annotation_status(reflib),
        "vepCache": _vep_cache_status(lib, reflib),
        "pharmcat": _pharmcat_status(reflib),
        "genomes": _genome_status(reflib),
        "ploidy": _ploidy_status(reflib),
        "mappabilityMaps": _mappability_map_status(reflib),
        "testGenome": test_genome,
    }


def ref_status_values(status: dict[str, Any]) -> dict[str, str]:
    return {
        "library.geneMapInstalled": _json_bool(status["geneMap"]["installed"]),
        "library.isBootstrapped": _json_bool(status["bootstrap"]["installed"]),
        "library.annotationVcfInstalled": _json_bool(
            status["annotationVcf"]["installed"]
        ),
        "library.annotationVcfFile": status["annotationVcf"]["file"],
        "library.annotationVcfArgument": status["annotationVcf"]["argument"],
        "library.annotationVcfReady": _json_bool(status["annotationVcf"]["ready"]),
        "library.spliceaiInstalled": _json_bool(status["spliceai"]["installed"]),
        "library.spliceaiFile": status["spliceai"]["file"],
        "library.alphamissenseInstalled": _json_bool(
            status["alphamissense"]["installed"]
        ),
        "library.alphamissenseFile": status["alphamissense"]["file"],
        "library.pharmgkbInstalled": _json_bool(status["pharmgkb"]["installed"]),
        "library.pharmgkbFile": status["pharmgkb"]["file"],
        **{
            f"library.{key}Installed": _json_bool(
                any(entry["installed"] for entry in builds.values())
            )
            for key, builds in status["annotations"].items()
            if key not in ("spliceai", "alphamissense", "pharmgkb")
        },
        "library.vepCacheInstalled": _json_bool(bool(status["vepCache"]["versions"])),
        "library.pharmcatInstalled": _json_bool(status["pharmcat"]["installed"]),
        "library.testGenomeInstalled": _json_bool(status["testGenome"]["installed"]),
        "library.testGenomeStatus": status["testGenome"]["status"],
        "library.testGenomePath": status["testGenome"]["path"],
    }


def print_ref_status(status: dict[str, Any]) -> None:
    library = status["referenceLibrary"]
    print(f"Reference library: {library['path']}")
    if not library["exists"]:
        print("  (this folder does not exist; set reference_library or pass --ref)")
    print(f"Bootstrap assets: {_status_label(status['bootstrap']['installed'])}")
    print(f"Gene maps: {_status_label(status['geneMap']['installed'])}")
    print(f"Microarray targets: {_path_status(status['annotationVcf'])}")
    print(f"Test genome: {status['testGenome']['status']}")
    print()
    print(f"{'Annotation data':<16}{'hg19':<14}hg38")
    labels = {key: label for key, label, _exts, _builds in ANNOTATION_DATA}
    for key, builds in status["annotations"].items():
        cells = [
            _annotation_label(builds[build]) if build in builds else "-"
            for build in ("hg19", "hg38")
        ]
        print(f"  {labels[key]:<14}{cells[0]:<14}{cells[1]}")
    vep = status["vepCache"]
    versions = ", ".join(vep["versions"]) or "missing"
    print(f"\nVEP cache: {versions}" + (f" ({vep['path']})" if vep["versions"] else ""))
    pharmcat = status["pharmcat"]
    print(
        f"PharmCAT: {pharmcat['version']} ({pharmcat['path']})"
        if pharmcat["installed"]
        else "PharmCAT: missing"
    )
    print("\nGenomes:" if status["genomes"] else "\nGenomes: none")
    width = max((len(genome["name"]) for genome in status["genomes"]), default=0) + 2
    for genome in status["genomes"]:
        ready = (
            "aligner index (bwa)"
            if genome["bwaIndex"]
            else "FASTA index"
            if genome["faiIndex"]
            else "not indexed"
        )
        print(f"  {genome['name']:<{width}}{ready}")


def _annotation_label(entry: dict[str, Any]) -> str:
    if entry["installed"]:
        return "installed"
    return "not indexed" if entry["file"] else "missing"


def _annotation_status(reflib: str) -> dict[str, dict[str, dict[str, Any]]]:
    status: dict[str, dict[str, dict[str, Any]]] = {}
    for key, _label, extensions, builds in ANNOTATION_DATA:
        status[key] = {}
        for build in builds:
            path = first_existing_file(
                [
                    os.path.join(directory, f"{key}_{build}{extension}")
                    for directory in (os.path.join(reflib, "ref"), reflib)
                    for extension in extensions
                ]
            )
            indexed = bool(path) and any(
                os.path.isfile(path + index) for index in (".tbi", ".csi")
            )
            status[key][build] = {"file": path, "installed": indexed}
    return status


def _vep_cache_status(lib: ReferenceLibrary, reflib: str) -> dict[str, Any]:
    path = lib.vep_cache or os.path.join(reflib, "vep")
    species = os.path.join(path, "homo_sapiens")
    versions = (
        sorted(
            name
            for name in os.listdir(species)
            if os.path.isdir(os.path.join(species, name))
        )
        if os.path.isdir(species)
        else []
    )
    return {"path": path, "versions": versions}


def _pharmcat_status(reflib: str) -> dict[str, Any]:
    from wgsextract_cli.commands.vcf.pharmcat import PIPELINE_FILES

    path = str(settings.get("pharmcat_directory") or os.path.join(reflib, "pharmcat"))
    installed = all(os.path.isfile(os.path.join(path, n)) for n in PIPELINE_FILES)
    version = ""
    if installed:
        common = os.path.join(path, "pcat", "common.py")
        with contextlib.suppress(OSError):
            with open(common) as f:
                match = re.search(r"PHARMCAT_VERSION = '([^']+)'", f.read())
            version = match.group(1) if match else ""
    return {"installed": installed, "path": path, "version": version or "installed"}


def _genome_status(reflib: str) -> list[dict[str, Any]]:
    genomes_dir = os.path.join(reflib, "genomes")
    if not os.path.isdir(genomes_dir):
        return []
    genomes = []
    for name in sorted(os.listdir(genomes_dir)):
        path = os.path.join(genomes_dir, name)
        if not is_fasta_path(name) or not os.path.isfile(path):
            continue
        genomes.append(
            {
                "name": name,
                "path": path,
                "faiIndex": os.path.isfile(path + ".fai"),
                "bwaIndex": all(
                    os.path.isfile(path + ext)
                    for ext in (".amb", ".ann", ".bwt", ".pac", ".sa")
                ),
            }
        )
    return genomes


def _resolve_reference_library(args: argparse.Namespace) -> str:
    configured_reflib = settings.get("reference_library")
    ref = getattr(args, "ref", None)
    explicit_dests = getattr(args, "_explicit_dests", None)
    ref_is_explicit = explicit_dests is None or "ref" in explicit_dests
    if ref and ref_is_explicit:
        ref_path = os.path.abspath(str(ref))
        if os.path.isdir(ref_path):
            return ref_path
        if os.path.isfile(ref_path):
            return _reference_library_from_fasta(ref_path)
    if configured_reflib:
        return os.path.abspath(str(configured_reflib))
    if ref:
        ref_path = os.path.abspath(str(ref))
        if os.path.isdir(ref_path):
            return ref_path
        if os.path.isfile(ref_path):
            return _reference_library_from_fasta(ref_path)
    return default_reference_library()


def _reference_library_from_fasta(ref_path: str) -> str:
    ref_dir = os.path.dirname(ref_path)
    if os.path.basename(ref_dir).lower() == "genomes":
        return os.path.dirname(ref_dir)
    return ref_dir


def _resolve_genome_library(args: argparse.Namespace, reflib: str) -> str:
    genome_library = getattr(args, "genome_library", None) or settings.get(
        "genome_library"
    )
    if genome_library:
        return os.path.abspath(str(genome_library))
    return os.path.join(os.path.dirname(reflib), "genomes")


def _ploidy_status(reflib: str) -> dict[str, Any]:
    files = {
        "hg19": first_existing_file(
            [
                os.path.join(reflib, "ploidy_hg19.txt"),
                os.path.join(reflib, "ref", "ploidy_hg19.txt"),
                os.path.join(reflib, "microarray", "ploidy_hg19.txt"),
            ]
        ),
        "hg38": first_existing_file(
            [
                os.path.join(reflib, "ploidy_hg38.txt"),
                os.path.join(reflib, "ref", "ploidy_hg38.txt"),
                os.path.join(reflib, "microarray", "ploidy_hg38.txt"),
            ]
        ),
    }
    return {
        "installed": all(files.values()),
        "files": files,
    }


def _mappability_map_status(reflib: str) -> dict[str, Any]:
    from wgsextract_cli.core.constants import MAPPABILITY_MAP_FILES

    maps_dir = os.path.join(reflib, "maps")
    files = {
        file_name: os.path.join(maps_dir, file_name)
        for file_name in MAPPABILITY_MAP_FILES
    }
    return {
        "installed": all(os.path.isfile(path) for path in files.values()),
        "files": files,
    }


def _test_genome_status(genome_library: str) -> dict[str, Any]:
    path = os.path.join(genome_library, "wgsextract-benchmark-hg19-mini")
    partial = os.path.join(
        genome_library, ".downloads", "wgsextract-benchmark-hg19-mini.zip.partial"
    )
    installed = os.path.isdir(path) and os.path.isfile(
        os.path.join(path, "genome-config.toml")
    )
    if installed:
        status = "installed"
    elif os.path.isfile(partial):
        status = "incomplete"
    else:
        status = get_genome_status("wgsextract-benchmark-hg19-mini", genome_library)
    return {
        "installed": installed,
        "status": status,
        "path": path,
    }


def _path_status(status: dict[str, Any]) -> str:
    if status["installed"]:
        return f"installed ({status['file']})"
    return "missing"


def _status_label(installed: bool) -> str:
    return "installed" if installed else "missing"


def _json_bool(value: bool) -> str:
    return "true" if value else "false"
