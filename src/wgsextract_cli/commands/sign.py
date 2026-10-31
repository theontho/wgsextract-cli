"""Checksum manifests and OpenSSH signatures for output files."""

import argparse
import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePath

from wgsextract_cli.core.config import settings
from wgsextract_cli.core.messages import CLI_HELP
from wgsextract_cli.core.paths import PathInput, absolute_path, append_suffix
from wgsextract_cli.core.utils import WGSExtractError, get_resource_defaults

MANIFEST_NAME = "SHA256SUMS"
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

    create = subs.add_parser(
        "create", parents=[base_parser], help=CLI_HELP["cmd_sign-create"]
    )
    create.add_argument(
        "paths", nargs="+", metavar="PATH", help="Files or directories to sign."
    )
    create.add_argument(
        "--output",
        help=f"Manifest path. Defaults to <outdir>/{MANIFEST_NAME}, or "
        "SHA256SUMS in the inputs' common directory.",
    )
    create.add_argument(
        "--key",
        help="OpenSSH private key (or public key held by ssh-agent) to sign "
        "with. Defaults to the signing_key setting; without either, only "
        "the checksum manifest is written.",
    )
    create.set_defaults(func=cmd_create)

    verify = subs.add_parser(
        "verify", parents=[base_parser], help=CLI_HELP["cmd_sign-verify"]
    )
    verify.add_argument("manifest", help=f"{MANIFEST_NAME} manifest to verify.")
    verify.add_argument(
        "--signature", help="Signature file. Defaults to <manifest>.sig."
    )
    trust = verify.add_mutually_exclusive_group()
    trust.add_argument("--public-key", help="The signer's OpenSSH public key.")
    trust.add_argument(
        "--allowed-signers", help="OpenSSH allowed_signers file (needs --identity)."
    )
    verify.add_argument("--identity", help="Signer identity in --allowed-signers.")
    verify.set_defaults(func=cmd_verify)


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


def _collect_files(paths: list[str], exclude: set[Path]) -> list[Path]:
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


def _manifest_path(args: argparse.Namespace) -> Path:
    if args.output:
        return absolute_path(args.output)
    if args.outdir:
        return absolute_path(Path(args.outdir) / MANIFEST_NAME)
    parents = [
        absolute_path(p if p.is_dir() else p.parent) for p in map(Path, args.paths)
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


def cmd_create(args: argparse.Namespace) -> None:
    manifest = _manifest_path(args)
    signature = append_suffix(manifest, ".sig")
    key = args.key or settings.get("signing_key")
    if key and not Path(str(key)).is_file():
        raise WGSExtractError(f"Signing key not found: {key}")
    keygen = _ssh_keygen() if key else None

    files = _collect_files(args.paths, {manifest, signature})
    if not files:
        raise WGSExtractError("No files to sign.")
    base = manifest.parent
    names = [_manifest_name(f, base) for f in files]
    if any("\n" in n or "\\" in n for n in names):
        raise WGSExtractError(
            "File names with newlines or backslashes can't be listed."
        )

    threads, _ = get_resource_defaults(args.threads, None)
    logging.info(f"Hashing {len(files)} file(s)...")
    digests = _hash_files(files, threads)

    base.mkdir(parents=True, exist_ok=True)
    if signature.exists():
        signature.unlink()  # it would no longer match the new manifest
    tmp = append_suffix(manifest, ".tmp")
    with tmp.open("w", newline="\n") as f:
        # GNU coreutils format, so `sha256sum -c SHA256SUMS` verifies it too.
        f.writelines(f"{d}  {n}\n" for d, n in zip(digests, names, strict=True))
    tmp.replace(manifest)
    logging.info(f"Checksum manifest written to {manifest}")

    if keygen:
        # Inherit the terminal so ssh-keygen can prompt for a key passphrase.
        cmd = [keygen, "-Y", "sign", "-f", str(key), "-n", NAMESPACE, str(manifest)]
        if subprocess.run(cmd).returncode != 0 or not signature.is_file():
            raise WGSExtractError("ssh-keygen could not sign the manifest.")
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
    args: argparse.Namespace, manifest: Path, signature: Path
) -> None:
    keygen = _ssh_keygen()
    with tempfile.TemporaryDirectory() as tmp:
        if args.public_key:
            with open(args.public_key) as f:
                key_type, key_data = f.read().split()[:2]
            allowed = Path(tmp) / "allowed_signers"
            with allowed.open("w", newline="\n") as f:
                f.write(f'{_SIGNER} namespaces="{NAMESPACE}" {key_type} {key_data}\n')
            trust = ["verify", "-f", str(allowed), "-I", _SIGNER]
        elif args.allowed_signers:
            if not args.identity:
                raise WGSExtractError("--allowed-signers needs --identity.")
            trust = ["verify", "-f", args.allowed_signers, "-I", args.identity]
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
        logging.warning(
            f"The signature is intact and was made with {signer}, but it was not "
            "checked against a trusted key; pass --public-key or "
            "--allowed-signers to confirm who signed it."
        )
    else:
        logging.info(f"Good signature from {signer}.")


def cmd_verify(args: argparse.Namespace) -> None:
    manifest = absolute_path(args.manifest)
    if not manifest.is_file():
        raise WGSExtractError(f"Manifest not found: {args.manifest}")
    signature = (
        Path(args.signature) if args.signature else append_suffix(manifest, ".sig")
    )
    if signature.is_file():
        _verify_signature(args, manifest, signature)
    elif args.signature or args.public_key or args.allowed_signers:
        raise WGSExtractError(f"Signature not found: {signature}")
    else:
        logging.warning("The manifest is not signed; checking file hashes only.")

    entries = _read_manifest(manifest)
    base = manifest.parent
    paths = [base.joinpath(*name.split("/")) for _, name in entries]
    missing = [
        name for (_, name), p in zip(entries, paths, strict=True) if not p.is_file()
    ]
    present = [(e, p) for e, p in zip(entries, paths, strict=True) if p.is_file()]
    threads, _ = get_resource_defaults(args.threads, None)
    logging.info(f"Checking {len(entries)} file(s)...")
    digests = _hash_files([p for _, p in present], threads)
    changed = [
        name
        for ((digest, name), _), actual in zip(present, digests, strict=True)
        if digest != actual
    ]
    problems = [f"missing: {n}" for n in missing] + [f"changed: {n}" for n in changed]
    if problems:
        raise WGSExtractError(
            f"{len(problems)} of {len(entries)} file(s) failed verification:\n  "
            + "\n  ".join(problems[:20])
            + (f"\n  ... and {len(problems) - 20} more" if len(problems) > 20 else "")
        )
    logging.info(f"All {len(entries)} file(s) match the manifest.")
