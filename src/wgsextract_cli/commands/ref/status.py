import argparse
import contextlib
import json
import os
import re
from pathlib import Path
from typing import Any

from wgsextract_cli.core.config import settings
from wgsextract_cli.core.gene_map import are_gene_maps_installed
from wgsextract_cli.core.paths import absolute_path
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
# (key, label, file extensions, builds): <key>_<build><ext>, tabix-indexed
# (bigWig files are self-indexed).
ANNOTATION_DATA = [
    ("clinvar", "ClinVar", (".vcf.gz",), ("hg19", "hg38")),
    ("gnomad", "gnomAD", (".tsv.gz", ".vcf.bgz", ".vcf.gz"), ("hg19", "hg38")),
    ("revel", "REVEL", (".tsv.gz",), ("hg19", "hg38")),
    ("phylop", "PhyloP", (".tsv.gz",), ("hg19", "hg38")),
    ("cadd", "CADD", (".tsv.gz",), ("hg19", "hg38")),
    ("gerp", "GERP", (".bw",), ("hg19", "hg38")),
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
            "exists": Path(reflib).is_dir(),
        },
        "genomeLibrary": {
            "path": genome_library,
            "exists": Path(genome_library).is_dir(),
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
                    str(Path(directory, f"{key}_{build}{extension}"))
                    for directory in (str(Path(reflib, "ref")), reflib)
                    for extension in extensions
                ]
            )
            # bigWig files carry their own index; the rest need a tabix sidecar.
            indexed = bool(path) and (
                path.endswith(".bw")
                or any(Path(path + index).is_file() for index in (".tbi", ".csi"))
            )
            status[key][build] = {"file": path, "installed": indexed}
    return status


def _vep_cache_status(lib: ReferenceLibrary, reflib: str) -> dict[str, Any]:
    path = lib.vep_cache or str(Path(reflib, "vep"))
    species = str(Path(path, "homo_sapiens"))
    versions = (
        sorted(name for name in os.listdir(species) if Path(species, name).is_dir())
        if Path(species).is_dir()
        else []
    )
    return {"path": path, "versions": versions}


def _pharmcat_status(reflib: str) -> dict[str, Any]:
    from wgsextract_cli.commands.vcf.pharmcat import PIPELINE_FILES

    path = str(settings.get("pharmcat_directory") or str(Path(reflib, "pharmcat")))
    installed = all(Path(path, n).is_file() for n in PIPELINE_FILES)
    version = ""
    if installed:
        common = str(Path(path, "pcat", "common.py"))
        with contextlib.suppress(OSError):
            with open(common) as f:
                match = re.search(r"PHARMCAT_VERSION = '([^']+)'", f.read())
            version = match.group(1) if match else ""
    return {"installed": installed, "path": path, "version": version or "installed"}


def _genome_status(reflib: str) -> list[dict[str, Any]]:
    genomes_dir = str(Path(reflib, "genomes"))
    if not Path(genomes_dir).is_dir():
        return []
    genomes = []
    for name in sorted(os.listdir(genomes_dir)):
        path = str(Path(genomes_dir, name))
        if not is_fasta_path(name) or not Path(path).is_file():
            continue
        genomes.append(
            {
                "name": name,
                "path": path,
                "faiIndex": Path(path + ".fai").is_file(),
                "bwaIndex": all(
                    Path(path + ext).is_file()
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
        ref_path = str(absolute_path(str(ref)))
        if Path(ref_path).is_dir():
            return ref_path
        if Path(ref_path).is_file():
            return _reference_library_from_fasta(ref_path)
    if configured_reflib:
        return str(absolute_path(str(configured_reflib)))
    if ref:
        ref_path = str(absolute_path(str(ref)))
        if Path(ref_path).is_dir():
            return ref_path
        if Path(ref_path).is_file():
            return _reference_library_from_fasta(ref_path)
    return default_reference_library()


def _reference_library_from_fasta(ref_path: str) -> str:
    ref_dir = str(Path(ref_path).parent)
    if Path(ref_dir).name.lower() == "genomes":
        return str(Path(ref_dir).parent)
    return ref_dir


def _resolve_genome_library(args: argparse.Namespace, reflib: str) -> str:
    genome_library = getattr(args, "genome_library", None) or settings.get(
        "genome_library"
    )
    if genome_library:
        return str(absolute_path(str(genome_library)))
    return str(Path(reflib).parent / "genomes")


def _ploidy_status(reflib: str) -> dict[str, Any]:
    files = {
        "hg19": first_existing_file(
            [
                str(Path(reflib, "ploidy_hg19.txt")),
                str(Path(reflib, "ref", "ploidy_hg19.txt")),
                str(Path(reflib, "microarray", "ploidy_hg19.txt")),
            ]
        ),
        "hg38": first_existing_file(
            [
                str(Path(reflib, "ploidy_hg38.txt")),
                str(Path(reflib, "ref", "ploidy_hg38.txt")),
                str(Path(reflib, "microarray", "ploidy_hg38.txt")),
            ]
        ),
    }
    return {
        "installed": all(files.values()),
        "files": files,
    }


def _mappability_map_status(reflib: str) -> dict[str, Any]:
    from wgsextract_cli.core.constants import MAPPABILITY_MAP_FILES

    maps_dir = str(Path(reflib, "maps"))
    files = {
        file_name: str(Path(maps_dir, file_name)) for file_name in MAPPABILITY_MAP_FILES
    }
    return {
        "installed": all(Path(path).is_file() for path in files.values()),
        "files": files,
    }


def _test_genome_status(genome_library: str) -> dict[str, Any]:
    path = str(Path(genome_library, "wgsextract-benchmark-hg19-mini"))
    partial = str(
        Path(genome_library, ".downloads", "wgsextract-benchmark-hg19-mini.zip.partial")
    )
    installed = Path(path).is_dir() and Path(path, "genome-config.toml").is_file()
    if installed:
        status = "installed"
    elif Path(partial).is_file():
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
