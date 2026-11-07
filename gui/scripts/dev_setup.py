#!/usr/bin/env python3
"""Shared project setup and smoke checks, after user tool bootstrapping."""

from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOOPBACK_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def run(*args: str) -> None:
    print("+", " ".join(args), flush=True)
    subprocess.run([shutil.which(args[0]) or args[0], *args[1:]], check=True)


def succeeds(*args: str) -> bool:
    executable = shutil.which(args[0])
    return bool(
        executable
        and subprocess.run(
            [executable, *args[1:]], capture_output=True, check=False
        ).returncode
        == 0
    )


def windows_sdk_installed(root: Path, arch: str) -> bool:
    return any(
        (library.parents[2] / "ucrt" / arch / "ucrt.lib").is_file()
        for library in root.glob(f"*/um/{arch}/kernel32.lib")
    )


def desktop_prerequisites() -> list[str]:
    if sys.platform == "linux":
        if not succeeds(
            "pkg-config", "--exists", "webkit2gtk-4.1", "gtk+-3.0", "librsvg-2.0"
        ) or not shutil.which("cc"):
            return [
                "Debian/Ubuntu: sudo apt-get update && sudo apt-get install "
                "build-essential pkg-config libwebkit2gtk-4.1-dev "
                "libayatana-appindicator3-dev librsvg2-dev patchelf",
                "Fedora: sudo dnf install gcc gcc-c++ make pkgconf-pkg-config "
                "webkit2gtk4.1-devel gtk3-devel libappindicator-gtk3-devel "
                "librsvg2-devel patchelf",
            ]
    elif sys.platform == "darwin":
        if not succeeds("xcode-select", "-p") or not succeeds(
            "xcrun", "--find", "clang"
        ):
            return ["macOS: xcode-select --install (accept the installer dialog)."]
    elif sys.platform == "win32":
        vswhere = (
            Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)"))
            / "Microsoft Visual Studio/Installer/vswhere.exe"
        )
        if (
            not vswhere.exists()
            or not subprocess.run(
                [
                    str(vswhere),
                    "-latest",
                    "-products",
                    "*",
                    "-requires",
                    "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                    "-property",
                    "installationPath",
                ],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
        ):
            return [
                "Windows: install Visual Studio Build Tools, selecting "
                "'Desktop development with C++' and a Windows 10/11 SDK: "
                "https://visualstudio.microsoft.com/visual-cpp-build-tools/"
            ]
        kits = (
            Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)"))
            / "Windows Kits/10/Lib"
        )
        arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
        if not windows_sdk_installed(kits, arch):
            return [
                "Windows: use Visual Studio Installer > Modify > Individual components "
                "to install a Windows 10/11 SDK (both UM and UCRT libraries are required)."
            ]
        # WebView2 is a runtime, not an SDK; Windows 11 normally includes it.
        roots = (
            Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")),
            Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))),
        )
        if not any(
            (root / "Microsoft/EdgeWebView/Application").is_dir() for root in roots
        ):
            return [
                "Windows: install the WebView2 Evergreen Runtime: "
                "https://developer.microsoft.com/microsoft-edge/webview2/"
            ]
    return []


def smoke(command: list[str] | None = None) -> None:
    """Exercise the actual server over HTTP, using an ephemeral loopback port."""
    if command is None:
        run("npm", "--prefix", "app", "run", "build")
    log_path = ROOT / "tmp/dev-setup" / ("dev-smoke.log" if command else "smoke.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    smoke_url: str | None = None
    with (
        tempfile.TemporaryDirectory(prefix="smoke-", dir=log_path.parent) as directory,
        log_path.open("w", encoding="utf-8") as log,
    ):
        private_root = Path(directory)
        data_root = private_root / "data"
        data_root.mkdir()
        child_env = {
            **os.environ,
            "GUI_FOR_CLI_APP_SUPPORT_NAME": f"gui-for-cli-{private_root.name}",
        }
        if sys.platform == "win32":
            child_env["LOCALAPPDATA"] = str(data_root)
        elif sys.platform == "darwin":
            child_env["HOME"] = str(data_root)
        else:
            child_env["XDG_DATA_HOME"] = str(data_root)
        process = subprocess.Popen(
            command
            or [
                shutil.which("node") or "node",
                "app/dist/web/src/server/main.js",
                "--bundle",
                "examples/GitHelper",
                "--port",
                "0",
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            env=child_env,
            start_new_session=sys.platform != "win32",
        )
        try:
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(f"Server exited:\n{log_path.read_text()}")
                urls = re.findall(
                    r"Web UI: (http://127\.0\.0\.1:\d+/)", log_path.read_text()
                )
                if urls:
                    url = urls[-1]
                    try:
                        with LOOPBACK_OPENER.open(url, timeout=5) as response:
                            if (
                                response.status == 200
                                and b"<html" in response.read().lower()
                            ):
                                print(
                                    "Smoke passed: GitHelper Web UI returned HTTP 200."
                                )
                                smoke_url = url
                                return
                    except (urllib.error.URLError, TimeoutError):
                        pass
                time.sleep(0.2)
            raise RuntimeError(f"Server did not become ready:\n{log_path.read_text()}")
        finally:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    check=False,
                )
            else:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                if sys.platform != "win32":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                process.wait()
            if smoke_url:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        LOOPBACK_OPENER.open(smoke_url, timeout=1).close()
                    except (urllib.error.URLError, TimeoutError):
                        break
                    time.sleep(0.1)
                else:
                    raise RuntimeError(
                        "Development server remained alive after cleanup."
                    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-smoke", action="store_true")
    parser.add_argument(
        "--web-only",
        action="store_true",
        help="Do not require desktop system prerequisites.",
    )
    args = parser.parse_args()
    os.chdir(ROOT)
    for tool in ("uv", "node", "npm", "cargo", "rustc"):
        run(tool, "--version")
    run(sys.executable, "--version")
    missing = desktop_prerequisites()
    for instruction in missing:
        print(f"Manual prerequisite (setup never elevates): {instruction}", flush=True)
    run("npm", "--prefix", "app", "ci")
    run(
        "cargo",
        "metadata",
        "--locked",
        "--no-deps",
        "--format-version",
        "1",
        "--manifest-path",
        "app/web/packagers/tauri/Cargo.toml",
    )
    if not args.skip_smoke:
        smoke()
    if not succeeds("gh", "auth", "status"):
        print("Advisory: install GitHub CLI and run 'gh auth login' to open PRs.")
    if not any(shutil.which(tool) for tool in ("copilot", "claude", "codex")):
        print(
            "Advisory: optionally install/authenticate Copilot CLI, Claude Code, or Codex."
        )
    print(
        "No sibling wgsextract-cli is needed to develop the UI with examples/GitHelper. "
        "WGSExtract genome actions require its separately documented CLI/runtime setup."
    )
    if missing and not args.web_only:
        print(
            "Web setup succeeded; desktop prerequisites are missing. Install them and rerun, "
            "or use --web-only / -WebOnly for browser-only development."
        )
        return 1
    print(
        "Developer setup complete. Next: make dev BUNDLE=examples/GitHelper "
        "(Windows: powershell -ExecutionPolicy Bypass -File .\\make.ps1 dev BUNDLE=examples/GitHelper)."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (subprocess.CalledProcessError, RuntimeError) as error:
        print(f"Setup failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
