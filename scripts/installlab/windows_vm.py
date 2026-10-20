"""Windows targets for the install lab.

`windows`: a Windows 11 ARM64 VM under QEMU (hvf) on an Apple Silicon host. The
base image is built once by `lab.py prepare windows` from Microsoft's own files;
every run boots a throwaway qcow2 overlay of it, so the base never changes and
nothing survives between runs.

`windows-host`: a brand-new standard user on a disposable GitHub Windows runner,
after hiding the runner's preinstalled MSYS2/conda/Java/etc. (CI only).

Both run guest/windows/scenario.ps1 as the standard user "Lab User" (a space in
every profile path, as on many real PCs) over SSH, and
copy the evidence back for the host's oracle.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

from housekeeping import LAB, lab_key, mark_used

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
WIN_DIR = LAB / "windows"
BASE = WIN_DIR / "base.qcow2"
BASE_VARS = WIN_DIR / "base-vars.fd"
ISO = WIN_DIR / "win11-arm64.iso"
VIRTIO_URL = (
    "https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/"
    "archive-virtio/virtio-win-0.1.302-1/virtio-win-0.1.302.iso"
)
# Fedora publishes no digest for the ISO; pinned from the first download.
VIRTIO_SHA256 = "303f7ae40dad495d6ae474fdc571df58958a4dbc5c37a522d80f9a203867949d"
VIRTIO_ISO = WIN_DIR / "virtio-win-0.1.302.iso"
# UUP dump: Microsoft's Windows Update files (aria2 verifies each one against the
# hash in Microsoft's update metadata), converted to an install ISO.
UUP_API = "https://api.uupdump.net"
UUP_TITLE = re.compile(r"^Windows 11, version \d\dH\d \(\d+\.\d+\)$")
ADMIN = "labadmin"
TESTER = "Lab User"
GUEST_LAB = "C:/lab"


def log(message: str) -> None:
    print(f"[lab {time.strftime('%H:%M:%S')}] {message}", flush=True)


def sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ssh_key() -> Path:
    """The key the Windows images trust. It lives with them in WIN_DIR, so it
    can't drift from them; an image's admin can't rewrite its key files over
    SSH anyway (Windows filters an admin's token on network logons)."""
    key = WIN_DIR / "ssh" / "id_ed25519"
    if not key.exists():
        key.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "wgse-lab", "-f", str(key)],
            check=True,
        )  # fmt: skip
    return key


def ssh_opts(port: int) -> list[str]:
    return [
        "-i", str(ssh_key()), "-p", str(port),
        "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=" + os.devnull,
        "-o", "LogLevel=ERROR", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=30",
    ]  # fmt: skip


def scp_opts(port: int) -> list[str]:
    opts = ssh_opts(port)
    opts[opts.index("-p")] = "-P"
    return opts


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_for_ssh(
    user: str, port: int, timeout: float, alive: subprocess.Popen[bytes] | None = None
) -> None:
    deadline = time.monotonic() + timeout
    rejections = 0
    while time.monotonic() < deadline:
        if alive is not None and alive.poll() is not None:
            raise SystemExit("The VM exited before SSH came up.")
        probe = subprocess.run(
            ["ssh", *ssh_opts(port), "-l", user, "127.0.0.1", "whoami"],
            capture_output=True,
            text=True,
        )
        if probe.returncode == 0:
            return
        if "Permission denied" in probe.stderr:
            rejections += 1
            if rejections >= 3:  # sshd is up; the image doesn't trust this key
                raise SystemExit(
                    f"The Windows image refuses {ssh_key()}: it was built with "
                    "another key. Run `lab.py prepare windows` again."
                )
        time.sleep(10)
    raise SystemExit(f"SSH as {user} did not come up within {timeout:.0f}s")


# --- artifacts ------------------------------------------------------------------


