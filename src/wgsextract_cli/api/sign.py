"""``sign``: SHA-256 checksum manifests, optionally signed with an OpenSSH key,
so whoever receives a set of output files can check none changed.

``create`` hashes files (folders are walked) into a GNU coreutils
``SHA256SUMS`` manifest whose names are relative to its folder, and, given a
key, signs it to ``SHA256SUMS.sig`` with ``ssh-keygen -Y sign``. ``verify``
checks the signature, if there is one, then every listed file's hash; it
fails if any is missing or changed. Signing and checking signatures need
OpenSSH's ``ssh-keygen`` 8.1 or newer.

The result's metrics: ``files`` (listed), ``signed``, and for ``verify``
``missing_files`` and ``changed_files``."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from wgsextract_cli.core.results import CommandResult

from ._params import Params, param
from ._runtime import CancelToken, ProgressCallback, execute
from ._shared import outdir_param, threads_param

MANIFEST_NAME = "SHA256SUMS"


@dataclass(frozen=True, kw_only=True)
class SignCreateParams(Params):
    """Write a checksum manifest of ``paths``, signed when a key is given."""

    COMMAND = "sign create"

    paths: tuple[Path, ...] = param(
        help="Files or directories to sign.",
        doc="Files, or folders whose files (recursively) to list.",
        metavar="PATH",
        path="file",
        positional=True,
    )
    output: Path | None = param(
        None,
        help=f"Manifest path. Defaults to <outdir>/{MANIFEST_NAME}, or "
        "SHA256SUMS in the inputs' common directory.",
        path="file",
    )
    key: Path | None = param(
        None,
        help="OpenSSH private key (or public key held by ssh-agent) to sign "
        "with. Defaults to the signing_key setting; without either, only "
        "the checksum manifest is written.",
        doc="OpenSSH private key (or public key held by ssh-agent) to sign "
        "with (default: config signing_key; with neither, the manifest is not "
        "signed). Never taken from a server request.",
        path="file",
        local_only=True,
    )
    outdir: Path | None = outdir_param("the inputs' common folder")
    threads: str | None = threads_param()

    def write_folders(self) -> Iterator[Path]:
        if self.output is not None:
            yield self.output.parent
        elif self.outdir is None:
            from wgsextract_cli.commands.sign import manifest_path

            yield manifest_path(self).parent


@dataclass(frozen=True, kw_only=True)
class SignVerifyParams(Params):
    """Check a checksum manifest's signature and every file it lists. With
    neither ``public_key`` nor ``allowed_signers``, a signature is only
    checked to be intact (a warning says it was not checked against a trusted
    key); with no signature file, only the hashes are checked."""

    COMMAND = "sign verify"

    manifest: Path = param(
        help=f"{MANIFEST_NAME} manifest to verify.",
        metavar="manifest",
        path="file",
        positional=True,
    )
    signature: Path | None = param(
        None,
        help="Signature file. Defaults to <manifest>.sig.",
        path="file",
    )
    public_key: Path | None = param(
        None,
        help="The signer's OpenSSH public key.",
        path="file",
        exclusive="trust",
    )
    allowed_signers: Path | None = param(
        None,
        help="OpenSSH allowed_signers file (needs --identity).",
        path="file",
        exclusive="trust",
    )
    identity: str | None = param(None, help="Signer identity in --allowed-signers.")
    threads: str | None = threads_param()


def create(
    params: SignCreateParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Write (and sign) a checksum manifest (see ``SignCreateParams``)."""
    from wgsextract_cli.commands.sign import create_manifest

    return execute(
        params, create_manifest, progress=progress, cancel=cancel, check=check
    )


def verify(
    params: SignVerifyParams,
    *,
    progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    check: bool = True,
) -> CommandResult:
    """Verify a checksum manifest and its signature (see ``SignVerifyParams``)."""
    from wgsextract_cli.commands.sign import verify_manifest

    return execute(
        params, verify_manifest, progress=progress, cancel=cancel, check=check
    )


__all__ = ["SignCreateParams", "SignVerifyParams", "create", "verify"]
