import logging
import os
from collections.abc import Callable

from wgsextract_cli.core.annotation_resources import (
    download_alphamissense,
    download_clinvar,
    download_pharmgkb,
    download_revel,
    download_spliceai,
)
from wgsextract_cli.core.dependency_checks import (
    log_dependency_info,
    verify_dependencies,
)
from wgsextract_cli.core.messages import LOG_MESSAGES
from wgsextract_cli.core.ref_library import (
    get_available_genomes,
    get_genome_status,
)
from wgsextract_cli.core.reference_processing import (
    download_and_process_genome,
    download_gnomad,
    download_phylop,
)
from wgsextract_cli.core.utils import WGSExtractError

from ._ref_core_commands import (
    cmd_library_list,
)


def cmd_library(args):
    """Interactive or non-interactive library manager."""
    from wgsextract_cli.core.config import settings

    if args.list:
        return cmd_library_list(args)

    deps = ["curl", "samtools", "bcftools", "tabix", "bgzip", "htsfile"]
    verify_dependencies(deps)
    log_dependency_info(deps)

    genomes = get_available_genomes()

    # Determine reference library directory
    reflib_dir = args.ref
    if not reflib_dir:
        reflib_dir = settings.get("reference_library")
    if not reflib_dir:
        prog_root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "../../../..")
        )
        reflib_dir = os.path.join(prog_root, "reference")

    reflib_dir = os.path.abspath(reflib_dir)

    # Non-interactive install
    if args.install:
        choice = args.install.upper()
        target_idx = -1
        target_genome = None

        # Try to find by code first
        for i, g in enumerate(genomes):
            if g["code"].upper() == choice:
                target_idx = i
                target_genome = g
                break

        # Then try by index
        if target_idx == -1 and choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(genomes):
                target_idx = idx
                target_genome = genomes[idx]

        if target_genome:
            download_and_process_genome(target_genome, reflib_dir, interactive=False)
            return
        else:
            logging.error(f"Genome '{args.install}' not found in library.")
            raise WGSExtractError("Ref library installation failed.")

    # Interactive menu
    print("\n" + "=" * 80)
    print("WGS Extract Reference Library Manager")
    print(f"Library Path: {reflib_dir}")
    print("=" * 80)
    print("Select an option:")
    print(" 0) Exit")
    print(" G) Gene Map (hg19/hg38)")
    print(" C) ClinVar (hg19/hg38)")
    print(" R) REVEL (hg19/hg38)")
    print(" P) PhyloP (hg19/hg38)")
    print(" N) gnomAD (hg19/hg38)")
    print(" S) SpliceAI (hg19/hg38)")
    print(" A) AlphaMissense (hg19/hg38)")
    print(" K) PharmGKB")

    # Check for installed genomes
    for i, g in enumerate(genomes, 1):
        s = get_genome_status(g["final"], reflib_dir)
        status = ""
        if s == "installed":
            status = " [Installed]"
        elif s == "incomplete":
            status = " [Incomplete]"
        print(f" {i:2}) {g['label']}{status}")
    print("=" * 80)

    try:
        choice = input("\nEnter choice (number or G): ").strip().upper()
        if not choice or choice == "0":
            print("Exiting library manager.")
            return

        if choice == "G":
            from wgsextract_cli.core.gene_map import (
                are_gene_maps_installed,
                delete_gene_maps,
                download_gene_maps,
            )

            if are_gene_maps_installed(reflib_dir):
                confirm = input("Gene maps already installed. Delete? (y/n): ").lower()
                if confirm == "y":
                    if delete_gene_maps(reflib_dir):
                        print("Gene maps deleted.")
                    else:
                        print("Deletion failed.")
            else:
                download_gene_maps(reflib_dir)
            return

        if choice == "C":
            from wgsextract_cli.core.annotation_resources import download_clinvar

            download_clinvar(reflib_dir)
            return

        if choice == "R":
            from wgsextract_cli.core.annotation_resources import download_revel

            download_revel(reflib_dir)
            return

        if choice == "P":
            from wgsextract_cli.core.reference_processing import download_phylop

            download_phylop(reflib_dir)
            return

        if choice == "N":
            from wgsextract_cli.core.reference_processing import download_gnomad

            download_gnomad(reflib_dir)
            return

        if choice == "S":
            from wgsextract_cli.core.annotation_resources import download_spliceai

            download_spliceai(reflib_dir)
            return

        if choice == "A":
            from wgsextract_cli.core.annotation_resources import download_alphamissense

            download_alphamissense(reflib_dir)
            return

        if choice == "K":
            from wgsextract_cli.core.annotation_resources import download_pharmgkb

            download_pharmgkb(reflib_dir)
            return

        idx = int(choice) - 1
        if 0 <= idx < len(genomes):
            if not os.path.exists(reflib_dir):
                os.makedirs(reflib_dir, exist_ok=True)

            download_and_process_genome(genomes[idx], reflib_dir)
        else:
            print("Invalid choice.")
    except (ValueError, EOFError, KeyboardInterrupt):
        print("\nExiting library manager.")


