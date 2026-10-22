"""The Windows VM for testing the WGS Extract app (lab.py run windows --gui-*).

An app needs what the CLI doesn't: a signed-in desktop to open its window in,
and the WebView2 runtime that draws it. `lab.py prepare windows-gui` adds both
to the CLI's base image as a second read-only layer (gui-base.qcow2, backed by
base.qcow2, which it never changes):

- The WebView2 Evergreen runtime. Every Windows 11 PC has it (Windows and Edge
  keep it installed); the image built from Windows Update files has neither
  Edge nor WebView2. Pinned by SHA-256.
- Lab User signs in at boot (autologon), as on a PC whose user is signed in.
  Only needed to drive the VM headlessly; the account stays a standard user.

The first interactive sign-in (OneDrive setup, Start menu, Explorer's caches)
happens while preparing, so a run's baseline is a user who has used their PC
before. Each run boots a throwaway overlay of gui-base.qcow2, waits for Lab
User's desktop, and records the screen every minute (results/../screens).
"""

from __future__ import annotations

import contextlib
import os
import platform
import secrets
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import windows_vm
from housekeeping import lab_key, mark_used
from windows_vm import ADMIN, BASE, BASE_VARS, TESTER, WIN_DIR, X64_HOST, log, sha256

# The app's Windows: a VM with the host's architecture, or the runner itself.
GUEST_X64 = X64_HOST or (
    sys.platform == "win32" and platform.machine().lower() in ("amd64", "x86_64")
)
GUI_BASE = WIN_DIR / "gui-base.qcow2"
GUI_BASE_VARS = WIN_DIR / "gui-base-vars.fd"
# The Evergreen standalone installer for the guest's architecture.
WEBVIEW2_URL, WEBVIEW2_SHA256 = (
    (
        "https://msedge.sf.dl.delivery.mp.microsoft.com/filestreamingservice/files/"
        "35871e19-d11b-432d-9d1e-7be0ff286540/MicrosoftEdgeWebView2RuntimeInstallerX64.exe",
        "0625fb5c1e75be58a164e14f95aa58a3e5badb859b3d17c952385f3e4a9ff3e8",
    )
    if GUEST_X64
    else (
        "https://msedge.sf.dl.delivery.mp.microsoft.com/filestreamingservice/files/"
        "a8eefc09-1989-4d69-b834-689bb46de37d/MicrosoftEdgeWebView2RuntimeInstallerARM64.exe",
        "216324c5433da0e82f83c379a171c847eca53f655053d5699e5fef6f5b951d8d",
    )
)
WEBVIEW2 = WIN_DIR / WEBVIEW2_URL.rsplit("/", 1)[1]
WEBVIEW2_SDK_URL = (
    "https://www.nuget.org/api/v2/package/Microsoft.Web.WebView2/1.0.2903.40"
)
WEBVIEW2_SDK_SHA256 = "ef128016dd1e51c59178c827ed5b8aa3322c57afa8675d930f8109505542ad74"
WEBVIEW2_SDK = WIN_DIR / "microsoft.web.webview2.1.0.2903.40.nupkg"
WEBVIEW2_CLIENT = (
    r"HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients"
    r"\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
)
# Lab User's desktop is up: Explorer runs in an interactive session as them.
SESSION_UP = (
    "$e = Get-CimInstance Win32_Process -Filter \"Name='explorer.exe'\" | "
    "Where-Object { $_.SessionId -ne 0 -and "
    f"(Invoke-CimMethod -InputObject $_ -MethodName GetOwner).User -eq '{TESTER}' }}; "
    "if (-not $e) { exit 1 }"
)