def add_windows_artifacts(serve: Path) -> None:
    """install_windows.bat and a .zip of this tree, as GitHub's release archive
    would contain it (git's eol attributes applied and symlinks kept as
    symlinks, like `git archive`)."""
    shutil.copy2(REPO / "install_windows.bat", serve / "install_windows.bat")
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    files = [
        rel
        for rel in listed
        if rel and ((REPO / rel).is_symlink() or (REPO / rel).is_file())
    ]
    attrs = subprocess.run(
        ["git", "check-attr", "-z", "--stdin", "eol"],
        cwd=REPO,
        input="\0".join(files),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    crlf = {attrs[i] for i in range(0, len(attrs) - 2, 3) if attrs[i + 2] == "crlf"}
    with zipfile.ZipFile(serve / "wgsextract-cli.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for rel in files:
            entry = zipfile.ZipInfo(f"wgsextract-cli-lab/{rel}")
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.create_system = (
                3  # Unix modes in external_attr, as git archive writes
            )
            if (REPO / rel).is_symlink():
                entry.external_attr = 0o120777 << 16
                data = os.readlink(REPO / rel).encode()
            else:
                entry.external_attr = (
                    (REPO / rel).stat().st_mode & 0o777 | 0o100000
                ) << 16
                data = (REPO / rel).read_bytes()
                if rel in crlf:
                    data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            z.writestr(entry, data)


# --- machines -------------------------------------------------------------------


class WindowsMachine:
    """Shared by the VM and the CI host: stage files, run a guest script as the
    standard user over SSH, collect the evidence."""

    serve_host = "127.0.0.1"
    guest_host = "127.0.0.1"
    results_in_guest = "C:\\lab\\results"
    guest_scripts_in_guest = "C:\\lab\\guest"
    installer_name = "install_windows.bat"
    archive_name = "wgsextract-cli.zip"
    admin = ADMIN
    port = 22

    def __init__(self, name: str, results: Path, guest: Path) -> None:
        self.name, self.results, self.guest = name, results, guest
        self.serve = guest.parent / "serve"

    def __enter__(self) -> WindowsMachine:
        add_windows_artifacts(self.serve)
        try:
            self.create()
        except BaseException:
            # A VM that never came up (or was interrupted) must not keep running.
            self.destroy()
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        if os.environ.get("LAB_KEEP"):
            log(f"Keeping machine {self.name} (LAB_KEEP set)")
            return
        self.destroy()

    def create(self) -> None: ...
    def destroy(self) -> None: ...

    def ssh(self, user: str, command: str, **kwargs: object) -> int:
        return subprocess.run(  # type: ignore[call-overload,no-any-return]
            ["ssh", *ssh_opts(self.port), "-l", user, "127.0.0.1", command], **kwargs
        ).returncode

    def admin_ps(self, script: str) -> None:
        # -EncodedCommand sidesteps cmd.exe quoting entirely.

        script = "$ProgressPreference = 'SilentlyContinue'; " + script
        encoded = base64.b64encode(script.encode("utf-16-le")).decode()
        if self.ssh(self.admin, f"powershell -NoProfile -EncodedCommand {encoded}"):
            raise SystemExit(f"admin command failed: {script[:120]}")

    def stage(self) -> None:
        self.admin_ps(
            f"Remove-Item -Recurse -Force {GUEST_LAB} -ErrorAction SilentlyContinue; "
            f"New-Item -ItemType Directory -Force {GUEST_LAB}/results | Out-Null; "
            f"icacls.exe {GUEST_LAB}/results /grant '{TESTER}:(OI)(CI)M' | Out-Null"
        )
        subprocess.run(
            ["scp", *scp_opts(self.port), "-q", "-r", str(self.guest),
             f"{self.admin}@127.0.0.1:{GUEST_LAB}/guest"],
            check=True,
        )  # fmt: skip

    def exec(self, env: dict[str, str], command: str) -> int:
        """Run guest/windows/<command> as Lab User with the LAB_* settings."""
        env = {**env, "LAB_RESULTS": self.results_in_guest}
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "lab-env.json"
            env_file.write_text(json.dumps(env))
            subprocess.run(
                ["scp", *scp_opts(self.port), "-q", str(env_file),
                 f"{self.admin}@127.0.0.1:{GUEST_LAB}/lab-env.json"],
                check=True,
            )  # fmt: skip
        script = f"{self.guest_scripts_in_guest}\\windows\\{command}"
        rc = self.ssh(
            TESTER,
            f"powershell -NoProfile -ExecutionPolicy Bypass -File {script} "
            f"-EnvFile C:\\lab\\lab-env.json",
        )
        self.collect()
        return rc

    def collect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(
                ["scp", *scp_opts(self.port), "-q", "-r",
                 f"{self.admin}@127.0.0.1:{GUEST_LAB}/results", tmp],
                check=True,
            )  # fmt: skip
            shutil.copytree(Path(tmp) / "results", self.results, dirs_exist_ok=True)


def qemu_share() -> Path:
    qemu = shutil.which("qemu-system-aarch64")
    if not qemu:
        raise SystemExit("The windows target needs QEMU (qemu-system-aarch64).")
    return Path(qemu).resolve().parents[1] / "share" / "qemu"


def qemu_command(
    disk: Path, vars_fd: Path, port: int, qmp: Path, cdroms: list[Path]
) -> list[str]:
    cmd = [
        "qemu-system-aarch64",
        "-M", "virt,highmem=on", "-accel", "hvf", "-cpu", "host",
        "-smp", "6", "-m", "8192",
        "-drive", f"if=pflash,format=raw,readonly=on,file={qemu_share() / 'edk2-aarch64-code.fd'}",
        "-drive", f"if=pflash,format=raw,file={vars_fd}",
        "-device", "ramfb", "-device", "qemu-xhci",
        "-device", "usb-kbd", "-device", "usb-tablet",
        "-drive", f"if=none,id=disk0,format=qcow2,file={disk},discard=unmap",
        # Disk first: an empty disk falls through to the install ISO, and after
        # the image is applied the firmware boots Windows, not the ISO again.
        "-device", "nvme,drive=disk0,serial=wgselab0,bootindex=0",
        "-nic", f"user,model=virtio-net-pci,hostfwd=tcp:127.0.0.1:{port}-:22",
        "-qmp", f"unix:{qmp},server=on,wait=off",
        "-display", "none",
    ]  # fmt: skip
    for i, iso in enumerate(cdroms):
        cmd += [
            "-drive", f"if=none,id=cd{i},media=cdrom,readonly=on,file={iso}",
            "-device", f"usb-storage,drive=cd{i},removable=on,bootindex={i + 1}",
        ]  # fmt: skip
    return cmd


def qmp_socket() -> Path:
    # UNIX socket paths must be under 104 bytes on macOS; run dirs are deeper.
    return Path(tempfile.mkdtemp(prefix="wgse-qmp-")) / "qmp.sock"


def qmp_command(qmp: Path, execute: str, **arguments: object) -> None:
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(10)
        s.connect(str(qmp))
        s.recv(65536)
        for message in (
            {"execute": "qmp_capabilities"},
            {"execute": execute, "arguments": arguments},
        ):
            s.sendall(json.dumps(message).encode() + b"\n")
            s.recv(65536)


def screenshot(qmp: Path, out: Path) -> None:
    """The VM's screen, for diagnosing a stuck install."""
    qmp_command(qmp, "screendump", filename=str(out), format="png")


class QemuWindowsMachine(WindowsMachine):
    guest_host = "10.0.2.2"  # QEMU user networking's address for the host
    base, base_vars, prepare_command = BASE, BASE_VARS, "lab.py prepare windows"

    def create(self) -> None:
        if not self.base.exists():
            raise SystemExit(f"Run `{self.prepare_command}` first.")
        mark_used(lab_key(self.base))
        self.workdir = WIN_DIR / "runs" / self.name
        self.workdir.mkdir(parents=True)
        self.disk = self.workdir / "disk.qcow2"
        subprocess.run(
            ["qemu-img", "create", "-q", "-f", "qcow2", "-F", "qcow2", "-b", str(self.base),
             str(self.disk)],
            check=True,
        )  # fmt: skip
        vars_fd = self.workdir / "vars.fd"
        shutil.copy2(self.base_vars, vars_fd)
        os.chmod(vars_fd, 0o644)
        self.port = free_port()
        self.qmp = qmp_socket()
        self.vm = subprocess.Popen(
            qemu_command(self.disk, vars_fd, self.port, self.qmp, []),
            stdout=(self.guest.parent / "qemu.log").open("w"),
            stderr=subprocess.STDOUT,
        )
        log(f"Booting Windows VM {self.name} (ssh port {self.port})...")
        wait_for_ssh(self.admin, self.port, 900, self.vm)
        self.after_boot()
        self.stage()

    def after_boot(self) -> None: ...

    def destroy(self) -> None:
        vm = getattr(self, "vm", None)
        if vm and vm.poll() is None:
            with contextlib.suppress(OSError):
                qmp_command(self.qmp, "quit")
            try:
                vm.wait(timeout=60)
            except subprocess.TimeoutExpired:
                vm.kill()
        if getattr(self, "workdir", None):
            shutil.rmtree(self.workdir, ignore_errors=True)
        if getattr(self, "qmp", None):
            shutil.rmtree(self.qmp.parent, ignore_errors=True)


class WindowsHostMachine(WindowsMachine):
    """A new standard user on a disposable GitHub Windows runner (never a dev PC).

    The runner comes with MSYS2 at C:\\msys64, Git, conda, Java, Python, Strawberry
    Perl and more on the machine PATH. They're hidden before the run, and the
    oracle's tool survey fails the run if any is still reachable.
    """

    def create(self) -> None:
        if not os.environ.get("CI") or sys.platform != "win32":
            raise SystemExit(
                "The windows-host target is only for disposable CI runners."
            )
        self.admin = os.environ["USERNAME"]
        key = ssh_key()
        # OpenSSH refuses a private key other users can read.
        subprocess.run(
            ["icacls", str(key), "/inheritance:r", "/grant:r", f"{self.admin}:F"],
            check=True,
            capture_output=True,
        )
        with tempfile.TemporaryDirectory() as media:
            shutil.copy2(key.with_suffix(".pub"), Path(media) / "lab_ed25519.pub")
            subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                 str(HERE / "vm" / "windows" / "lab-setup.ps1"), "-Media", media],
                check=True, env=windows_powershell_env(),
            )  # fmt: skip
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
             str(HERE / "vm" / "windows" / "deprovision-runner.ps1")],
            check=True, env=windows_powershell_env(),
        )  # fmt: skip
        try:
            wait_for_ssh(self.admin, self.port, 120)
            wait_for_ssh(TESTER, self.port, 120)
        except SystemExit:
            self.ssh_diagnostics()
            raise
        self.stage()

    def ssh_diagnostics(self) -> None:
        """What sshd saw, so a failed CI round says why."""
        log("SSH diagnostics:")
        subprocess.run(
            ["ssh", *ssh_opts(self.port), "-vvv", "-o", "LogLevel=DEBUG3",
             "-l", TESTER, "127.0.0.1", "whoami"],
        )  # fmt: skip
        script = r"""
$ErrorActionPreference = 'Continue'
Get-Command ssh, sshd -All -ErrorAction SilentlyContinue | Format-Table -AutoSize Source | Out-String -Width 300
Get-Service sshd | Format-List Name, Status, StartType, BinaryPathName | Out-String
Get-CimInstance Win32_Service -Filter "Name='sshd'" | Format-List PathName, StartName | Out-String
Get-LocalUser 'Lab User' | Format-List * | Out-String
Get-LocalGroupMember Users | Out-String
Get-Content C:\ProgramData\ssh\sshd_config | Where-Object { $_ -and $_ -notmatch '^\s*#' }
foreach ($f in 'C:\ProgramData\ssh\administrators_authorized_keys', 'C:\ProgramData\ssh\labuser_authorized_keys') {
    "--- $f"; icacls.exe $f; (Get-Acl -LiteralPath $f).Owner
}
Get-ChildItem C:\Users -Force | Format-Table Name | Out-String
Get-WinEvent -LogName OpenSSH/Operational -MaxEvents 60 -ErrorAction SilentlyContinue |
    Sort-Object TimeCreated | Format-Table -Wrap TimeCreated, Message | Out-String -Width 300
Get-ChildItem C:\ProgramData\ssh\logs -ErrorAction SilentlyContinue | Get-Content -Tail 60
"""
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            env=windows_powershell_env(),
        )


