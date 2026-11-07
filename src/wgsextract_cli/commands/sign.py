"""Checksum manifests and OpenSSH signatures for output files."""

import argparse
import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePath

from wgsextract_cli.api._cli import add_arguments, cli_handler
from wgsextract_cli.api.sign import MANIFEST_NAME, SignCreateParams, SignVerifyParams
from wgsextract_cli.core import read_scope
from wgsextract_cli.core.atomic_output import atomic_output
from wgsextract_cli.core.config import settings
from wgsextract_cli.core.messages import CLI_HELP
from wgsextract_cli.core.paths import PathInput, absolute_path, append_suffix
from wgsextract_cli.core.results import current_result, manifest_path_for, warn
from wgsextract_cli.core.utils import (
    UsageError,
    WGSExtractError,
    get_resource_defaults,
)

# ssh-keygen -Y signature namespace, so a signature made for another purpose
# (e.g. a git commit) can't be replayed as a wgsextract manifest signature.
NAMESPACE = "wgsextract"
_SIGNER = "wgsextract-signer"
_CHUNK = 4 << 20
_FINGERPRINT = re.compile(r"(\S+) key (SHA256:\S+)")
# Verifying a manifest takes well under a second.
_VERIFY_TIMEOUT = 10


def register(
    subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",
    base_parser: argparse.ArgumentParser,
) -> None:
    parser = subparsers.add_parser("sign", help=CLI_HELP["cmd_sign"])
    subs = parser.add_subparsers(dest="sign_cmd", required=True)
    for name, cls, impl in (
        ("create", SignCreateParams, create_manifest),
        ("verify", SignVerifyParams, verify_manifest),
    ):
        sub = subs.add_parser(
            name, parents=[base_parser], help=CLI_HELP[f"cmd_sign-{name}"]
        )
        add_arguments(sub, cls)
        sub.set_defaults(func=cli_handler(cls, impl))


def _ssh_keygen() -> str:
    path = shutil.which("ssh-keygen")
    if not path:
        raise WGSExtractError(
            "Signing needs OpenSSH's ssh-keygen (8.1 or newer), which was not "
            "found on PATH."
        )
    return path


def _sha256(path: PathInput) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as f:
        while chunk := f.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_files(paths: list[Path], threads: str) -> list[str]:
    # hashlib releases the GIL, so threads hash several files at once.
    with ThreadPoolExecutor(max_workers=max(1, int(threads))) as pool:
        return list(pool.map(_sha256, paths))


def _collect_files(paths: Sequence[PathInput], exclude: set[Path]) -> list[Path]:
    files: list[Path] = []
    for value in paths:
        path = Path(value)
        if path.is_dir():
            for root, dirs, names in os.walk(path):
                dirs.sort()
                files += [Path(root) / n for n in sorted(names)]
        elif path.is_file():
            files.append(path)
        else:
            raise WGSExtractError(f"Not a file or directory: {path}")
    unique = dict.fromkeys(absolute_path(f) for f in files)
    return [f for f in unique if f not in exclude]


def manifest_path(params: SignCreateParams) -> Path:
    """Where ``sign create`` writes its manifest."""
    if params.output:
        return absolute_path(params.output)
    if params.outdir:
        return absolute_path(Path(params.outdir) / MANIFEST_NAME)
    parents = [
        absolute_path(p if p.is_dir() else p.parent) for p in map(Path, params.paths)
    ]
    common = parents[0]
    for parent in parents[1:]:
        if not parent.is_relative_to(common.anchor):
            raise WGSExtractError(
                "Inputs on different drives need --output or --outdir."
            )
        while not parent.is_relative_to(common):
            common = common.parent
    return common / MANIFEST_NAME


def _manifest_name(path: PurePath, base: PurePath) -> str:
    """Relative names may leave --outdir; walk_up isn't available on Python 3.10."""
    if not path.is_relative_to(base.anchor):
        raise WGSExtractError("Manifest and inputs must be on the same drive.")
    common = base
    while not path.is_relative_to(common):
        common = common.parent
    upward = [".."] * len(base.relative_to(common).parts)
    return "/".join([*upward, path.relative_to(common).as_posix()])


def create_manifest(params: SignCreateParams) -> None:
    manifest = manifest_path(params)
    signature = append_suffix(manifest, ".sig")
    key = params.key or settings.get("signing_key")
    if key and not Path(str(key)).is_file():
        raise WGSExtractError(f"Signing key not found: {key}")
    keygen = _ssh_keygen() if key else None

    # This run's result manifest is rewritten after hashing; never list it.
    files = _collect_files(
        params.paths, {manifest, signature, absolute_path(manifest_path_for(manifest))}
    )
    if not files:
        raise WGSExtractError("No files to sign.")
    base = manifest.parent
    names = [_manifest_name(f, base) for f in files]
    if any("\n" in n or "\\" in n for n in names):
        raise WGSExtractError(
            "File names with newlines or backslashes can't be listed."
        )

    threads, _ = get_resource_defaults(params.threads, None)
    logging.info(f"Hashing {len(files)} file(s)...")
    digests = _hash_files(files, threads)

    base.mkdir(parents=True, exist_ok=True)
    if signature.exists():
        signature.unlink()  # it would no longer match the new manifest
    with atomic_output(manifest, retire_indexes=False) as staged:
        with open(staged, "x", newline="\n") as f:
            # GNU coreutils format, so `sha256sum -c SHA256SUMS` verifies it too.
            f.writelines(f"{d}  {n}\n" for d, n in zip(digests, names, strict=True))
    logging.info(f"Checksum manifest written to {manifest}")
    result = current_result()
    result.add_output(manifest, "checksums")
    result.add_metrics(files=len(files), signed=bool(keygen))

    if keygen:
        # Inherit the terminal so ssh-keygen can prompt for a key passphrase.
        cmd = [keygen, "-Y", "sign", "-f", str(key), "-n", NAMESPACE, str(manifest)]
        if subprocess.run(cmd).returncode != 0 or not signature.is_file():
            raise WGSExtractError("ssh-keygen could not sign the manifest.")
        result.add_output(signature, "signature")
        logging.info(
            f"Signature written to {signature}. Share your public key so others "
            f"can run: wgsextract sign verify {MANIFEST_NAME} --public-key KEY.pub"
        )


