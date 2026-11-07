#!/usr/bin/env python3
"""Create an Arch Linux pacman package for the Tauri AppImage."""

from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--appimage", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--app-name", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--icon", type=Path)
    parser.add_argument("--pkgrel", default="1")
    parser.add_argument("--url", default="https://github.com/WGSExtract/wgsextract-cli")
    args = parser.parse_args()

    package_path = create_archlinux_package(
        appimage=args.appimage,
        output_dir=args.output_dir,
        app_name=args.app_name,
        version=args.version,
        icon=args.icon,
        pkgrel=args.pkgrel,
        url=args.url,
    )
    print(f"Created Arch Linux package: {package_path}")
    return 0


def create_archlinux_package(
    *,
    appimage: Path,
    output_dir: Path,
    app_name: str,
    version: str,
    icon: Path | None,
    pkgrel: str,
    url: str,
) -> Path:
    if not appimage.is_file():
        raise FileNotFoundError(f"AppImage does not exist: {appimage}")

    zstd = shutil.which("zstd")
    if not zstd:
        raise RuntimeError("Arch Linux package creation requires zstd.")

    pkgname = arch_package_name(app_name)
    pkgver = arch_package_version(version)
    pkgrel = arch_package_version(pkgrel)
    arch = arch_package_arch(platform.machine())
    output_dir.mkdir(parents=True, exist_ok=True)
    package_path = output_dir / f"{pkgname}-{pkgver}-{pkgrel}-{arch}.pkg.tar.zst"

    with tempfile.TemporaryDirectory(prefix="gui-for-cli-archpkg-") as temp_dir:
        staging_dir = Path(temp_dir) / "package"
        archive_path = Path(temp_dir) / package_path.with_suffix("").name
        app_dir = staging_dir / "opt" / pkgname
        bin_dir = staging_dir / "usr/bin"
        applications_dir = staging_dir / "usr/share/applications"
        pixmaps_dir = staging_dir / "usr/share/pixmaps"

        app_dir.mkdir(parents=True)
        bin_dir.mkdir(parents=True)
        applications_dir.mkdir(parents=True)
        pixmaps_dir.mkdir(parents=True)

        installed_appimage = app_dir / f"{pkgname}.AppImage"
        shutil.copy2(appimage, installed_appimage)
        installed_appimage.chmod(0o755)

        launcher = bin_dir / pkgname
        launcher.write_text(
            "\n".join(
                [
                    "#!/bin/sh",
                    f'exec "/opt/{pkgname}/{pkgname}.AppImage" "$@"',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        launcher.chmod(0o755)

        desktop_file = applications_dir / f"{pkgname}.desktop"
        desktop_file.write_text(
            "\n".join(
                [
                    "[Desktop Entry]",
                    "Type=Application",
                    f"Name={app_name}",
                    f"Exec=/usr/bin/{pkgname}",
                    f"Icon={pkgname}",
                    "Categories=Development;Utility;",
                    "Terminal=false",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        if icon is not None:
            if not icon.is_file():
                raise FileNotFoundError(f"Icon does not exist for {pkgname}: {icon}")
            shutil.copy2(icon, pixmaps_dir / f"{pkgname}.png")

        pkginfo = staging_dir / ".PKGINFO"
        pkginfo.write_text(
            package_info(
                pkgname=pkgname,
                pkgver=pkgver,
                pkgrel=pkgrel,
                pkgdesc=f"{app_name} desktop package",
                url=url,
                arch=arch,
                installed_size=installed_size(staging_dir),
            ),
            encoding="utf-8",
        )

        write_tar(archive_path, staging_dir)
        package_path.unlink(missing_ok=True)
        subprocess.run([zstd, "-19", "-f", str(archive_path), "-o", str(package_path)], check=True)

    return package_path


def arch_package_name(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9@._+-]+", "-", value.lower()).strip("-")
    normalized = re.sub(r"-+", "-", normalized)
    if not normalized:
        raise ValueError(f"Cannot derive Arch package name from app name: {value!r}")
    return normalized


def arch_package_version(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._+]+", ".", value).strip(".")
    normalized = re.sub(r"\.+", ".", normalized)
    if not normalized:
        raise ValueError(f"Cannot derive Arch package version from value: {value!r}")
    return normalized


def arch_package_arch(machine: str) -> str:
    normalized = machine.lower()
    if normalized in {"amd64", "x86_64"}:
        return "x86_64"
    if normalized in {"aarch64", "arm64"}:
        return "aarch64"
    if normalized in {"armv7l", "armv7"}:
        return "armv7h"
    return re.sub(r"[^A-Za-z0-9_]+", "_", normalized) or "any"


def package_info(
    *,
    pkgname: str,
    pkgver: str,
    pkgrel: str,
    pkgdesc: str,
    url: str,
    arch: str,
    installed_size: int,
) -> str:
    lines = [
        f"pkgname = {pkgname}",
        f"pkgbase = {pkgname}",
        f"pkgver = {pkgver}-{pkgrel}",
        f"pkgdesc = {pkgdesc}",
        f"url = {url}",
        f"builddate = {int(time.time())}",
        "packager = GUI for CLI",
        f"size = {installed_size}",
        f"arch = {arch}",
        "license = MIT",
        "depend = fuse2",
        "",
    ]
    return "\n".join(lines)


def installed_size(staging_dir: Path) -> int:
    total = 0
    for path in staging_dir.rglob("*"):
        if path.is_file() and path.name != ".PKGINFO":
            total += path.stat().st_size
    return total


def write_tar(archive_path: Path, staging_dir: Path) -> None:
    with tarfile.open(archive_path, "w", format=tarfile.PAX_FORMAT) as archive:
        for path in sorted(staging_dir.rglob("*")):
            arcname = path.relative_to(staging_dir)
            archive.add(path, arcname=os.fspath(arcname), recursive=False)


if __name__ == "__main__":
    raise SystemExit(main())