def cmd_gene_map(args):
    from wgsextract_cli.core.gene_map import delete_gene_maps, download_gene_maps

    prog_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../.."))
    reflib = args.ref if args.ref else os.path.join(prog_root, "reference")

    if getattr(args, "delete", False):
        if delete_gene_maps(reflib):
            print(LOG_MESSAGES["del_genemap_success"])
        else:
            print(LOG_MESSAGES["del_genemap_failed"])
    else:
        if download_gene_maps(reflib):
            print(LOG_MESSAGES["dl_genemap_success"])
        else:
            print(LOG_MESSAGES["dl_genemap_failed"])


def _resolve_reflib(args) -> str:
    from wgsextract_cli.core.config import settings

    prog_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../.."))
    return (
        args.ref
        or settings.get("reference_library")
        or os.path.join(prog_root, "reference")
    )


def _run_ref_download(
    args,
    resource_name: str,
    downloader: Callable[[str], bool],
    action: str = "download and indexing",
) -> None:
    reflib = _resolve_reflib(args)
    logging.info("Starting %s %s...", resource_name, action)
    if downloader(reflib):
        logging.info("%s setup complete.", resource_name)
        return
    logging.error("%s setup failed.", resource_name)
    raise WGSExtractError("Ref library installation failed.")


def cmd_clinvar_dl(args):
    _run_ref_download(args, "ClinVar", download_clinvar)


def cmd_revel_dl(args):
    _run_ref_download(args, "REVEL", download_revel)


def cmd_phylop_dl(args):
    _run_ref_download(args, "PhyloP", download_phylop)


def cmd_gnomad_dl(args):
    _run_ref_download(args, "gnomAD", download_gnomad)


def cmd_spliceai_dl(args):
    _run_ref_download(args, "SpliceAI", download_spliceai)


def cmd_alphamissense_dl(args):
    _run_ref_download(args, "AlphaMissense", download_alphamissense)


def cmd_pharmgkb_dl(args):
    _run_ref_download(args, "PharmGKB", download_pharmgkb, "download")


def cmd_bootstrap(args):
    from wgsextract_cli.core.config import save_config, settings
    from wgsextract_cli.core.ref_library import install_mappability_maps
    from wgsextract_cli.core.reference_processing import download_bootstrap

    reflib = args.ref
    configured_reflib = settings.get("reference_library")
    if not reflib:
        reflib = configured_reflib
    if not reflib:
        prog_root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "../../../..")
        )
        reflib = os.path.join(prog_root, "reference")
    reflib = os.path.abspath(reflib)
    should_save_reflib = not configured_reflib

    logging.info("Starting reference library bootstrap...")
    if download_bootstrap(reflib):
        install_maps = getattr(
            args, "install_mappability_maps", False
        ) or os.environ.get("WGSEXTRACT_INSTALL_MAPPABILITY_MAPS") == "1"
        if install_maps and not install_mappability_maps(reflib):
            logging.error("Delly mappability map installation failed.")
            raise WGSExtractError("Ref library installation failed.")
        if not install_maps:
            logging.info(
                "Skipping optional Delly mappability maps. "
                "Use --install-mappability-maps or "
                "WGSEXTRACT_INSTALL_MAPPABILITY_MAPS=1 to preinstall them."
            )
        if should_save_reflib:
            save_config({"reference_library": reflib})
            logging.info(f"Saved reference library path to config.toml: {reflib}")
        logging.info(
            "Bootstrap complete. You can now install genomes via 'wgsextract ref library'."
        )
    else:
        logging.error("Bootstrap failed.")
        raise WGSExtractError("Ref library installation failed.")
