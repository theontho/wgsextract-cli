import argparse
import logging
from collections.abc import Callable, Mapping, Sequence
from os import fspath
from pathlib import Path

from wgsextract_cli.api._cli import cli_handler
from wgsextract_cli.api.ref import (
    RefAadrParams,
    RefAlphamissenseParams,
    RefBootstrapParams,
    RefCaddParams,
    RefClinvarParams,
    RefGeneMapParams,
    RefGerpParams,
    RefGnomadParams,
    RefLibraryParams,
    RefPharmgkbParams,
    RefPhylopParams,
    RefRevelParams,
    RefSpliceaiParams,
    RefYbrowseParams,
    _Resource,
)
from wgsextract_cli.core.aadr_resources import install_aadr_2m
from wgsextract_cli.core.annotation_resources import (
    build_gnomad_af,
    download_alphamissense,
    download_cadd,
    download_clinvar,
    download_gerp,
    download_pharmgkb,
    download_phylop,
    download_revel,
    download_spliceai,
)
from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.gene_map import (
    are_gene_maps_installed,
    delete_gene_maps,
    download_gene_maps,
)
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.ref_library.catalog import (
    get_available_genomes,
    get_genome_status,
)
from wgsextract_cli.core.reference_assets import resolve_reference_library
from wgsextract_cli.core.reference_processing import (
    delete_genome,
    download_and_process_genome,
    download_gnomad,
)
from wgsextract_cli.core.results import (
    MANIFEST_SUFFIX,
    current_result,
    file_snapshot,
    manifest_path_for,
    record_changed_files,
)
from wgsextract_cli.core.utils import UsageError, WGSExtractError

from .core_commands import (
    cmd_library_list,
)

_MENU_DOWNLOADS: dict[str, tuple[str, Callable[[str], bool]]] = {
    "C": ("ClinVar", download_clinvar),
    "R": ("REVEL", download_revel),
    "P": ("PhyloP hg38 (~9.9 GB)", download_phylop),
    "N": ("gnomAD", download_gnomad),
    "S": ("SpliceAI", download_spliceai),
    "A": ("AlphaMissense", download_alphamissense),
    "K": ("PharmGKB", download_pharmgkb),
    "D": ("CADD hg38 (~88 GB)", download_cadd),
    "E": ("GERP hg38 (~9.6 GB)", download_gerp),
    "F": ("gnomAD v4.1 frequencies hg38 (streams ~900 GB)", build_gnomad_af),
}


def _library(ref: Path | None) -> str:
    return resolve_reference_library(fspath(ref) if ref else None)


def run_aadr(params: RefAadrParams) -> None:
    installed = install_aadr_2m(_library(params.ref))
    current_result().add_output(installed, "reference_resource")
    logging.info("Installed CC0 AADR v66.p1 2M SNP metadata: %s", installed)


def run_ybrowse(params: RefYbrowseParams) -> None:
    from wgsextract_cli.core.ybrowse import install_snapshot

    verify_dependencies(["bgzip", "tabix"])
    library = Path(_library(params.ref))
    installed = install_snapshot(library, params.snapshot)
    _record_installed(installed)
    logging.info("Installed sites-only GRCh38 yBrowse resource: %s", installed)


def _record_installed(path: Path) -> None:
    """Record an installed file, or every file of an installed directory."""
    if not path.is_dir():
        current_result().add_output(path, "reference_resource")
        return
    record_changed_files(path, {}, "reference_resource")
    current_result().set_manifest_stem(path)


def _record_genome(final_path: str, before: dict[str, tuple[int, int]]) -> None:
    """The installed FASTA and the indexes and sidecars written beside it."""
    record_changed_files(Path(final_path).parent, before, "reference_sidecar")
    result = current_result()
    result.add_output(final_path, "reference")
    result.set_reference(final_path)
    result.set_manifest_stem(
        str(manifest_path_for(final_path)).removesuffix(MANIFEST_SUFFIX)
    )


_LIBRARY_DEPS = ["curl", "samtools", "bcftools", "tabix", "bgzip", "htsfile"]


def run_library(params: RefLibraryParams) -> None:
    """Install ``params.install`` from the first of its mirrors that works."""
    verify_dependencies(_LIBRARY_DEPS)
    log_dependency_info(_LIBRARY_DEPS)
    if not params.install:
        raise UsageError("--install is required.")
    genomes = get_available_genomes()
    reflib_dir = _library(params.ref)
    choice = params.install.upper()
    # A code names the genome on every mirror that hosts it, in catalog order.
    targets = [g for g in genomes if g["code"].upper() == choice]
    if not targets and choice.isdigit() and 0 < int(choice) <= len(genomes):
        targets = [genomes[int(choice) - 1]]
    if not targets:
        raise WGSExtractError(f"Genome '{params.install}' not found in library.")
    _install_from_mirrors(targets, reflib_dir)


