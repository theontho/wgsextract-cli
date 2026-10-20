"""Build the WGS Extract app the lab tests, from a gui-for-cli checkout: the
macOS DMG (and the bundle-test runner), the Windows installer (cross-built), and
the Linux .deb (built in a container). Every run builds them anew, so a stale
build is never what passes.
"""

from __future__ import annotations

import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

import windows_vm
from housekeeping import mark_used

LINUX_IMAGE = "ubuntu:24.04"


def log(message: str) -> None:
    windows_vm.log(message)


def run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    result: subprocess.CompletedProcess[str] = subprocess.run(  # type: ignore[call-overload]
        cmd, text=True, check=True, **kwargs
    )
    return result


def build_gui_artifacts(gui_repo: Path, dmg: Path | None, serve: Path) -> bool:
    """The app's DMG and the bundle-test runner; returns whether the app is
    Developer ID signed. Builds both from gui_repo unless a DMG is given, so a
    run never tests a stale build."""
    apple = gui_repo / "platform" / "apple"
    if dmg is None:
        log("Packaging the macOS app (make package PLATFORM=swiftui-macos)...")
        run(["make", "package", "PLATFORM=swiftui-macos"], cwd=gui_repo)
        dmg = max(
            (gui_repo / "out" / "release" / "swiftui").glob("WGSExtract*.dmg"),
            key=lambda p: p.stat().st_mtime,
        )
    shutil.copy2(dmg, serve / "WGSExtract.dmg")
    log("Building the gui-for-cli bundle-test runner...")
    run(["swift", "build", "-c", "release", "--product", "gui-for-cli"], cwd=apple)
    release = apple / ".build" / "release"
    with tarfile.open(serve / "gui-runner.tar.gz", "w:gz") as tar:
        for name in ("gui-for-cli", "gui-for-cli_GUIForCLICore.bundle"):
            tar.add(release / name, arcname=name)
    mount = Path(tempfile.mkdtemp(prefix="wgse-lab-dmg-"))
    run(
        [
            "hdiutil",
            "attach",
            "-nobrowse",
            "-readonly",
            "-mountpoint",
            str(mount),
            str(dmg),
        ]
    )
    try:
        info = subprocess.run(
            ["codesign", "-dv", "--verbose=2", str(mount / "WGSExtract.app")],
            capture_output=True,
            text=True,
        ).stderr
    finally:
        run(["hdiutil", "detach", str(mount)])
        mount.rmdir()
    return "Authority=Developer ID Application" in info


def build_windows_gui_installer(
    gui_repo: Path | None, installer: Path | None, serve: Path
) -> tuple[str, bool]:
    """The app's NSIS installer, cross-built from gui_repo unless one is given;
    returns its name and whether it carries an Authenticode signature (the
    guest checks that it verifies)."""
    if installer is None:
        assert gui_repo is not None
        log(
            "Building the Windows installer (make package PLATFORM=tauri-windows-cross)..."
        )
        run(["make", "package", "PLATFORM=tauri-windows-cross"], cwd=gui_repo)
        (installer,) = (gui_repo / "out" / "release" / "tauri-windows").glob(
            "*-setup.exe"
        )
    shutil.copy2(installer, serve / installer.name)
    return installer.name, pe_has_signature(installer)


# Builds the Linux packages the way `make package PLATFORM=tauri` does on Linux,
# in an amd64 Ubuntu container (Tauri can't cross-build them). Cargo's registry
# and the build tree persist in Docker volumes between builds.
LINUX_GUI_BUILD = """
set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl ca-certificates git build-essential pkg-config file xdg-utils \
  libwebkit2gtk-4.1-dev libssl-dev libayatana-appindicator3-dev librsvg2-dev >/dev/null
/root/.cargo/bin/cargo --version 2>/dev/null || curl -fsSL https://sh.rustup.rs | sh -s -- -y --profile minimal
. /root/.cargo/env
curl -fsSL https://nodejs.org/dist/v24.22.0/node-v24.22.0-linux-x64.tar.xz | tar -xJ -C /opt
export PATH=/opt/node-v24.22.0-linux-x64/bin:$PATH
mkdir -p /build/src && tar -xzf /out/src.tar.gz -C /build/src
cd /build/src/platform/typescript
npm ci --no-audit --no-fund >/dev/null
# The target volume persists between builds (for speed); an earlier version's
# .deb would still be in the bundle dir.
rm -rf /build/target/release/bundle/deb
CARGO_TARGET_DIR=/build/target TAURI_DIST_BUNDLES=deb npm run tauri:dist
cp /build/target/release/bundle/deb/*.deb /out/
"""


def build_linux_gui_package(
    gui_repo: Path | None, package: Path | None, serve: Path
) -> str:
    """The app's .deb, built from gui_repo unless one is given; returns its name."""
    if package is None:
        assert gui_repo is not None
        log("Building the Linux .deb in an amd64 Ubuntu container (takes a while)...")
        out = serve.parent / "linux-build"
        out.mkdir()
        listed = run(
            ["git", "ls-files", "-co", "--exclude-standard", "-z"],
            cwd=gui_repo, capture_output=True,
        ).stdout.split("\0")  # fmt: skip
        with tarfile.open(out / "src.tar.gz", "w:gz") as tar:
            for rel in filter(None, listed):
                if (gui_repo / rel).exists():
                    tar.add(gui_repo / rel, arcname=rel, recursive=False)
        docker = windows_vm.docker()
        for used in ("docker:gui-build", f"docker:{LINUX_IMAGE}"):
            mark_used(used)
        run([docker, "run", "--rm", "--platform", "linux/amd64",
             "-v", f"{out}:/out", "-v", "wgse-lab-gui-cargo:/root/.cargo",
             "-v", "wgse-lab-gui-rustup:/root/.rustup",
             "-v", "wgse-lab-gui-target:/build/target",
             LINUX_IMAGE, "bash", "-c", LINUX_GUI_BUILD])  # fmt: skip
        (package,) = out.glob("*.deb")
    shutil.copy2(package, serve / package.name)
    return package.name


def pe_has_signature(path: Path) -> bool:
    """Whether a PE file has an Authenticode certificate table."""
    data = path.read_bytes()
    pe = int.from_bytes(data[0x3C:0x40], "little")
    optional = pe + 24
    magic = int.from_bytes(data[optional : optional + 2], "little")
    # The security entry is data directory 4 (directories: PE32 at 96, PE32+ at 112).
    entry = optional + (112 if magic == 0x20B else 96) + 4 * 8
    return int.from_bytes(data[entry + 4 : entry + 8], "little") > 0