# --- base image -----------------------------------------------------------------


def windows_powershell_env() -> dict[str, str]:
    """Environment for Windows PowerShell 5.1 started from PowerShell 7 (GitHub's
    `pwsh` shell): 7's PSModulePath makes 5.1 fail to load its own modules
    (ConvertTo-SecureString "could not be loaded"), so let 5.1 build its own."""
    return {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}


def docker() -> str:
    return (
        shutil.which("docker")
        or "/Applications/OrbStack.app/Contents/MacOS/xbin/docker"
    )


def build_iso() -> None:
    """Windows 11 ARM64 install ISO from Windows Update files (UUP dump).

    Microsoft's own ISO download refuses scripted requests, so build the same
    media from the files Windows Update serves; each is checked against the
    hash in Microsoft's metadata. The conversion runs in a Linux container
    (cabextract, wimlib, chntpw, genisoimage). Its ISO boots without the
    "Press any key to boot from CD" prompt, which would stall a headless VM.
    """
    builds = json.loads(
        subprocess.run(
            ["curl", "-fsSL", f"{UUP_API}/listid.php?search=Windows%2011%20arm64&sortByDate=1"],
            capture_output=True, text=True, check=True,
        ).stdout
    )["response"]["builds"]  # fmt: skip
    items = builds.values() if isinstance(builds, dict) else builds
    build = next(
        b for b in items if b["arch"] == "arm64" and UUP_TITLE.match(b["title"])
    )
    log(f"Building the ISO from {build['title']} ({build['uuid']})...")
    work = WIN_DIR / "uup"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    subprocess.run(
        ["curl", "-fsSL", "-o", str(work / "pack.zip"),
         f"https://uupdump.net/get.php?id={build['uuid']}&pack=en-us&edition=professional",
         "-d", "autodl=2&updates=1&cleanup=1"],
        check=True,
    )  # fmt: skip
    with zipfile.ZipFile(work / "pack.zip") as z:
        z.extractall(work)
    script = (
        "set -e; apt-get update -qq; DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
        "aria2 cabextract wimtools chntpw genisoimage >/dev/null; "
        "sed -i 's/\\r$//' uup_download_linux.sh; "
        # Patch convert.sh after the script downloads it, right before it runs.
        "sed -i 's#^  ./files/convert.sh wim#  sed -i s,efisys.bin,efisys_noprompt.bin, "
        "./files/convert.sh\\n&#' uup_download_linux.sh; "
        "bash uup_download_linux.sh"
    )
    subprocess.run(
        [docker(), "run", "--rm", "-v", f"{work}:/w", "-w", "/w", "ubuntu:24.04",
         "sh", "-c", script],
        check=True,
    )  # fmt: skip
    built = next(p for p in work.iterdir() if p.suffix.lower() == ".iso")
    built.rename(ISO)
    (WIN_DIR / "win11-arm64.json").write_text(
        json.dumps({"build": build, "sha256": sha256(ISO)}, indent=2)
    )
    shutil.rmtree(work)