def cmd_library(args: argparse.Namespace) -> None:
    """Interactive or non-interactive library manager."""
    if args.list:
        return cmd_library_list(args)
    if args.install:
        return _cli_library(args)

    verify_dependencies(_LIBRARY_DEPS)
    log_dependency_info(_LIBRARY_DEPS)
    genomes = get_available_genomes()
    reflib_dir = resolve_reference_library(args.ref)

    # Interactive menu
    print("\n" + "=" * 80)
    print("WGS Extract Reference Library Manager")
    print(f"Library Path: {reflib_dir}")
    print("=" * 80)
    print("Select an option:")
    print(" 0) Exit")
    print(" G) Gene Map (hg19/hg38)")
    for key, (name, _) in _MENU_DOWNLOADS.items():
        suffix = "" if key == "K" or "hg38" in name else " (hg19/hg38)"
        print(f" {key}) {name}{suffix}")

    for i, g in enumerate(genomes, 1):
        s = get_genome_status(g["final"], reflib_dir)
        status = {"installed": " [Installed]", "incomplete": " [Incomplete]"}.get(s, "")
        print(f" {i:2}) {g['label']}{status}")
    print("=" * 80)

    try:
        choice = input("\nEnter choice (number or G): ").strip().upper()
        if not choice or choice == "0":
            print("Exiting library manager.")
            return

        if choice == "G":
            if are_gene_maps_installed(reflib_dir):
                confirm = input("Gene maps already installed. Delete? (y/n): ").lower()
                if confirm == "y":
                    _delete_gene_maps(reflib_dir)
            else:
                _download_gene_maps(reflib_dir)
            return

        if choice in _MENU_DOWNLOADS:
            name, downloader = _MENU_DOWNLOADS[choice]
            _run_downloader(reflib_dir, name, downloader)
            return

        idx = int(choice) - 1
        if not 0 <= idx < len(genomes):
            print("Invalid choice.")
            return
        genome = genomes[idx]
        status = get_genome_status(genome["final"], reflib_dir)
        restart = False
        if status == "installed":
            print(f"\n{genome['final']} is already installed.")
            if input("Re-download anyway? [y/N]: ").strip().lower() != "y":
                return
            restart = True
        elif status == "incomplete":
            print(f"\n{genome['final']} is incomplete.")
            action = input("[R]esume, [D]elete, or [C]ancel? ").strip().lower()
            if action == "c":
                return
            if action == "d":
                delete_genome(genome["final"], reflib_dir)
                return
        _install_genome(genome, reflib_dir, restart=restart)
    except (ValueError, EOFError, KeyboardInterrupt):
        print("\nExiting library manager.")


def _install_from_mirrors(
    mirrors: Sequence[Mapping[str, str]], reflib_dir: str
) -> None:
    """Install from the first mirror that works. A later mirror starts afresh:
    its file may differ (NIH's hs38DH is uncompressed), so no partial is resumed."""
    for i, genome in enumerate(mirrors):
        if i:
            logging.warning(
                f"Trying the next mirror: {genome.get('description', genome['url'])}"
            )
        Path(reflib_dir).mkdir(parents=True, exist_ok=True)
        before = file_snapshot(Path(reflib_dir, "genomes"))
        if download_and_process_genome(genome, reflib_dir, restart=i > 0):
            _record_genome(str(Path(reflib_dir, "genomes", genome["final"])), before)
            return
    tried = f" from any of its {len(mirrors)} mirrors" if len(mirrors) > 1 else ""
    raise WGSExtractError(f"Failed to install genome {mirrors[0]['final']}{tried}.")


def _install_genome(
    genome: Mapping[str, str], reflib_dir: str, restart: bool = False
) -> None:
    Path(reflib_dir).mkdir(parents=True, exist_ok=True)
    before = file_snapshot(Path(reflib_dir, "genomes"))
    if not download_and_process_genome(genome, reflib_dir, restart=restart):
        raise WGSExtractError(f"Failed to install genome {genome['final']}.")
    _record_genome(str(Path(reflib_dir, "genomes", genome["final"])), before)


def run_gene_map(params: RefGeneMapParams) -> None:
    reflib = _library(params.ref)
    if params.delete:
        _delete_gene_maps(reflib)
    else:
        _download_gene_maps(reflib)


