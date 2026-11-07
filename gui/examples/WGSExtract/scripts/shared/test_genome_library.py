#!/usr/bin/env python3
"""Manage the small GitHub-hosted WGS Extract benchmark genome dataset."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

DATASET_ID = "wgsextract-benchmark-hg19-mini"
DATASET_TITLE = "WGS Extract hg19 mini benchmark genome"
DATASET_URL = (
    "https://github.com/WGSExtract/wgsextract-cli/releases/download/v0.1.0/"
    "wgsextract-benchmark-hg19-mini.zip"
)
DATASET_SHA256 = "ad0f8070dc5ca35c4a6de540493a81df082d160417f747ae68d9c098c110a9f6"
GENOME_CONFIG_NAME = "genome-config.toml"


class HTTPSOnlyRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlparse(newurl).scheme.lower() != "https":
            raise URLError(f"Blocked non-HTTPS redirect: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("state", "download", "delete"))
    parser.add_argument("genome_library", nargs="?", default="")
    args = parser.parse_args(argv[1:])

    root = genome_library_root(args.genome_library)
    if args.command == "state":
        print_state(root)
        return 0
    if args.command == "download":
        download_dataset(root)
        return 0
    delete_dataset(root)
    return 0


def genome_library_root(argument: str) -> Path:
    if argument:
        return Path(argument).expanduser()
    return Path(os.environ.get("GUI_FOR_CLI_BUNDLE_WORKSPACE") or os.getcwd()) / "genomes"


def print_state(root: Path) -> None:
    dataset_dir = root / DATASET_ID
    config = dataset_dir / GENOME_CONFIG_NAME
    installed = dataset_dir.is_dir() and config.is_file()
    partial = (root / ".downloads" / f"{DATASET_ID}.zip.partial").is_file()
    status = "installed" if installed else "incomplete" if partial else "missing"
    payload = {
        "values": {
            "library.testGenomeInstalled": "true" if installed else "false",
            "library.testGenomeStatus": status,
            "library.testGenomePath": str(dataset_dir),
        }
    }
    json.dump(payload, sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")


def download_dataset(root: Path) -> None:
    dataset_dir = root / DATASET_ID
    if dataset_dir.is_dir() and (dataset_dir / GENOME_CONFIG_NAME).is_file():
        missing = missing_payload_files(dataset_dir)
        if missing:
            print(f"{DATASET_TITLE} is missing files; reinstalling: {', '.join(missing)}")
        else:
            print(f"{DATASET_TITLE} is already installed at {dataset_dir}")
            return

    root.mkdir(parents=True, exist_ok=True)
    downloads = root / ".downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    archive_path = downloads / f"{DATASET_ID}.zip"
    partial_path = archive_path.with_suffix(".zip.partial")

    if not archive_path.is_file() or sha256(archive_path) != DATASET_SHA256:
        if archive_path.exists():
            archive_path.unlink()
        download_file(DATASET_URL, partial_path)
        verify_sha256(partial_path, DATASET_SHA256)
        partial_path.replace(archive_path)
    else:
        print(f"Using cached {archive_path}")

    with tempfile.TemporaryDirectory(prefix=f"{DATASET_ID}-", dir=str(downloads)) as tmp:
        extract_root = Path(tmp) / "extract"
        extract_zip_safely(archive_path, extract_root)
        source_root = dataset_payload_root(extract_root)
        write_genome_config(source_root)
        install_payload(source_root, dataset_dir)
    print(f"Installed {DATASET_TITLE} at {dataset_dir}")


def missing_payload_files(dataset_dir: Path) -> list[str]:
    manifest_path = dataset_dir / "manifest.json"
    try:
        with manifest_path.open(encoding="utf-8") as handle:
            manifest = json.load(handle)
    except (OSError, ValueError):
        return ["manifest.json"]
    files = manifest.get("files")
    if not isinstance(files, dict):
        return ["manifest.json"]
    missing: list[str] = []
    for value in files.values():
        if isinstance(value, str) and value and not (dataset_dir / value).is_file():
            missing.append(value)
    return missing


def download_file(url: str, destination: Path) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise SystemExit(f"Unsupported URL scheme for dataset download: {parsed.scheme or 'none'}")
    request = Request(url, headers={"User-Agent": "gui-for-cli/wgsextract-test-genome"})
    opener = build_opener(HTTPSOnlyRedirectHandler)
    print(f"Downloading {url}")
    try:
        with opener.open(request, timeout=300) as response:
            total = int(response.headers.get("Content-Length") or "0")
            downloaded = 0
            last_reported = 0
            with destination.open("wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    downloaded += len(chunk)
                    if downloaded - last_reported >= 5 * 1024 * 1024:
                        print(progress_line(downloaded, total), flush=True)
                        last_reported = downloaded
        print(progress_line(downloaded, total), flush=True)
    except (OSError, URLError) as error:
        if destination.exists():
            destination.unlink()
        raise SystemExit(f"Download failed: {error}") from error


def progress_line(downloaded: int, total: int) -> str:
    if total > 0:
        return f"Downloaded {human_bytes(downloaded)} of {human_bytes(total)}"
    return f"Downloaded {human_bytes(downloaded)}"


def human_bytes(total: int) -> str:
    if total >= 1_073_741_824:
        return f"{total / 1_073_741_824:.1f} GB"
    if total >= 1_048_576:
        return f"{total / 1_048_576:.1f} MB"
    if total >= 1024:
        return f"{total / 1024:.1f} KB"
    return f"{total} B"


def verify_sha256(path: Path, expected: str) -> None:
    actual = sha256(path)
    if actual != expected:
        path.unlink(missing_ok=True)
        raise SystemExit(f"Checksum mismatch for {path}: expected {expected}, got {actual}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract_zip_safely(archive_path: Path, extract_root: Path) -> None:
    extract_root.mkdir(parents=True, exist_ok=True)
    root = extract_root.resolve()
    try:
        with zipfile.ZipFile(archive_path) as archive:
            for member in archive.infolist():
                if stat.S_ISLNK(member.external_attr >> 16):
                    raise SystemExit(f"Unsafe zip symlink entry: {member.filename}")
                target = (extract_root / member.filename).resolve()
                if not is_relative_to(target, root):
                    raise SystemExit(f"Unsafe zip entry: {member.filename}")
            archive.extractall(extract_root)
    except zipfile.BadZipFile as error:
        raise SystemExit(f"Invalid dataset zip: {archive_path}") from error


def dataset_payload_root(extract_root: Path) -> Path:
    manifest = extract_root / "manifest.json"
    if manifest.is_file():
        return extract_root
    candidates = list(extract_root.glob("*/manifest.json"))
    if len(candidates) == 1:
        return candidates[0].parent
    raise SystemExit(f"Dataset manifest not found under {extract_root}")


def write_genome_config(dataset_dir: Path) -> None:
    manifest_path = dataset_dir / "manifest.json"
    with manifest_path.open(encoding="utf-8") as handle:
        manifest = json.load(handle)
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise SystemExit(f"Dataset manifest has no files object: {manifest_path}")

    lines = [
        "# WGS Extract per-genome configuration",
        f"# Downloaded by GUI for CLI from {DATASET_URL}",
        f"# Dataset: {manifest.get('dataset_id') or DATASET_ID}",
        "",
    ]
    add_config_line(lines, "alignment", files.get("bam") or files.get("cram"))
    add_config_line(lines, "vcf", files.get("vcf"))
    add_config_line(lines, "fastq_r1", files.get("fastq_r1"))
    add_config_line(lines, "fastq_r2", files.get("fastq_r2"))
    (dataset_dir / GENOME_CONFIG_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")


def add_config_line(lines: list[str], key: str, value: object) -> None:
    if isinstance(value, str) and value:
        lines.append(f'{key} = "{toml_escape(value)}"')


def toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def install_payload(source_root: Path, dataset_dir: Path) -> None:
    tmp_target = dataset_dir.with_name(f".{dataset_dir.name}.installing")
    if tmp_target.exists():
        shutil.rmtree(tmp_target)
    shutil.copytree(source_root, tmp_target)
    if dataset_dir.exists():
        shutil.rmtree(dataset_dir)
    tmp_target.replace(dataset_dir)


def delete_dataset(root: Path) -> None:
    dataset_dir = root / DATASET_ID
    downloads = root / ".downloads"
    root_resolved = root.resolve()
    target = dataset_dir.resolve(strict=False)
    if not is_relative_to(target, root_resolved):
        raise SystemExit(f"Refusing to delete outside genome library: {dataset_dir}")
    deleted = False
    if dataset_dir.exists():
        shutil.rmtree(dataset_dir)
        print(f"Deleted {dataset_dir}")
        deleted = True
    for candidate in (
        downloads / f"{DATASET_ID}.zip",
        downloads / f"{DATASET_ID}.zip.partial",
        dataset_dir.with_name(f".{dataset_dir.name}.installing"),
    ):
        target = candidate.resolve(strict=False)
        if not is_relative_to(target, root_resolved):
            raise SystemExit(f"Refusing to delete outside genome library: {candidate}")
        if candidate.is_dir():
            shutil.rmtree(candidate)
            print(f"Deleted {candidate}")
            deleted = True
        elif candidate.exists():
            candidate.unlink()
            print(f"Deleted {candidate}")
            deleted = True
    remove_empty_downloads(downloads)
    if not deleted:
        print(f"No test genome dataset found at {dataset_dir}")


def remove_empty_downloads(downloads: Path) -> None:
    try:
        downloads.rmdir()
    except FileNotFoundError:
        return
    except OSError:
        return


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
