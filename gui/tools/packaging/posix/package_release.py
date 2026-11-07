#!/usr/bin/env python3
"""Build and stage Tauri release artifacts for macOS and Linux hosts."""

from __future__ import annotations

import argparse
import os
import platform
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from embedded_branding import load_embedded_branding  # noqa: E402
from common import REPO_ROOT, copy_path, repo, reset_dir, run  # noqa: E402
from macos_notarize import notarize_dmgs  # noqa: E402
from package_tauri_windows_cross import stage_tauri_windows_cross_release  # noqa: E402


def env_value(name: str, default: str) -> str:
    return os.environ.get(name) or default


RELEASE_DIR = Path(env_value("RELEASE_DIR", "out/release"))
APP_NAME = env_value("APP_NAME", "GUI for CLI")
TAURI_BUNDLE_DIR = Path(
    os.environ.get(
        "TAURI_BUNDLE_DIR",
        "app/web/packagers/tauri/target/release/bundle",
    )
)
MACOS_UPDATER_ARCHIVE_SUFFIX = ".app.tar.gz"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", choices=sorted(PACKAGES))
    args = parser.parse_args()
    PACKAGES[args.package]()
    return 0


def release_path(name: str, default: str) -> Path:
    return repo(env_value(name, str(RELEASE_DIR / default)))


def copy_matching_artifacts(bundle_dir: Path, dest: Path, patterns: list[str]) -> None:
    copied = False
    for pattern in patterns:
        for artifact in sorted(bundle_dir.glob(pattern)):
            copy_path(artifact, dest / artifact.name, git_filtered=False)
            copied = True
    if not copied:
        joined = ", ".join(patterns)
        raise FileNotFoundError(f"No artifacts matching [{joined}] under {bundle_dir}")


def copy_optional_matching_artifacts(bundle_dir: Path, dest: Path, patterns: list[str]) -> None:
    for pattern in patterns:
        for artifact in sorted(bundle_dir.glob(pattern)):
            copy_path(artifact, dest / artifact.name, git_filtered=False)


def macos_updater_arch(machine: str) -> str:
    """Tauri names updater archives `<App>.app.tar.gz` without an architecture;
    the release feed keys macOS platforms by an `_<arch>` filename suffix."""
    return "aarch64" if machine.lower() in {"arm64", "aarch64"} else "x64"


def stage_macos_updater_archives(bundle_dir: Path, dest: Path, arch: str) -> None:
    for archive in sorted(bundle_dir.glob(f"macos/*{MACOS_UPDATER_ARCHIVE_SUFFIX}")):
        stem = archive.name[: -len(MACOS_UPDATER_ARCHIVE_SUFFIX)]
        target_name = f"{stem}_{arch}{MACOS_UPDATER_ARCHIVE_SUFFIX}"
        copy_path(archive, dest / target_name, git_filtered=False)
        signature = archive.with_name(f"{archive.name}.sig")
        if signature.is_file():
            copy_path(signature, dest / f"{target_name}.sig", git_filtered=False)


def stage_tauri_archlinux_package(bundle_dir: Path, dest: Path) -> None:
    appimage = resolve_tauri_archlinux_appimage(bundle_dir)
    branding = load_embedded_branding(REPO_ROOT)
    run(
        [
            sys.executable,
            "tools/packaging/posix/package_tauri_archlinux.py",
            "--appimage",
            str(appimage),
            "--output-dir",
            str(dest),
            "--app-name",
            branding.effective_app_name or APP_NAME,
            "--version",
            branding.effective_app_version or "0.1.0",
            "--icon",
            str(repo("app/web/packagers/tauri/icons/icon.png")),
        ]
    )


def resolve_tauri_archlinux_appimage(bundle_dir: Path) -> Path:
    configured = os.environ.get("TAURI_ARCH_APPIMAGE_PATH") or os.environ.get("TAURI_ARCH_APPIMAGE")
    if configured:
        appimage = repo(configured)
        if not appimage.is_file():
            raise FileNotFoundError(f"Configured Arch Linux AppImage does not exist: {appimage}")
        return appimage

    appimage_dir = bundle_dir / "appimage"
    appimages = sorted(appimage_dir.glob("*.AppImage"))
    if not appimages:
        raise FileNotFoundError(
            f"No AppImage artifacts found under {appimage_dir}. "
            "Build the appimage bundle or set TAURI_ARCH_APPIMAGE_PATH."
        )
    if len(appimages) > 1:
        found = ", ".join(str(path) for path in appimages)
        raise RuntimeError(
            "Expected exactly one AppImage artifact for Arch Linux packaging; "
            f"found {len(appimages)}: {found}. Set TAURI_ARCH_APPIMAGE_PATH to choose one."
        )
    return appimages[0]


def stage_tauri_release() -> None:
    run(["npm", "--prefix", "app", "run", "tauri:dist"])
    dest = release_path("TAURI_RELEASE_DIR", "tauri")
    reset_dir(dest)
    bundle_dir = repo(TAURI_BUNDLE_DIR)
    if sys.platform == "darwin":
        copy_matching_artifacts(bundle_dir, dest, ["macos/*.app", "dmg/*.dmg"])
        notarize_dmgs(sorted(dest.glob("*.dmg")))
        stage_macos_updater_archives(bundle_dir, dest, macos_updater_arch(platform.machine()))
    elif sys.platform.startswith("linux"):
        copy_matching_artifacts(bundle_dir, dest, ["deb/*.deb"])
        copy_matching_artifacts(bundle_dir, dest, ["rpm/*.rpm"])
        copy_matching_artifacts(bundle_dir, dest, ["appimage/*.AppImage"])
        copy_optional_matching_artifacts(bundle_dir, dest, ["appimage/*.AppImage.sig"])
        stage_tauri_archlinux_package(bundle_dir, dest)
    else:
        raise RuntimeError(f"Unsupported POSIX Tauri packaging platform: {sys.platform}")


PACKAGES = {
    "tauri": stage_tauri_release,
    "tauri-windows-cross": lambda: stage_tauri_windows_cross_release(
        release_path("TAURI_WINDOWS_RELEASE_DIR", "tauri-windows")
    ),
}


if __name__ == "__main__":
    raise SystemExit(main())