def _read_manifest(manifest: PathInput) -> list[tuple[str, str]]:
    entries = []
    with Path(manifest).open() as f:
        for number, line in enumerate(f, 1):
            # "<digest> <mode><name>": mode is " " (text) or "*" (binary).
            match = re.fullmatch(r"([0-9a-fA-F]{64}) [ *](.+)", line.rstrip("\n"))
            if not match:
                raise WGSExtractError(f"{manifest}:{number}: not a SHA-256 line.")
            entries.append((match[1].lower(), match[2]))
    return entries


def _verify_signature(
    params: SignVerifyParams, manifest: Path, signature: Path
) -> None:
    keygen = _ssh_keygen()
    with tempfile.TemporaryDirectory() as tmp:
        if params.public_key:
            with open(params.public_key) as f:
                key_type, key_data = f.read().split()[:2]
            allowed = Path(tmp) / "allowed_signers"
            with allowed.open("w", newline="\n") as f:
                f.write(f'{_SIGNER} namespaces="{NAMESPACE}" {key_type} {key_data}\n')
            trust = ["verify", "-f", str(allowed), "-I", _SIGNER]
        elif params.allowed_signers:
            if not params.identity:
                raise UsageError("--allowed-signers needs --identity.")
            trust = [
                "verify",
                "-f",
                str(params.allowed_signers),
                "-I",
                params.identity,
            ]
        else:
            trust = ["check-novalidate"]
        cmd = [keygen, "-Y", *trust, "-n", NAMESPACE, "-s", str(signature)]
        with manifest.open("rb") as stdin:
            try:
                res = subprocess.run(
                    cmd,
                    stdin=stdin,
                    capture_output=True,
                    text=True,
                    errors="replace",
                    timeout=_VERIFY_TIMEOUT,
                )
            except subprocess.TimeoutExpired as e:
                # Windows' OpenSSH reports a bad signature and then never exits.
                message = "".join(
                    s.decode(errors="replace") if isinstance(s, bytes) else s
                    for s in (e.stdout or "", e.stderr or "")
                ).strip()
                raise WGSExtractError(
                    "Signature verification FAILED: "
                    + (message or "ssh-keygen did not finish.")
                ) from None
    message = (res.stdout + res.stderr).strip()
    if res.returncode != 0:
        raise WGSExtractError(f"Signature verification FAILED: {message}")
    match = _FINGERPRINT.search(message)
    signer = f"{match.group(1)} key {match.group(2)}" if match else "the signer's key"
    if trust[0] == "check-novalidate":
        warn(
            "signature_not_trusted",
            f"The signature is intact and was made with {signer}, but it was not "
            "checked against a trusted key; pass --public-key or "
            "--allowed-signers to confirm who signed it.",
        )
    else:
        logging.info(f"Good signature from {signer}.")


def _refuse_unreadable(paths: Sequence[Path]) -> None:
    """A server may read only its allowed folders: refuse before any stat or hash."""
    refused = [str(p) for p in paths if not read_scope.may_follow(p)]
    if refused:
        raise WGSExtractError(
            "Outside the folders this server may read: "
            + ", ".join(refused[:5])
            + (f" and {len(refused) - 5} more" if len(refused) > 5 else "")
        )


def verify_manifest(params: SignVerifyParams) -> None:
    manifest = absolute_path(params.manifest)
    if not manifest.is_file():
        raise WGSExtractError(f"Manifest not found: {params.manifest}")
    signature = (
        Path(params.signature) if params.signature else append_suffix(manifest, ".sig")
    )
    _refuse_unreadable([signature])
    if signature.is_file():
        _verify_signature(params, manifest, signature)
    elif params.signature or params.public_key or params.allowed_signers:
        raise WGSExtractError(f"Signature not found: {signature}")
    else:
        warn("unsigned", "The manifest is not signed; checking file hashes only.")

    entries = _read_manifest(manifest)
    base = manifest.parent
    paths = [base.joinpath(*name.split("/")) for _, name in entries]
    _refuse_unreadable(paths)

    missing = [
        name for (_, name), p in zip(entries, paths, strict=True) if not p.is_file()
    ]
    present = [(e, p) for e, p in zip(entries, paths, strict=True) if p.is_file()]
    threads, _ = get_resource_defaults(params.threads, None)
    logging.info(f"Checking {len(entries)} file(s)...")
    digests = _hash_files([p for _, p in present], threads)
    changed = [
        name
        for ((digest, name), _), actual in zip(present, digests, strict=True)
        if digest != actual
    ]
    problems = [f"missing: {n}" for n in missing] + [f"changed: {n}" for n in changed]
    current_result().add_metrics(
        files=len(entries),
        missing_files=len(missing),
        changed_files=len(changed),
        signed=signature.is_file(),
    )
    if problems:
        raise WGSExtractError(
            f"{len(problems)} of {len(entries)} file(s) failed verification:\n  "
            + "\n  ".join(problems[:20])
            + (f"\n  ... and {len(problems) - 20} more" if len(problems) > 20 else "")
        )
    logging.info(f"All {len(entries)} file(s) match the manifest.")