def _download_gene_maps(reflib: str) -> None:
    before = file_snapshot(reflib)
    if not download_gene_maps(reflib):
        raise WGSExtractError(LOG_MESSAGES["dl_genemap_failed"])
    record_changed_files(reflib, before, "reference_resource")
    print(LOG_MESSAGES["dl_genemap_success"])


def _delete_gene_maps(reflib: str) -> None:
    if not delete_gene_maps(reflib):
        raise WGSExtractError(LOG_MESSAGES["del_genemap_failed"])
    print(LOG_MESSAGES["del_genemap_success"])


def _run_downloader(
    reflib: str,
    resource_name: str,
    downloader: Callable[[str], bool],
    action: str = "download and indexing",
) -> None:
    if not Path(reflib).is_dir():
        logging.info("Creating a new reference library at %s.", reflib)
    logging.info("Starting %s %s...", resource_name, action)
    before = file_snapshot(reflib)
    if not downloader(reflib):
        raise WGSExtractError(f"{resource_name} setup failed.")
    record_changed_files(reflib, before, "reference_resource")
    logging.info("%s setup complete.", resource_name)


def run_clinvar(params: RefClinvarParams) -> None:
    _run_downloader(_library(params.ref), "ClinVar", download_clinvar)


def run_revel(params: RefRevelParams) -> None:
    _run_downloader(_library(params.ref), "REVEL", download_revel)


def run_phylop(params: RefPhylopParams) -> None:
    build = params.build
    _run_downloader(
        _library(params.ref),
        f"PhyloP {build}",
        lambda lib: download_phylop(lib, build),
        "download",
    )


def run_gnomad(params: RefGnomadParams) -> None:
    reflib = _library(params.ref)
    if params.release == "4.1":
        workers = int(params.threads) if params.threads else 6
        _run_downloader(
            reflib, "gnomAD v4.1", lambda lib: build_gnomad_af(lib, workers), "build"
        )
    else:
        _run_downloader(reflib, "gnomAD", download_gnomad)


def run_spliceai(params: RefSpliceaiParams) -> None:
    _run_downloader(_library(params.ref), "SpliceAI", download_spliceai)


def run_alphamissense(params: RefAlphamissenseParams) -> None:
    _run_downloader(_library(params.ref), "AlphaMissense", download_alphamissense)


def run_pharmgkb(params: RefPharmgkbParams) -> None:
    _run_downloader(_library(params.ref), "PharmGKB", download_pharmgkb, "download")


def run_cadd(params: RefCaddParams) -> None:
    build = params.build
    _run_downloader(
        _library(params.ref),
        f"CADD {build}",
        lambda lib: download_cadd(lib, build),
        "download",
    )


def run_gerp(params: RefGerpParams) -> None:
    build = params.build
    _run_downloader(
        _library(params.ref),
        f"GERP {build}",
        lambda lib: download_gerp(lib, build),
        "download",
    )


def run_bootstrap(params: RefBootstrapParams) -> None:
    from wgsextract_cli.core.config import save_config, settings
    from wgsextract_cli.core.reference_processing import download_bootstrap

    should_save_reflib = not settings.get("reference_library")
    reflib = _library(params.ref)

    logging.info("Starting reference library bootstrap...")
    before = file_snapshot(reflib)
    if not download_bootstrap(reflib):
        raise WGSExtractError("Reference library bootstrap failed.")
    record_changed_files(reflib, before, "reference_resource")
    current_result().set_manifest_stem(Path(reflib, "bootstrap"))
    if should_save_reflib:
        save_config({"reference_library": reflib})
        logging.info(f"Saved reference library path to config.toml: {reflib}")
    logging.info(
        "Bootstrap complete. You can now install genomes via 'wgsextract ref library'."
    )


cmd_aadr = cli_handler(RefAadrParams, run_aadr)
cmd_ybrowse = cli_handler(RefYbrowseParams, run_ybrowse)
_cli_library = cli_handler(RefLibraryParams, run_library)
cmd_gene_map = cli_handler(RefGeneMapParams, run_gene_map)
cmd_bootstrap = cli_handler(RefBootstrapParams, run_bootstrap)
RESOURCE_COMMANDS: tuple[tuple[type[_Resource], Callable[..., None]], ...] = (
    (RefClinvarParams, run_clinvar),
    (RefRevelParams, run_revel),
    (RefPhylopParams, run_phylop),
    (RefGnomadParams, run_gnomad),
    (RefSpliceaiParams, run_spliceai),
    (RefAlphamissenseParams, run_alphamissense),
    (RefPharmgkbParams, run_pharmgkb),
    (RefCaddParams, run_cadd),
    (RefGerpParams, run_gerp),
)