def fetch_virtio() -> None:
    if VIRTIO_ISO.exists() and sha256(VIRTIO_ISO) == VIRTIO_SHA256:
        return
    log("Downloading the virtio-win drivers (NetKVM for the VM's network card)...")
    subprocess.run(
        ["curl", "-fsSL", "--retry", "3", "-o", str(VIRTIO_ISO), VIRTIO_URL], check=True
    )
    if sha256(VIRTIO_ISO) != VIRTIO_SHA256:
        raise SystemExit(f"virtio-win checksum mismatch: {VIRTIO_ISO}")


def build_unattend_iso(out: Path) -> None:
    """autounattend.xml, the first-logon script, the lab's public key and the
    ARM64 NetKVM driver, on a small ISO that Windows Setup scans."""
    media = out.parent / "unattend-media"
    shutil.rmtree(media, ignore_errors=True)
    media.mkdir()
    for name in ("autounattend.xml", "lab-setup.ps1", "lab-apply.cmd"):
        data = (HERE / "vm" / "windows" / name).read_bytes()
        # Windows tools want CRLF (cmd.exe misparses LF-only batch files).
        (media / name).write_bytes(data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    shutil.copy2(ssh_key().with_suffix(".pub"), media / "lab_ed25519.pub")
    mount = Path(tempfile.mkdtemp(prefix="wgse-virtio-"))
    subprocess.run(
        ["hdiutil", "attach", "-nobrowse", "-readonly", "-mountpoint", str(mount),
         str(VIRTIO_ISO)],
        check=True, capture_output=True,
    )  # fmt: skip
    try:
        # copyfile, not copy2: the ISO's read-only modes would make media
        # undeletable (directory modes are fixed below).
        shutil.copytree(
            mount / "NetKVM" / "w11" / "ARM64",
            media / "drivers" / "NetKVM",
            copy_function=shutil.copyfile,
        )
        for d in (media / "drivers", media / "drivers" / "NetKVM"):
            os.chmod(d, 0o755)
    finally:
        subprocess.run(["hdiutil", "detach", str(mount)], capture_output=True)
        mount.rmdir()
    out.unlink(missing_ok=True)
    subprocess.run(
        ["hdiutil", "makehybrid", "-iso", "-joliet", "-default-volume-name", "LABSETUP",
         "-o", str(out), str(media)],
        check=True, capture_output=True,
    )  # fmt: skip
    shutil.rmtree(media)


def prepare() -> None:
    """Build <lab>/windows/base.qcow2: stock Windows 11 Pro ARM64 plus
    OpenSSH and the lab users (see vm/windows/autounattend.xml)."""
    WIN_DIR.mkdir(parents=True, exist_ok=True)
    if not ISO.exists():
        build_iso()
    fetch_virtio()
    for download in (ISO, VIRTIO_ISO):
        mark_used(lab_key(download))
    unattend = WIN_DIR / "unattend.iso"
    build_unattend_iso(unattend)
    for path in (BASE, BASE_VARS):
        if path.exists():
            os.chmod(path, 0o644)
            path.unlink()
    building = WIN_DIR / "base-building.qcow2"
    vars_fd = WIN_DIR / "base-building-vars.fd"
    subprocess.run(
        ["qemu-img", "create", "-q", "-f", "qcow2", str(building), "80G"], check=True
    )
    shutil.copy2(qemu_share() / "edk2-arm-vars.fd", vars_fd)
    os.chmod(vars_fd, 0o644)
    port = free_port()
    qmp = qmp_socket()
    log(
        f"Installing Windows unattended (ssh port {port}); this takes 30-60 minutes. "
        f"Screen: python3 {Path(__file__)} screenshot {qmp} out.png"
    )
    vm = subprocess.Popen(
        qemu_command(building, vars_fd, port, qmp, [ISO, unattend]),
        stdout=(WIN_DIR / "prepare-qemu.log").open("w"),
        stderr=subprocess.STDOUT,
    )
    try:
        wait_for_ssh(ADMIN, port, 3 * 3600, vm)
        # The first SSH logon creates Lab User's profile, so a run's baseline
        # is a user who has logged in before, not Windows' first-logon setup.
        wait_for_ssh(TESTER, port, 300, vm)
        log("Letting first-boot background work settle for 10 minutes...")
        time.sleep(600)
        subprocess.run(
            ["ssh", *ssh_opts(port), f"{ADMIN}@127.0.0.1", "shutdown /s /t 5"],
            check=True,
        )
        vm.wait(timeout=600)
    except BaseException:
        if vm.poll() is None:
            vm.kill()
        raise
    building.rename(BASE)
    vars_fd.rename(BASE_VARS)
    for path in (BASE, BASE_VARS):
        os.chmod(path, 0o444)  # runs use overlays; the base must never change
    unattend.unlink()
    shutil.rmtree(qmp.parent, ignore_errors=True)
    log(f"{BASE} is ready.")


if __name__ == "__main__":
    # windows_vm.py screenshot <qmp-socket> <out.png>: see a stuck VM's screen.
    if len(sys.argv) == 4 and sys.argv[1] == "screenshot":
        screenshot(Path(sys.argv[2]), Path(sys.argv[3]))
    else:
        raise SystemExit("usage: windows_vm.py screenshot <qmp-socket> <out.png>")