def wait_for_session(machine: windows_vm.WindowsMachine, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with contextlib.suppress(SystemExit):
            machine.admin_ps(SESSION_UP)
            return
        time.sleep(10)
    raise SystemExit(f"{TESTER}'s desktop session did not start within {timeout:.0f}s")


def add_webview2_sdk(dest: Path) -> None:
    """The WebView2 SDK's WinForms control (pinned NuGet package), for
    guest/windows/webview2-window.ps1: the control runs' other WebView2 app."""
    WIN_DIR.mkdir(parents=True, exist_ok=True)
    if not WEBVIEW2_SDK.exists() or sha256(WEBVIEW2_SDK) != WEBVIEW2_SDK_SHA256:
        subprocess.run(
            [
                "curl",
                "-fsSL",
                "--retry",
                "3",
                "-o",
                str(WEBVIEW2_SDK),
                WEBVIEW2_SDK_URL,
            ],
            check=True,
        )
    if sha256(WEBVIEW2_SDK) != WEBVIEW2_SDK_SHA256:
        raise SystemExit(f"WebView2 SDK checksum mismatch: {WEBVIEW2_SDK}")
    mark_used(lab_key(WEBVIEW2_SDK))
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(WEBVIEW2_SDK) as package:
        for member in (
            "lib/net462/Microsoft.Web.WebView2.Core.dll",
            "lib/net462/Microsoft.Web.WebView2.WinForms.dll",
            # The guest's PowerShell has the guest's architecture.
            f"runtimes/win-{'x64' if GUEST_X64 else 'arm64'}/native/WebView2Loader.dll",
        ):
            (dest / Path(member).name).write_bytes(package.read(member))


class QemuWindowsGuiMachine(windows_vm.QemuWindowsMachine):
    base, base_vars, prepare_command = (
        GUI_BASE,
        GUI_BASE_VARS,
        "lab.py prepare windows-gui",
    )

    def create(self) -> None:
        add_webview2_sdk(self.guest / "windows" / "webview2-sdk")
        super().create()

    def after_boot(self) -> None:
        wait_for_session(self, 600)
        self.screens = self.guest.parent / "screens"
        self.screens.mkdir(exist_ok=True)
        self.recording = threading.Event()
        threading.Thread(target=self.record_screen, daemon=True).start()

    def record_screen(self) -> None:
        """The VM's screen once a minute: what the user would have seen."""
        while not self.recording.wait(60):
            with contextlib.suppress(OSError):
                windows_vm.screenshot(
                    self.qmp, self.screens / f"{time.strftime('%H%M%S')}.png"
                )

    def destroy(self) -> None:
        if getattr(self, "recording", None):
            self.recording.set()
        super().destroy()


class WindowsHostGuiMachine(windows_vm.WindowsHostMachine):
    """A new standard user's desktop on a disposable GitHub Windows runner.

    The CLI's runner machine (a new standard user, the runner's tools hidden),
    plus what an app needs: Lab User signed in to a desktop session of their
    own, and the WebView2 runtime. A runner can't reboot into autologon, but
    it is Windows Server, which hosts a Remote Desktop session beside the
    runner's own, so Lab User signs in over Remote Desktop to 127.0.0.2 (the
    client refuses 127.0.0.1, its own console). The account joins Remote
    Desktop Users for that; it stays a standard user. Lab User's first
    sign-in settles before the baseline, as in the VM's prepared image.
    """

    def create(self) -> None:
        add_webview2_sdk(self.guest / "windows" / "webview2-sdk")
        super().create()
        self.ensure_webview2()
        self.sign_in()
        self.screens = self.guest.parent / "screens"
        self.screens.mkdir(exist_ok=True)
        self.recording = threading.Event()
        threading.Thread(target=self.record_screen, daemon=True).start()
        log(f"Letting {TESTER}'s first sign-in settle for 3 minutes...")
        time.sleep(180)

    def ensure_webview2(self) -> None:
        """Every Windows 11 PC has WebView2; the Server image may lack it."""
        has_it = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"if (-not (Get-ItemProperty '{WEBVIEW2_CLIENT}' -ErrorAction SilentlyContinue).pv) {{ exit 1 }}"],
            env=windows_vm.windows_powershell_env(),
        ).returncode == 0  # fmt: skip
        if has_it:
            log("The runner has the WebView2 runtime.")
            return
        fetch_webview2()
        log("Installing the WebView2 runtime...")
        subprocess.run([str(WEBVIEW2), "/silent", "/install"], check=True)

    def sign_in(self) -> None:
        password = f"Lab-{secrets.token_hex(8)}aA1!"
        user = f"{os.environ['COMPUTERNAME']}\\{TESTER}"
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", f"""
$ErrorActionPreference = 'Stop'
net user '{TESTER}' '{password}' | Out-Null
Add-LocalGroupMember -Group 'Remote Desktop Users' -Member '{TESTER}'
Set-ItemProperty 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Terminal Server' fDenyTSConnections 0
Set-Service TermService -StartupType Manual
Start-Service TermService
# The client's certificate prompt for the server: it's this machine.
$client = 'HKCU:\\Software\\Microsoft\\Terminal Server Client'
New-Item -Path $client -Force | Out-Null
Set-ItemProperty $client AuthenticationLevelOverride 0 -Type DWord
"""],
            check=True, env=windows_vm.windows_powershell_env(),
        )  # fmt: skip
        subprocess.run(
            ["cmdkey", "/generic:TERMSRV/127.0.0.2", f"/user:{user}", f"/pass:{password}"],
            check=True, capture_output=True,
        )  # fmt: skip
        log(f"Signing {TESTER} in over Remote Desktop...")
        self.client = subprocess.Popen(["mstsc", "/v:127.0.0.2", "/w:1280", "/h:800"])
        try:
            wait_for_session(self, 600)
        except SystemExit:
            self.screenshot("sign-in-failed")
            subprocess.run(["query", "session"])
            raise

    def screenshot(self, name: str) -> None:
        """The runner's own screen, where the Remote Desktop window shows Lab
        User's desktop."""
        path = (
            self.screens / f"{name}.png"
            if hasattr(self, "screens")
            else (self.results.parent / f"{name}.png")
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", f"""
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
$b = [System.Windows.Forms.SystemInformation]::VirtualScreen
$bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height
[System.Drawing.Graphics]::FromImage($bmp).CopyFromScreen($b.Left, $b.Top, 0, 0, $bmp.Size)
$bmp.Save('{path}')
"""],
            env=windows_vm.windows_powershell_env(),
        )  # fmt: skip

    def record_screen(self) -> None:
        while not self.recording.wait(60):
            self.screenshot(time.strftime("%H%M%S"))

    def destroy(self) -> None:
        if getattr(self, "recording", None):
            self.recording.set()
        if getattr(self, "client", None):
            self.client.kill()


def fetch_webview2() -> None:
    if WEBVIEW2.exists() and sha256(WEBVIEW2) == WEBVIEW2_SHA256:
        return
    WIN_DIR.mkdir(parents=True, exist_ok=True)
    log("Downloading the WebView2 runtime installer...")
    subprocess.run(
        ["curl", "-fsSL", "--retry", "3", "-o", str(WEBVIEW2), WEBVIEW2_URL], check=True
    )
    if sha256(WEBVIEW2) != WEBVIEW2_SHA256:
        raise SystemExit(f"WebView2 installer checksum mismatch: {WEBVIEW2}")


def prepare_gui() -> None:
    """Build gui-base.qcow2 on top of base.qcow2 (see the module docstring)."""
    if not BASE.exists():
        raise SystemExit("Run `lab.py prepare windows` first.")
    fetch_webview2()
    for used in (BASE, WEBVIEW2):
        mark_used(lab_key(used))
    building = WIN_DIR / "gui-base-building.qcow2"
    vars_fd = WIN_DIR / "gui-base-building-vars.fd"
    for path in (GUI_BASE, GUI_BASE_VARS, building, vars_fd):
        if path.exists():
            os.chmod(path, 0o644)
            path.unlink()
    # A relative backing file: resolved next to the overlay, wherever WIN_DIR lives.
    subprocess.run(
        ["qemu-img", "create", "-q", "-f", "qcow2", "-F", "qcow2", "-b", BASE.name,
         str(building)],
        check=True,
    )  # fmt: skip
    shutil.copy2(BASE_VARS, vars_fd)
    os.chmod(vars_fd, 0o644)
    port = windows_vm.free_port()
    qmp = windows_vm.qmp_socket()
    vm = subprocess.Popen(
        windows_vm.qemu_command(building, vars_fd, port, qmp, []),
        stdout=(WIN_DIR / "prepare-gui-qemu.log").open("w"),
        stderr=subprocess.STDOUT,
    )
    machine = windows_vm.WindowsMachine("gui-base", WIN_DIR, WIN_DIR)
    machine.port = port
    try:
        windows_vm.wait_for_ssh(ADMIN, port, 900, vm)
        subprocess.run(
            ["scp", *windows_vm.scp_opts(port), "-q", str(WEBVIEW2),
             f"{ADMIN}@127.0.0.1:C:/Windows/Temp/webview2.exe"],
            check=True,
        )  # fmt: skip
        log("Installing the WebView2 runtime...")
        machine.admin_ps(
            "$p = Start-Process C:\\Windows\\Temp\\webview2.exe -ArgumentList '/silent /install' "
            "-Wait -PassThru; Remove-Item C:\\Windows\\Temp\\webview2.exe; "
            f"if ($p.ExitCode -ne 0 -or -not (Get-ItemProperty '{WEBVIEW2_CLIENT}').pv) "
            "{ exit 1 }"
        )
        log(f"Signing {TESTER} in at boot...")
        # Windows 11 turns autologon back off (and clears its password) while
        # passwordless sign-in is required, and that setting applies at boot.
        machine.admin_ps(
            "Set-ItemProperty 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\PasswordLess"
            "\\Device' DevicePasswordLessBuildVersion 0; shutdown /r /t 5"
        )
        time.sleep(60)
        windows_vm.wait_for_ssh(ADMIN, port, 900, vm)
        password = f"Lab-{secrets.token_hex(8)}aA1!"
        winlogon = r"HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon"
        machine.admin_ps(
            f"net user '{TESTER}' '{password}' | Out-Null; "
            f"Set-ItemProperty '{winlogon}' AutoAdminLogon '1'; "
            f"Set-ItemProperty '{winlogon}' DefaultUserName '{TESTER}'; "
            f"Set-ItemProperty '{winlogon}' DefaultDomainName '.'; "
            f"Set-ItemProperty '{winlogon}' DefaultPassword '{password}'; "
            "shutdown /r /t 5"
        )
        time.sleep(60)
        windows_vm.wait_for_ssh(ADMIN, port, 900, vm)
        wait_for_session(machine, 900)
        log("Letting the first desktop sign-in settle for 10 minutes...")
        time.sleep(600)
        # The first sign-in leaves the Start menu open.
        windows_vm.qmp_command(qmp, "send-key", keys=[{"type": "qcode", "data": "esc"}])
        windows_vm.screenshot(qmp, WIN_DIR / "gui-base.png")
        machine.admin_ps("shutdown /s /t 5")
        vm.wait(timeout=600)
    except BaseException:
        if vm.poll() is None:
            vm.kill()
        raise
    finally:
        shutil.rmtree(qmp.parent, ignore_errors=True)
    building.rename(GUI_BASE)
    vars_fd.rename(GUI_BASE_VARS)
    for path in (GUI_BASE, GUI_BASE_VARS):
        os.chmod(path, 0o444)
    log(f"{GUI_BASE} is ready (screen: {WIN_DIR / 'gui-base.png'}).")
