"""Explicit installation and read-only validation of the public AADR 2M panel."""

import hashlib
from pathlib import Path

from wgsextract_cli.core.ref_library.downloads import download_file
from wgsextract_cli.core.reference_assets import resolve_reference_library
from wgsextract_cli.core.utils import WGSExtractError

AADR_2M_URL = "https://dataverse.harvard.edu/api/access/datafile/13994517"
AADR_2M_SHA256 = "39312150ce88ec757a88e52a7921ca7fd1190d9cce11cfe39c756ea5737b7a9b"
AADR_2M_FILENAME = "v66.p1_2M.aadr.patch.PUB.snp"


def aadr_2m_path(reflib_dir: str | None = None) -> Path:
    return Path(resolve_reference_library(reflib_dir), "aadr", AADR_2M_FILENAME)


def require_aadr_2m(reflib_dir: str | None = None) -> Path:
    """Never download, repair or delete a missing/corrupt installed resource."""
    path = aadr_2m_path(reflib_dir)
    hint = f"Run 'wgsextract ref aadr --install --ref {path.parent.parent}'."
    if not path.is_file():
        raise WGSExtractError(f"AADR 2M SNP metadata is not installed: {path}. {hint}")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise WGSExtractError(f"Cannot read AADR 2M SNP metadata: {path}.") from error
    if digest.hexdigest() != AADR_2M_SHA256:
        raise WGSExtractError(f"AADR 2M SNP metadata SHA256 mismatch: {path}. {hint}")
    return path


def install_aadr_2m(reflib_dir: str | None = None) -> Path:
    """Use the existing resumable, hash-verified shared-download mechanism."""
    path = aadr_2m_path(reflib_dir)
    if path.is_file():
        try:
            return require_aadr_2m(reflib_dir)
        except WGSExtractError:
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    if not download_file(
        AADR_2M_URL,
        str(path),
        expected_sha256=AADR_2M_SHA256,
        resolve_release_digest=False,
    ):
        raise WGSExtractError("Could not fetch the pinned AADR v66.p1 2M SNP metadata.")
    return require_aadr_2m(reflib_dir)
