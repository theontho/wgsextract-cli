import argparse
import logging
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

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
from wgsextract_cli.core.utils import WGSExtractError

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


def cmd_aadr(args: argparse.Namespace) -> None:
    installed = install_aadr_2m(resolve_reference_library(args.ref))
    logging.info("Installed CC0 AADR v66.p1 2M SNP metadata: %s", installed)


def cmd_ybrowse(args: argparse.Namespace) -> None:
    from wgsextract_cli.core.ybrowse import install_snapshot

    verify_dependencies(["bgzip", "tabix"])
    library = Path(resolve_reference_library(args.ref))
    installed = install_snapshot(library, args.snapshot)
    logging.info("Installed sites-only GRCh38 yBrowse resource: %s", installed)


def cmd_library(args: argparse.Namespace) -> None:
    """Interactive or non-interactive library manager."""
    if args.list:
        return cmd_library_list(args)

    deps = ["curl", "samtools", "bcftools", "tabix", "bgzip", "htsfile"]
    verify_dependencies(deps)
    log_dependency_info(deps)

    genomes = get_available_genomes()
    reflib_dir = resolve_reference_library(args.ref)

    # Non-interactive install
    if args.install:
        choice = args.install.upper()
        # A code names the genome on every mirror that hosts it, in catalog order.
        targets = [g for g in genomes if g["code"].upper() == choice]
        if not targets and choice.isdigit() and 0 < int(choice) <= len(genomes):
            targets = [genomes[int(choice) - 1]]
        if not targets:
            raise WGSExtractError(f"Genome '{args.install}' not found in library.")
        _install_from_mirrors(targets, reflib_dir)
        return

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
        if download_and_process_genome(genome, reflib_dir, restart=i > 0):
            return
    tried = f" from any of its {len(mirrors)} mirrors" if len(mirrors) > 1 else ""
    raise WGSExtractError(f"Failed to install genome {mirrors[0]['final']}{tried}.")


def _install_genome(
    genome: Mapping[str, str], reflib_dir: str, restart: bool = False
) -> None:
    Path(reflib_dir).mkdir(parents=True, exist_ok=True)
    if not download_and_process_genome(genome, reflib_dir, restart=restart):
        raise WGSExtractError(f"Failed to install genome {genome['final']}.")


def cmd_gene_map(args: argparse.Namespace) -> None:
    reflib = resolve_reference_library(args.ref)
    if getattr(args, "delete", False):
        _delete_gene_maps(reflib)
    else:
        _download_gene_maps(reflib)


def _download_gene_maps(reflib: str) -> None:
    if not download_gene_maps(reflib):
        raise WGSExtractError(LOG_MESSAGES["dl_genemap_failed"])
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
    if not downloader(reflib):
        raise WGSExtractError(f"{resource_name} setup failed.")
    logging.info("%s setup complete.", resource_name)


def _run_ref_download(
    args: argparse.Namespace,
    resource_name: str,
    downloader: Callable[[str], bool],
    action: str = "download and indexing",
) -> None:
    _run_downloader(
        resolve_reference_library(args.ref), resource_name, downloader, action
    )


def cmd_clinvar_dl(args: argparse.Namespace) -> None:
    _run_ref_download(args, "ClinVar", download_clinvar)


def cmd_revel_dl(args: argparse.Namespace) -> None:
    _run_ref_download(args, "REVEL", download_revel)


def cmd_phylop_dl(args: argparse.Namespace) -> None:
    _run_ref_download(
        args,
        f"PhyloP {args.build}",
        lambda lib: download_phylop(lib, args.build),
        "download",
    )


def cmd_gnomad_dl(args: argparse.Namespace) -> None:
    if getattr(args, "release", "2.1.1") == "4.1":
        workers = int(args.threads) if getattr(args, "threads", None) else 6
        _run_ref_download(
            args,
            "gnomAD v4.1",
            lambda lib: build_gnomad_af(lib, workers),
            "build",
        )
        return
    _run_ref_download(args, "gnomAD", download_gnomad)


def cmd_spliceai_dl(args: argparse.Namespace) -> None:
    _run_ref_download(args, "SpliceAI", download_spliceai)


def cmd_alphamissense_dl(args: argparse.Namespace) -> None:
    _run_ref_download(args, "AlphaMissense", download_alphamissense)


def cmd_pharmgkb_dl(args: argparse.Namespace) -> None:
    _run_ref_download(args, "PharmGKB", download_pharmgkb, "download")


def cmd_cadd_dl(args: argparse.Namespace) -> None:
    _run_ref_download(
        args,
        f"CADD {args.build}",
        lambda lib: download_cadd(lib, args.build),
        "download",
    )


def cmd_gerp_dl(args: argparse.Namespace) -> None:
    _run_ref_download(
        args,
        f"GERP {args.build}",
        lambda lib: download_gerp(lib, args.build),
        "download",
    )


def cmd_bootstrap(args: argparse.Namespace) -> None:
    from wgsextract_cli.core.config import save_config, settings
    from wgsextract_cli.core.reference_processing import download_bootstrap

    should_save_reflib = not settings.get("reference_library")
    reflib = resolve_reference_library(args.ref)

    logging.info("Starting reference library bootstrap...")
    if not download_bootstrap(reflib):
        raise WGSExtractError("Reference library bootstrap failed.")
    if should_save_reflib:
        save_config({"reference_library": reflib})
        logging.info(f"Saved reference library path to config.toml: {reflib}")
    logging.info(
        "Bootstrap complete. You can now install genomes via 'wgsextract ref library'."
    )
