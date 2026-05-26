"""Reference library package — genome catalog and download helpers.

Re-exports the public surface so `from wgsextract_cli.core.ref_library import X`
continues to work after the split into `downloads` and `catalog` submodules.
"""

from .catalog import (
    GENOME_DATA,
    get_available_genomes,
    get_genome_status,
    get_grouped_genomes,
    install_mappability_maps,
    install_standard_mappability_maps,
    is_genome_installed,
    load_genomes_from_csv,
)
from .downloads import (
    CancelEvent,
    ProgressCallback,
    download_file,
    resolve_github_release_asset_sha256,
    verify_download_sha256,
)

__all__ = [
    "GENOME_DATA",
    "CancelEvent",
    "ProgressCallback",
    "download_file",
    "get_available_genomes",
    "get_genome_status",
    "get_grouped_genomes",
    "install_mappability_maps",
    "install_standard_mappability_maps",
    "is_genome_installed",
    "load_genomes_from_csv",
    "resolve_github_release_asset_sha256",
    "verify_download_sha256",
]
