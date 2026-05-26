import argparse
import json
import os
from typing import Any

from wgsextract_cli.core.config import settings
from wgsextract_cli.core.gene_map import are_gene_maps_installed
from wgsextract_cli.core.ref_library import get_genome_status
from wgsextract_cli.core.reference_assets import (
    build_hint,
    find_annotation_resource,
    find_annotation_vcf,
    first_existing_file,
)
from wgsextract_cli.core.reference_processing import bootstrap_has_support_assets
from wgsextract_cli.core.reference_resolver import ReferenceLibrary


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
        "library.testGenomeInstalled": _json_bool(status["testGenome"]["installed"]),
        "library.testGenomeStatus": status["testGenome"]["status"],
        "library.testGenomePath": status["testGenome"]["path"],
    }


def print_ref_status(status: dict[str, Any]) -> None:
    print(f"Reference library: {status['referenceLibrary']['path']}")
    print(f"Bootstrap assets: {_status_label(status['bootstrap']['installed'])}")
    print(f"Gene maps: {_status_label(status['geneMap']['installed'])}")
    print(f"Annotation VCF: {_path_status(status['annotationVcf'])}")
    print(f"SpliceAI: {_path_status(status['spliceai'])}")
    print(f"AlphaMissense: {_path_status(status['alphamissense'])}")
    print(f"PharmGKB: {_path_status(status['pharmgkb'])}")
    print(f"Test genome: {status['testGenome']['status']}")


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
    prog_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))
    return os.path.join(prog_root, "reference")


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
