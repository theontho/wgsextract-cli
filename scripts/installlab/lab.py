#!/usr/bin/env python3
"""Install lab: test the real install -> use -> update -> uninstall cycle on a
machine that has never seen WGS Extract, as many times as you like.

Every run starts from a fresh clone of a clean image and is destroyed afterwards,
so nothing installed by a previous run (or by the developer) can make a run pass.
The guest only collects evidence; this host process judges it (see oracle.py).

  lab.py prepare macos          one-time: build the clean macOS base image
  lab.py run linux|macos        one full scenario run, then a verdict
  lab.py run macos --mode control   measure background OS noise for the state check
  lab.py run linux --sabotage fake-launcher   negative control: must FAIL
  lab.py selftest linux         run every sabotage; each one must FAIL
  lab.py judge <results-dir>    re-judge collected evidence
  lab.py run macos --gui-repo <path>   the macOS app instead of the CLI: build its
                                DMG, drag-install it, let it run its own setup,
                                run real actions, uninstall from the app
  lab.py prepare windows-gui    one-time: add WebView2 and a signed-in desktop
  lab.py run windows --gui-repo <path>   the Windows app (or --gui-installer <exe>)
  lab.py run linux --gui-repo <path>     the Linux app (or --gui-installer <deb>)
  lab.py mutate <results-dir>   corrupted copies of a passing run must FAIL
  lab.py gc [--dry-run]         remove expired runs, images and leftovers (run,
                                prepare and selftest do this when they finish)

Targets: `macos` (Tart VM from a vanilla macOS image + Command Line Tools),
`linux` (fresh ubuntu:24.04 amd64 container + curl/git), `host` (the current
machine, for disposable CI runners only).
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import getpass
import hashlib
import http.server
import json
import os
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
import housekeeping  # noqa: E402
import macos_policy  # noqa: E402
import windows_gui_vm  # noqa: E402
import windows_vm  # noqa: E402
from gui_build import (  # noqa: E402
    build_gui_artifacts,
    build_linux_gui_package,
    build_windows_gui_installer,
)
from gui_oracle import GuiOracle  # noqa: E402
from oracle import Oracle, find_tools  # noqa: E402

DATASET_URL = (
    "https://github.com/theontho/wgsextract-cli/releases/download/v0.1.0/"
    "wgsextract-benchmark-hg19-mini.zip"
)
DATASET_SHA256 = "ad0f8070dc5ca35c4a6de540493a81df082d160417f747ae68d9c098c110a9f6"
MACOS_VANILLA = "ghcr.io/cirruslabs/macos-tahoe-vanilla:latest"
MACOS_BASE = "wgse-lab-macos-base"
LINUX_IMAGE = "ubuntu:24.04"
SABOTAGES = {
    # name: checks that must fail when the sabotage is applied
    # The installer "succeeds" without doing anything, so step.install passes;
    # the CLI that isn't there must give it away.
    "noop-installer": ["workload.help", "output.help"],
    "fake-launcher": ["output.help", "output.mt_haplogroup"],
    "break-samtools": ["workload.mt_bam"],
    "leftover": ["state.uninstall_restores_baseline"],
    "skip-uninstall": ["step.uninstall", "state.uninstall_restores_baseline"],
}
GENERIC_NOISE = [
    "*/.DS_Store",
    "*/Library/Caches/com.apple.*",
    "*/Library/Logs/*",
    "*/.cache/fontconfig*",
]


def log(message: str) -> None:
    print(f"[lab {time.strftime('%H:%M:%S')}] {message}", flush=True)


def run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    result: subprocess.CompletedProcess[str] = subprocess.run(  # type: ignore[call-overload]
        cmd, text=True, check=True, **kwargs
    )
    return result


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --- artifacts ------------------------------------------------------------------


def build_artifacts(work: Path) -> dict[str, Path]:
    """install.sh, a source archive of this tree, the guest scripts and the dataset."""
    serve = work / "serve"
    serve.mkdir(parents=True)
    shutil.copy2(REPO / "install.sh", serve / "install.sh")

    # The tree under test, as a release archive would contain it.
    listed = run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=REPO,
        capture_output=True,
    ).stdout.split("\0")
    with tarfile.open(serve / "wgsextract-cli.tar.gz", "w:gz") as tar:
        for rel in filter(None, listed):
            source = REPO / rel
            if source.exists() or source.is_symlink():
                tar.add(source, arcname=f"wgsextract-cli-lab/{rel}", recursive=False)

    dataset = fetch_dataset(housekeeping.LAB / "cache")
    shutil.copy2(dataset, serve / "dataset.tar.gz")

    guest = work / "guest"
    shutil.copytree(HERE / "guest", guest)
    shutil.copy2(HERE / "gui" / "wgsextract-plan.json", guest / "gui-plan.json")
    return {"serve": serve, "guest": guest, "dataset": dataset}


def fetch_dataset(cache: Path) -> Path:
    """Download (once) and verify the mini genome; repackage as tar.gz for guests
    without unzip."""
    cache.mkdir(parents=True, exist_ok=True)
    housekeeping.mark_used(housekeeping.lab_key(cache))
    archive = cache / "dataset.tar.gz"
    if archive.exists():
        return archive
    zipped = cache / "wgsextract-benchmark-hg19-mini.zip"
    if not zipped.exists() or sha256(zipped) != DATASET_SHA256:
        log("Downloading the HG00096 mini genome dataset...")
        run(["curl", "-fsSL", "-o", str(zipped), DATASET_URL])
    if sha256(zipped) != DATASET_SHA256:
        raise SystemExit(f"Dataset checksum mismatch for {zipped}")
    unpacked = cache / "unpacked"
    shutil.rmtree(unpacked, ignore_errors=True)
    with zipfile.ZipFile(zipped) as z:
        z.extractall(unpacked)
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(unpacked / "hg19-mini-hg00096", arcname="hg19-mini-hg00096")
    return archive


def dataset_dir() -> Path:
    fetch_dataset(housekeeping.LAB / "cache")
    return housekeeping.LAB / "cache" / "unpacked" / "hg19-mini-hg00096"


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


@contextlib.contextmanager
def http_server(directory: Path, host: str) -> Iterator[int]:
    """Serve the artifacts over HTTP, so the guest downloads them like a user."""
    handler = functools.partial(_QuietHandler, directory=str(directory))
    server = http.server.ThreadingHTTPServer((host, 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()


# --- machines -------------------------------------------------------------------


class Machine:
    """A disposable machine: created clean, destroyed after the run."""

    serve_host = "127.0.0.1"  # where the host's artifact server listens
    guest_host = "127.0.0.1"  # how the guest reaches it
    results_in_guest = ""
    guest_scripts_in_guest = ""
    # Packages beyond the documented prerequisites, e.g. what an earlier
    # release required of the user it is being updated from.
    prerequisites: tuple[str, ...] = ()

    def __init__(self, name: str, results: Path, guest: Path) -> None:
        self.name, self.results, self.guest = name, results, guest

    def __enter__(self) -> Machine:
        self.create()
        return self

    def __exit__(self, *exc: object) -> None:
        if os.environ.get("LAB_KEEP"):
            log(f"Keeping machine {self.name} (LAB_KEEP set)")
            return
        self.destroy()

    def create(self) -> None: ...
    def destroy(self) -> None: ...
    def exec(self, env: dict[str, str], command: str) -> int:
        raise NotImplementedError


SSH_KEY = housekeeping.LAB / "ssh" / "id_ed25519"
SSH_OPTS = [
    "-i", str(SSH_KEY),
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=5",
]  # fmt: skip


class KeyRejected(Exception):
    """The VM answers SSH but doesn't trust the lab's key."""


def boot_vm(name: str, dirs: list[str]) -> tuple[subprocess.Popen[bytes], str]:
    """Start a Tart VM headless; return the process and its IP once SSH answers.

    Raises KeyRejected (with the VM stopped) if it refuses the lab's key.
    """
    vm = subprocess.Popen(
        ["tart", "run", name, "--no-graphics", *(f"--dir={d}" for d in dirs)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 600
    rejections = 0
    while time.monotonic() < deadline:
        ip = subprocess.run(["tart", "ip", name], capture_output=True, text=True)
        if ip.returncode == 0 and ip.stdout.strip():
            address = ip.stdout.strip()
            probe = subprocess.run(
                ["ssh", *SSH_OPTS, f"admin@{address}", "true"],
                capture_output=True,
                text=True,
            )
            if probe.returncode == 0:
                return vm, address
            if "Permission denied" in probe.stderr:
                rejections += 1
                if rejections >= 3:  # sshd is up; the key is the problem
                    stop_vm(name, vm)
                    raise KeyRejected(name)
        time.sleep(3)
    vm.terminate()
    raise SystemExit(f"VM {name} did not accept SSH")


def stop_vm(name: str, vm: subprocess.Popen[bytes]) -> None:
    subprocess.run(["tart", "stop", name], capture_output=True)
    vm.wait(timeout=300)


def authorize_lab_key(ip: str) -> None:
    """Add the lab's public key to admin's authorized_keys over password login
    (the vanilla image's admin/admin), with macOS's built-in expect."""
    pub = SSH_KEY.with_suffix(".pub").read_text().strip()
    expect = f"""
set timeout 300
spawn ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o PubkeyAuthentication=no admin@{ip} "mkdir -p ~/.ssh && chmod 700 ~/.ssh && grep -qxF '{pub}' ~/.ssh/authorized_keys 2>/dev/null || echo '{pub}' >> ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys; sync"
expect {{
    "assword:" {{ send "admin\\r"; exp_continue }}
    "Connection refused" {{ sleep 3; exit 2 }}
    eof
}}
"""
    for _ in range(60):
        if subprocess.run(["/usr/bin/expect", "-c", expect]).returncode == 0 and (
            subprocess.run(["ssh", *SSH_OPTS, f"admin@{ip}", "true"]).returncode == 0
        ):
            return
        time.sleep(5)
    raise SystemExit(f"Could not authorize the lab SSH key on {ip}.")


def ssh_retry(ip: str, command: str, attempts: int = 5) -> None:
    """Run an idempotent command in a just-booted VM, whose SSH may still drop
    (or drop as the command changes its network settings)."""
    for attempt in range(1, attempts + 1):
        if subprocess.run(["ssh", *SSH_OPTS, f"admin@{ip}", command]).returncode == 0:
            return
        if attempt < attempts:
            time.sleep(10)
    raise SystemExit(f"Failed in the VM at {ip}: {command[:80]}")


def repair_base_key() -> None:
    """Make the macOS base trust the lab's current key, in about a minute
    instead of rebuilding it: the key changes when the lab's state moves."""
    log(f"{MACOS_BASE} doesn't trust {SSH_KEY}; authorizing it there...")
    vm = subprocess.Popen(
        ["tart", "run", MACOS_BASE, "--no-graphics"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        ip = ""
        for _ in range(200):
            result = subprocess.run(
                ["tart", "ip", MACOS_BASE], capture_output=True, text=True
            )
            if result.returncode == 0 and result.stdout.strip():
                ip = result.stdout.strip()
                break
            time.sleep(3)
        if not ip:
            raise SystemExit(f"{MACOS_BASE} never got an IP address.")
        authorize_lab_key(ip)
    finally:
        stop_vm(MACOS_BASE, vm)


class TartMachine(Machine):
    serve_host = "192.168.64.1"
    guest_host = "192.168.64.1"
    results_in_guest = "/Volumes/My Shared Files/results"
    guest_scripts_in_guest = "/Volumes/My Shared Files/guest"

    def create(self) -> None:
        dirs = [f"results:{self.results}", f"guest:{self.guest}:ro"]
        run(["tart", "clone", MACOS_BASE, self.name])
        try:
            self.vm, self.ip = boot_vm(self.name, dirs)
        except KeyRejected:
            subprocess.run(["tart", "delete", self.name], capture_output=True)
            repair_base_key()
            run(["tart", "clone", MACOS_BASE, self.name])
            self.vm, self.ip = boot_vm(self.name, dirs)
        with contextlib.ExitStack() as cleanup:
            cleanup.callback(self.destroy)
            policy = macos_policy.check(["ssh", *SSH_OPTS, f"admin@{self.ip}"])
            (self.results / "gatekeeper-policy.json").write_text(policy.stdout)
            if policy.returncode:
                raise SystemExit(
                    "The macOS base lacks the standard Gatekeeper policy. Run "
                    "'pixi run -e dev python scripts/installlab/lab.py prepare macos "
                    "--repair-policy' before testing."
                )
            cleanup.pop_all()

    def destroy(self) -> None:
        subprocess.run(["tart", "stop", self.name], capture_output=True)
        if getattr(self, "vm", None):
            self.vm.wait(timeout=120)
        subprocess.run(["tart", "delete", self.name], capture_output=True)

    def exec(self, env: dict[str, str], command: str) -> int:
        assignments = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
        # A login shell, so the guest sees a normal user's environment.
        remote = f"/bin/zsh -l -c {shlex.quote(f'{assignments} {command}')}"
        return subprocess.run(["ssh", *SSH_OPTS, f"admin@{self.ip}", remote]).returncode


class DockerMachine(Machine):
    serve_host = "127.0.0.1"
    guest_host = "host.docker.internal"
    results_in_guest = "/results"
    guest_scripts_in_guest = "/guest"

    def __init__(self, name: str, results: Path, guest: Path) -> None:
        super().__init__(name, results, guest)
        self.docker = shutil.which("docker") or (
            "/Applications/OrbStack.app/Contents/MacOS/xbin/docker"
        )

    def create(self) -> None:
        housekeeping.mark_used(f"docker:{LINUX_IMAGE}")
        if sys.platform.startswith("linux"):
            # Docker Desktop and OrbStack forward host.docker.internal to the
            # host's loopback; plain Linux Docker maps it to the bridge gateway,
            # so serve there (reachable from containers, not from the network).
            self.serve_host = subprocess.run(
                [self.docker, "network", "inspect", "bridge", "--format",
                 "{{(index .IPAM.Config 0).Gateway}}"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()  # fmt: skip
        run(
            [
                self.docker,
                "run",
                "-d",
                "--name",
                self.name,
                "--platform",
                "linux/amd64",
                "--add-host",
                "host.docker.internal:host-gateway",
                "-v",
                f"{self.results}:/results",
                "-v",
                f"{self.guest}:/guest:ro",
                LINUX_IMAGE,
                "sleep",
                "infinity",
            ],  # fmt: skip
            stdout=subprocess.DEVNULL,
        )
        # The documented prerequisites a fresh Ubuntu user must install, and a
        # normal (non-root) user account to install as.
        run(
            [
                self.docker,
                "exec",
                self.name,
                "sh",
                "-c",
                "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install "
                f"-y -qq curl ca-certificates {' '.join(self.prerequisites)} >/dev/null "
                "&& useradd -m -s /bin/bash tester && chown tester /results"
                + (
                    # An Ubuntu desktop's first user, who may install packages.
                    " && echo 'tester ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/tester"
                    " && chmod 440 /etc/sudoers.d/tester"
                    if "sudo" in self.prerequisites
                    else ""
                ),
            ]  # fmt: skip
        )

    def destroy(self) -> None:
        try:
            if sys.platform.startswith("linux"):
                # The container's users own what they wrote to /results; give
                # it back to the host user (Docker Desktop and OrbStack map
                # ownership), or the host can't write its verdict there.
                chown = subprocess.run(
                    [self.docker, "exec", self.name, "chown", "-R",
                     f"{os.getuid()}:{os.getgid()}", "/results"],
                    capture_output=True, text=True,
                )  # fmt: skip
                if chown.returncode:
                    log(f"could not take back /results: {chown.stderr.strip()}")
        finally:
            subprocess.run([self.docker, "rm", "-f", self.name], capture_output=True)

    def exec(self, env: dict[str, str], command: str) -> int:
        flags = [f"--env={k}={v}" for k, v in env.items()]
        return subprocess.run(
            [
                self.docker,
                "exec",
                "-u",
                "tester",
                "-w",
                "/home/tester",
                *flags,
                self.name,
                "bash",
                "-lc",
                command,
            ]  # fmt: skip
        ).returncode


# What a GitHub macOS runner adds that a user's Mac doesn't have. The image's
# Homebrew and JDKs reach every account (/etc/paths.d, the /usr/bin/java stub);
# moved aside on the throwaway runner, the oracle's tool survey still fails the
# run if a preinstalled tool leaks through.
MACOS_RUNNER_EXTRAS = (
    "/opt/homebrew",
    "/usr/local/Homebrew",
    "/Library/Java/JavaVirtualMachines",
)


def deprovision_macos_runner() -> None:
    """Make a disposable macOS runner look like a user's Mac before a run."""
    for extra in MACOS_RUNNER_EXTRAS:
        if os.path.isdir(extra):
            run(["sudo", "mv", extra, f"{extra}.lab-hidden"])
    # The runner image turns Gatekeeper off; a user's Mac has it on. Every macOS
    # run records its status and the oracle fails a run that had it off.
    run(["sudo", "spctl", "--global-enable"])


class HostMachine(Machine):
    """A brand-new user account on a disposable CI runner (never a dev machine).

    GitHub's macOS runners can't nest VMs, and they come with Homebrew, Python,
    Java and more. A fresh user with only the system PATH can't see any of that
    unless the installer wrongly depends on it.
    """

    user = "wgselab"

    @property
    def home(self) -> str:
        return (
            f"/Users/{self.user}" if sys.platform == "darwin" else f"/home/{self.user}"
        )

    def create(self) -> None:
        if not os.environ.get("CI"):
            raise SystemExit("The host target is only for disposable CI runners.")
        self.results_in_guest = str(self.results)
        self.guest_scripts_in_guest = str(self.guest)
        password = secrets.token_hex(16)
        if sys.platform == "darwin":
            deprovision_macos_runner()
            run(["sudo", "sysadminctl", "-addUser", self.user, "-password", password,
                 "-home", self.home])  # fmt: skip
            run(["sudo", "createhomedir", "-c", "-u", self.user], capture_output=True)
        else:
            run(["sudo", "useradd", "-m", "-s", "/bin/bash", self.user])
        self.share_results()

    def share_results(self) -> None:
        """The results folder must be writable by the user; nothing else is shared."""
        os.chmod(self.results, 0o777)
        for parent in self.results.parents:
            mode = os.stat(parent).st_mode
            if mode & 0o001:
                continue
            if os.stat(parent).st_uid != os.getuid():
                raise SystemExit(f"{parent} isn't traversable by other users.")
            os.chmod(parent, mode | 0o011)

    def destroy(self) -> None:
        # selftest runs one scenario after another on the same runner.
        if sys.platform == "darwin":
            subprocess.run(["sudo", "sysadminctl", "-deleteUser", self.user])
            subprocess.run(["sudo", "rm", "-rf", self.home])
        else:
            subprocess.run(["sudo", "userdel", "-r", self.user])

    def run_as(self) -> list[str]:
        return ["sudo", "-u", self.user]

    def exec(self, env: dict[str, str], command: str) -> int:
        shell = "/bin/zsh" if sys.platform == "darwin" else "/bin/bash"
        clean_env = [
            f"HOME={self.home}",
            f"USER={self.user}",
            "PATH=/usr/bin:/bin:/usr/sbin:/sbin",
            *(f"{k}={v}" for k, v in env.items()),
        ]
        return subprocess.run(
            [*self.run_as(), "env", "-i", *clean_env, shell, "-l", "-c", command],
            cwd="/",
        ).returncode


class HostGuiMachine(HostMachine):
    """The signed-in account of a disposable macOS CI runner, for the app.

    An app needs a desktop session, and a runner can't sign a new account in
    (that takes the login window or a reboot), so the app scenario runs as the
    account signed in at the runner's console, with the runner's tools hidden
    (deprovision_macos_runner). Launch Services gives the app launchd's
    environment, as on any Mac, and the run is judged against this account's own
    baseline: the tool survey, the state diffs and the uninstall check are the
    same as for a new account. The workflow installs the oracle's tools only
    after the scenario, so nothing the lab uses is in this account's home.
    """

    def __init__(self, name: str, results: Path, guest: Path) -> None:
        super().__init__(name, results, guest)
        self.user = getpass.getuser()

    @property
    def home(self) -> str:
        return str(Path.home())

    def create(self) -> None:
        if not os.environ.get("CI") or sys.platform != "darwin":
            raise SystemExit(
                "The host app target is only for disposable macOS runners."
            )
        self.results_in_guest = str(self.results)
        self.guest_scripts_in_guest = str(self.guest)
        deprovision_macos_runner()

    def destroy(self) -> None: ...

    def run_as(self) -> list[str]:
        return []


MACHINES: dict[str, type[Machine] | type[windows_vm.WindowsMachine]] = {
    "macos": TartMachine,
    "linux": DockerMachine,
    "host": HostMachine,
    "windows": windows_vm.QemuWindowsMachine,
    "windows-host": windows_vm.WindowsHostMachine,
}


# --- commands -------------------------------------------------------------------


def noise_patterns(target: str, product: str = "cli") -> list[str]:
    names = [target]
    if product == "gui":
        # An app's machine has a desktop, with its own background activity.
        names.append(f"{target}-gui")
    if target == "host" and sys.platform == "darwin":
        # A fresh user on a macOS runner sees the same Apple background activity.
        names.append("macos")
    patterns = list(GENERIC_NOISE)
    for name in names:
        noise_file = HERE / "noise" / f"{name}.txt"
        if noise_file.is_file():
            patterns += [
                line.strip()
                for line in noise_file.read_text().splitlines()
                if line.strip() and not line.startswith("#")
            ]
    return patterns


def judge(results: Path, target: str) -> bool:
    meta = json.loads((results / "lab-run.json").read_text())
    if meta.get("mode") == "control":
        return report_control(results, target)
    if meta.get("product") == "gui":
        oracle: Oracle = GuiOracle(
            results, meta["nonce"], find_tools(), signed=meta["signed"]
        )
    else:
        oracle = Oracle(results, meta["nonce"], find_tools())
        if meta.get("install_dir"):
            oracle.install_marker = f"/{meta['install_dir']}"
    on_mac = target == "macos" or (
        target == "host" and meta.get("host_platform") == "darwin"
    )
    if on_mac and meta.get("product") != "gui":
        oracle.check_gatekeeper_enabled()
    checks = oracle.run(
        dataset_dir(), noise_patterns(target, meta.get("product", "cli"))
    )
    failed = [c for c in checks if not c.ok]
    for check in checks:
        mark = "PASS" if check.ok else "FAIL"
        print(f"  {mark}  {check.name:38} {check.detail}".rstrip())
    report = {
        **meta,
        "passed": not failed,
        "checks": [c.__dict__ for c in checks],
    }
    (results / "verdict.json").write_text(json.dumps(report, indent=2))
    print(
        f"\nVERDICT: {'PASS' if not failed else 'FAIL'} "
        f"({len(checks) - len(failed)}/{len(checks)} checks passed) -> {results}"
    )
    return not failed


def report_control(results: Path, target: str) -> bool:
    """Print what changed on an untouched machine: candidates for noise/<target>.txt."""
    base = set((results / "inventory-baseline.txt").read_text().splitlines())
    final = set((results / "inventory-after_uninstall.txt").read_text().splitlines())
    changed = sorted((final - base) | (base - final))
    print(f"Control run: {len(changed)} paths changed with nothing installed:")
    for line in changed:
        print("  " + line)
    return True


# Releases whose installer required git (it fetched Yleaf from its repository).
RELEASES_NEEDING_GIT = {f"v0.3.{n}" for n in range(14)} | {"v0.4.0"}


def run_scenario(
    target: str,
    mode: str,
    sabotage: str,
    out: Path | None,
    gui: argparse.Namespace | None = None,
    install_dir: str | None = None,
    update_from: str | None = None,
) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    product = "gui" if gui else "cli"
    work = (
        out
        or housekeeping.LAB
        / "runs"
        / f"{target}-{'gui-' if gui else ''}{sabotage or mode}-{stamp}"
    )
    results = work / "results"
    results.mkdir(parents=True)
    artifacts = build_artifacts(work)
    meta: dict[str, object] = {"product": product}
    if install_dir:
        meta["install_dir"] = install_dir
    if update_from:
        meta["update_from"] = update_from
    if gui and target == "linux":
        if mode != "control":
            meta["installer"] = build_linux_gui_package(
                gui.gui_repo, gui.gui_installer, artifacts["serve"]
            )
            meta["installer_sha256"] = sha256(
                artifacts["serve"] / str(meta["installer"])
            )
        # A local .deb is installed without a signature check, as apt does.
        meta["signed"] = False
        meta["cli_from_tree"] = gui.cli_from_tree
    elif gui and target.startswith("windows"):
        if mode != "control":
            installer, meta["signed"] = build_windows_gui_installer(
                gui.gui_repo, gui.gui_installer, artifacts["serve"]
            )
            meta["installer"] = installer
            meta["installer_sha256"] = sha256(artifacts["serve"] / installer)
        meta["cli_from_tree"] = gui.cli_from_tree
    elif gui:
        meta["signed"] = build_gui_artifacts(gui.gui_repo, gui.dmg, artifacts["serve"])
        meta["dmg_sha256"] = sha256(artifacts["serve"] / "WGSExtract.dmg")
        meta["cli_from_tree"] = gui.cli_from_tree
    machine_cls = MACHINES[target]
    if gui and target == "windows":
        machine_cls = windows_gui_vm.QemuWindowsGuiMachine
    elif gui and target == "windows-host":
        machine_cls = windows_gui_vm.WindowsHostGuiMachine
    elif gui and target == "host":
        machine_cls = HostGuiMachine
    name = f"wgse-lab-{secrets.token_hex(4)}"
    machine = machine_cls(name, results, artifacts["guest"])
    if isinstance(machine, HostMachine):
        # The account the scenario runs as: mutations plant paths in its home.
        meta["home"] = machine.home
    nonce = secrets.token_hex(16)
    (results / "lab-run.json").write_text(
        json.dumps(
            {
                "target": target,
                "mode": mode,
                "sabotage": sabotage,
                "host_platform": sys.platform,
                "nonce": nonce,
                "archive_sha256": sha256(artifacts["serve"] / "wgsextract-cli.tar.gz"),
                **meta,
            }
        )
    )
    # The machine first: a VM's host-side bridge address only exists once it runs.
    if isinstance(machine, DockerMachine) and update_from in RELEASES_NEEDING_GIT:
        machine.prerequisites = ("git",)
    if isinstance(machine, DockerMachine) and gui:
        # An Ubuntu desktop: its user may use sudo, it has Python and fontconfig
        # (whose setup makes /usr/local/share/fonts), and Xvfb stands in for its
        # display.
        machine.prerequisites = ("sudo", "python3", "fontconfig", "xvfb", "xauth")
    with machine:
        with http_server(artifacts["serve"], machine.serve_host) as port:
            base = f"http://{machine.guest_host}:{port}"
            env = {
                "LAB_RESULTS": machine.results_in_guest,
                "LAB_NONCE": nonce,
                "LAB_MODE": mode,
                "LAB_SABOTAGE": sabotage,
                "LAB_INSTALLER_URL": f"{base}/install.sh",
                "LAB_ARCHIVE_URL": f"{base}/wgsextract-cli.tar.gz",
                "LAB_DATASET_URL": f"{base}/dataset.tar.gz",
                "LAB_DATASET_SHA256": sha256(artifacts["serve"] / "dataset.tar.gz"),
            }
            if install_dir:
                env["LAB_INSTALL_DIR"] = install_dir
            if update_from:
                env["LAB_UPDATE_FROM"] = update_from
            if gui:
                env["LAB_DMG_URL"] = f"{base}/WGSExtract.dmg"
                env["LAB_RUNNER_URL"] = f"{base}/gui-runner.tar.gz"
                if not gui.cli_from_tree:
                    # Setup installs the CLI release the app pins, like a user's.
                    del env["LAB_ARCHIVE_URL"]
            if gui and target == "linux":
                env.pop("LAB_INSTALLER_URL")
                env.pop("LAB_ARCHIVE_URL", None)
                if "installer" in meta:
                    env["LAB_GUI_INSTALLER_URL"] = f"{base}/{meta['installer']}"
                    env["LAB_GUI_INSTALLER_NAME"] = str(meta["installer"])
                if gui.cli_from_tree:
                    env["LAB_CLI_ARCHIVE_URL"] = f"{base}/wgsextract-cli.tar.gz"
                log(f"Running the app {mode} scenario on {target} machine {name}...")
                scenario = shlex.quote(f"{machine.guest_scripts_in_guest}/gui-linux.sh")
                machine.exec(env, f"sh {scenario}")
                return results
            if gui and target.startswith("windows"):
                env.pop("LAB_INSTALLER_URL")
                env.pop("LAB_ARCHIVE_URL", None)
                if "installer" in meta:
                    env["LAB_GUI_INSTALLER_URL"] = f"{base}/{meta['installer']}"
                    env["LAB_GUI_INSTALLER_NAME"] = str(meta["installer"])
                if gui.cli_from_tree:
                    env["LAB_CLI_ARCHIVE_URL"] = f"{base}/wgsextract-cli.zip"
                log(f"Running the app {mode} scenario on {target} machine {name}...")
                machine.exec(env, "gui-scenario.ps1")
                return results
            if target.startswith("windows"):
                env["LAB_INSTALLER_URL"] = f"{base}/install_windows.bat"
                env["LAB_ARCHIVE_URL"] = f"{base}/wgsextract-cli.zip"
                log(f"Running the {mode} scenario on {target} machine {name}...")
                machine.exec(env, "scenario.ps1")
                return results
            script = "gui-macos.sh" if gui else "scenario.sh"
            log(f"Running the {product} {mode} scenario on {target} machine {name}...")
            scenario = shlex.quote(f"{machine.guest_scripts_in_guest}/{script}")
            machine.exec(env, f"sh {scenario}")
    return results


def enable_gatekeeper(ip: str, vnc_log: Path, evidence: Path) -> None:
    """Restore both the master switch and the normal identified-developer rules."""
    # The setting is written lazily; sync before the VM is stopped or it's lost.
    ssh_retry(
        ip,
        "echo admin | sudo -S spctl --global-enable && spctl --status && sync && sleep 5",
    )
    macos_policy.restore(["ssh", *SSH_OPTS, f"admin@{ip}"], vnc_log, evidence)


def cmd_prepare(args: argparse.Namespace) -> None:
    """Clone the vanilla image, authorize the lab SSH key, install the CLT."""
    if args.repair_policy and args.target != "macos":
        raise SystemExit("--repair-policy is only supported for macos.")
    if args.target == "windows":
        windows_vm.prepare()
        return
    if args.target == "windows-gui":
        windows_gui_vm.prepare_gui()
        return
    macos_policy.require_ui_tools()
    if not SSH_KEY.exists():
        SSH_KEY.parent.mkdir(parents=True, exist_ok=True)
        run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "wgse-lab",
             "-f", str(SSH_KEY)])  # fmt: skip
    if not args.repair_policy:
        log(
            f"Building {MACOS_BASE} from {MACOS_VANILLA} (+ Xcode Command Line Tools)..."
        )
        subprocess.run(["tart", "delete", MACOS_BASE], capture_output=True)
        run(["tart", "clone", MACOS_VANILLA, MACOS_BASE])
    else:
        log(f"Repairing the standard Gatekeeper policy in {MACOS_BASE}...")
    evidence = housekeeping.LAB / "macos-policy"
    evidence.mkdir(parents=True, exist_ok=True)
    vnc_log = evidence / "vnc.private.log"
    with vnc_log.open("w") as output:
        vnc_log.chmod(0o600)
        vm = subprocess.Popen(
            ["tart", "run", MACOS_BASE, "--no-graphics", "--vnc-experimental"],
            stdout=output,
            stderr=subprocess.STDOUT,
        )
    try:
        ip = ""
        for _ in range(200):
            if vm.poll() is not None:
                raise SystemExit(f"{MACOS_BASE} stopped unexpectedly; see {vnc_log}.")
            result = subprocess.run(
                ["tart", "ip", MACOS_BASE], capture_output=True, text=True
            )
            if result.returncode == 0 and result.stdout.strip():
                ip = result.stdout.strip()
                break
            time.sleep(3)
        if not ip:
            raise SystemExit("The base VM never got an IP address.")
        # The vanilla image only has password login (admin/admin).
        authorize_lab_key(ip)
        if args.repair_policy:
            enable_gatekeeper(ip, vnc_log, evidence)
            log("Standard Gatekeeper policy verified.")
            return
        # The installer requires the Command Line Tools, a documented user
        # prerequisite; install them headlessly the way softwareupdate allows.
        # The host's DNS forwarder for Tart's bridge doesn't answer on some hosts
        # (seen alongside OrbStack); use public resolvers inside the lab VM.
        ssh_retry(
            ip,
            "echo admin | sudo -S networksetup -setdnsservers Ethernet 1.1.1.1 8.8.8.8",
        )
        script = (
            "touch /tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress; "
            "label=$(softwareupdate -l 2>/dev/null | sed -n 's/^\\* Label: //p' | "
            "grep 'Command Line Tools' | sort -V | tail -1); echo \"Installing $label\"; "
            'echo admin | sudo -S softwareupdate -i "$label" --verbose; '
            "rm -f /tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress; "
            "xcode-select -p && xcrun --find clang"
        )
        ssh_retry(ip, f"/bin/zsh -lc {shlex.quote(script)}")
        enable_gatekeeper(ip, vnc_log, evidence)
    finally:
        subprocess.run(["tart", "stop", MACOS_BASE])
        vm.wait(timeout=300)
    log(f"{MACOS_BASE} is ready.")


def cmd_run(args: argparse.Namespace) -> None:
    gui = args.gui_repo or args.gui_installer
    if gui and args.target not in ("macos", "windows", "linux", "host", "windows-host"):
        raise SystemExit("The app is tested on the macos, windows and linux targets.")
    if gui and args.target == "host" and sys.platform != "darwin":
        raise SystemExit("On a Linux runner, test the app with the linux target.")
    if args.gui_installer and args.target in ("macos", "host"):
        raise SystemExit(
            "--gui-installer is a Windows .exe or Linux .deb; use --dmg on macos"
        )
    results = run_scenario(
        args.target,
        args.mode,
        args.sabotage or "",
        args.out,
        args if gui else None,
        install_dir=args.install_dir,
        update_from=args.update_from,
    )
    if args.no_judge:
        log(f"Evidence collected (judge it elsewhere): {results}")
        return
    sys.exit(0 if judged_as_expected(results, args.target) else 1)


def judged_as_expected(results: Path, target: str) -> bool:
    """A run must pass; a sabotaged run (a negative control) must fail, on the
    checks its sabotage breaks."""
    ok = judge(results, target)
    sabotage = json.loads((results / "lab-run.json").read_text()).get("sabotage")
    if not sabotage:
        return ok
    expected = SABOTAGES[sabotage]
    verdict = json.loads((results / "verdict.json").read_text())
    failed = {c["name"] for c in verdict["checks"] if not c["ok"]}
    caught = all(name in failed for name in expected)
    print(
        f"Negative control {sabotage}: "
        f"{'caught' if caught else 'NOT CAUGHT'} (expected failing: {expected})"
    )
    return caught and not ok


def cmd_selftest(args: argparse.Namespace) -> None:
    missed = []
    for sabotage, expected in SABOTAGES.items():
        results = run_scenario(args.target, "install", sabotage, None)
        judge(results, args.target)
        verdict = json.loads((results / "verdict.json").read_text())
        failed = {c["name"] for c in verdict["checks"] if not c["ok"]}
        if verdict["passed"] or not all(name in failed for name in expected):
            missed.append(sabotage)
    print(
        "\nSELFTEST:",
        "every sabotage was caught" if not missed else f"NOT CAUGHT: {missed}",
    )
    sys.exit(1 if missed else 0)


# Corruptions of a passing run's evidence, each of which the oracle must catch.
MUTATIONS = {
    "wrong-nonce": lambda r: (r / "nonce").write_text("0" * 32),
    "missing-step": lambda r: (r / "evidence" / "uninstall.rc").unlink(),
    "failed-step": lambda r: (r / "workload" / "evidence" / "snp.rc").write_text("1"),
    "wrong-haplogroup": lambda r: _rewrite(
        r / "workload" / "mt_haplo" / "haplogrep_results.txt", "H16a1", "H1"
    ),
    "missing-output": lambda r: shutil.rmtree(r / "workload" / "align"),
    "empty-calls": lambda r: (r / "workload" / "snp" / "snps.vcf.gz").write_bytes(b""),
    "leftover-state": lambda r: _append(
        r / "inventory-after_uninstall.txt", f"dir\t{_planted(r)['leftover']}"
    ),
    "invisible-install": lambda r: shutil.copy2(
        r / "inventory-baseline.txt", r / "inventory-after_install.txt"
    ),
    "contaminated-baseline": lambda r: _append(
        r / "preexisting-tools.txt", f"samtools\t{_planted(r)['tool']}"
    ),
    "silent-y-failure": lambda r: (
        r / "workload" / "evidence" / "y_haplo.log"
    ).write_text(""),
}


def _json_edit(path: Path, edit: Callable[[Any], None]) -> None:
    data = json.loads(path.read_text())
    edit(data)
    path.write_text(json.dumps(data))


def _fail_action(report: Any, action: str) -> None:
    for step in report["steps"]:
        if step.get("id") == action:
            step["status"] = "failed"


# The app reports its own setup, actions and uninstall; corrupting what it
# reports must fail the run as surely as corrupting the outputs.
GUI_MUTATIONS: dict[str, Callable[[Path], object]] = {
    "wrong-nonce": MUTATIONS["wrong-nonce"],
    "missing-step": MUTATIONS["missing-step"],
    "wrong-haplogroup": MUTATIONS["wrong-haplogroup"],
    "missing-output": MUTATIONS["missing-output"],
    "empty-calls": MUTATIONS["empty-calls"],
    "leftover-state": lambda r: _append(
        r / "inventory-after_uninstall.txt",
        f"dir\t{_planted(r)['home']}/Library/Application Support/dev.guiforcli.embed.wgsextract",
    ),
    "invisible-install": MUTATIONS["invisible-install"],
    "contaminated-baseline": MUTATIONS["contaminated-baseline"],
    "setup-failed": lambda r: _json_edit(
        r / "setup-result.json", lambda d: d.update(status="failed")
    ),
    "action-failed": lambda r: _json_edit(
        r / "bundle-report.json", lambda d: _fail_action(d, "snp")
    ),
    "action-missing": lambda r: _json_edit(
        r / "bundle-report.json", lambda d: d.update(steps=d["steps"][:-1])
    ),
    "uninstall-failed": lambda r: _json_edit(
        r / "uninstall-result.json", lambda d: d.update(status="failed")
    ),
    "unsigned-accepted": lambda r: (
        (r / "evidence" / "gatekeeper.rc").write_text("0"),
        (r / "evidence" / "gatekeeper.log").write_text("accepted\n"),
    ),
    "gatekeeper-off": lambda r: (r / "gatekeeper-status.txt").write_text(
        "assessments disabled\n"
    ),
    "no-window": lambda r: (r / "startup.txt").write_text(""),
}
_WIN_HOME = "C:/Users/lab user"
_WIN_COMMON = [
    "wrong-nonce",
    "missing-step",
    "wrong-haplogroup",
    "missing-output",
    "empty-calls",
    "invisible-install",
    "contaminated-baseline",
    "setup-failed",
    "action-failed",
    "action-missing",
    "uninstall-failed",
]
# The Windows app: the same, plus what a Windows uninstall can leave behind.
WINDOWS_GUI_MUTATIONS: dict[str, Callable[[Path], object]] = {
    **{name: GUI_MUTATIONS[name] for name in _WIN_COMMON},
    "leftover-state": lambda r: _append(
        r / "inventory-after_uninstall.txt",
        f"dir\t{_WIN_HOME}/AppData/Local/dev.guiforcli.web.embed.wgsextract",
    ),
    "apps-entry-left": lambda r: _append(
        r / "inventory-after_uninstall.txt",
        "regkey\tHKEY_CURRENT_USER/Software/Microsoft/Windows/CurrentVersion/Uninstall/WGSExtract",
    ),
    "shortcut-left": lambda r: _append(
        r / "inventory-after_uninstall.txt",
        f"file\t{_WIN_HOME}/AppData/Roaming/Microsoft/Windows/Start Menu/Programs/WGSExtract.lnk",
    ),
    "user-path-left": lambda r: _append(
        r / "inventory-after_uninstall.txt",
        f"reg\tHKCU:/Environment/Path\tExpandString:{_WIN_HOME}/.pixi/bin",
    ),
    "app-still-running": lambda r: _append(
        r / "inventory-after_uninstall.txt",
        f"proc\t{_WIN_HOME}/AppData/Local/WGSExtract/node/node.exe",
    ),
    "unsigned-accepted": lambda r: (
        (r / "evidence" / "signature.rc").write_text("0"),
        (r / "evidence" / "signature.log").write_text("Status: Valid\n"),
    ),
    "no-window": lambda r: (r / "app-stdout.txt").write_text(""),
}
# The Linux app: the same, plus what removing a package can leave behind.
LINUX_GUI_MUTATIONS: dict[str, Callable[[Path], object]] = {
    **{name: GUI_MUTATIONS[name] for name in _WIN_COMMON},
    "leftover-state": lambda r: _append(
        r / "inventory-after_uninstall.txt",
        "dir\t/home/tester/.local/share/dev.guiforcli.web.embed.wgsextract",
    ),
    "pixi-left": lambda r: _append(
        r / "inventory-after_uninstall.txt", "dir\t/home/tester/.pixi"
    ),
    "desktop-entry-left": lambda r: _append(
        r / "inventory-after_uninstall.txt",
        "file\t/usr/share/applications/WGSExtract.desktop",
    ),
    "no-window": lambda r: (r / "app-stdout.txt").write_text(""),
}


def _planted(results: Path) -> dict[str, str]:
    """Paths the mutations plant, in the form the run's own machine records."""
    meta = json.loads((results / "lab-run.json").read_text())
    target = meta["target"]
    if target.startswith("windows"):
        return {
            "home": "C:/Users/lab user",
            "leftover": "C:/Users/lab user/.pixi",
            "tool": "c:/msys64/ucrt64/bin/samtools.exe",
        }
    if target == "macos" or meta.get("host_platform") == "darwin":
        home = meta.get("home", "/Users/admin")
        return {
            "home": home,
            "leftover": f"{home}/.pixi",
            "tool": "/opt/homebrew/bin/samtools",
        }
    home = meta.get("home", "/home/tester")
    return {
        "home": home,
        "leftover": f"{home}/.pixi",
        "tool": "/usr/local/bin/samtools",
    }


def _rewrite(path: Path, old: str, new: str) -> None:
    path.write_text(path.read_text().replace(old, new))


def _append(path: Path, line: str) -> None:
    path.write_text(path.read_text() + line + "\n")


def cmd_mutate(args: argparse.Namespace) -> None:
    """Corrupt copies of a passing run; the oracle must fail every one."""
    meta = json.loads((args.results / "lab-run.json").read_text())
    if not judge(args.results, meta["target"]):
        raise SystemExit("mutate needs a passing run to start from.")
    missed = []
    if meta.get("product") != "gui":
        mutations = MUTATIONS
    elif meta["target"].startswith("windows"):
        mutations = WINDOWS_GUI_MUTATIONS
    elif meta["target"] == "linux":
        mutations = LINUX_GUI_MUTATIONS
    else:
        mutations = GUI_MUTATIONS
    for name, mutate in mutations.items():
        copy = args.results.parent / f"mutant-{name}"
        shutil.rmtree(copy, ignore_errors=True)
        shutil.copytree(args.results, copy, symlinks=True)
        mutate(copy)
        print(f"\n--- mutation {name}")
        if judge(copy, meta["target"]):
            missed.append(name)
        shutil.rmtree(copy)
    print("\nMUTATIONS:", "all caught" if not missed else f"NOT CAUGHT: {missed}")
    sys.exit(1 if missed else 0)


def cmd_judge(args: argparse.Namespace) -> None:
    meta = json.loads((args.results / "lab-run.json").read_text())
    sys.exit(0 if judged_as_expected(args.results, meta["target"]) else 1)


def main() -> None:
    # Stopping the lab (kill, Ctrl-C) still destroys its machines.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(required=True)
    p = sub.add_parser("prepare", help="Build the clean base image for a target.")
    p.add_argument("target", choices=["macos", "windows", "windows-gui"])
    p.add_argument(
        "--repair-policy",
        action="store_true",
        help="Repair normal Gatekeeper policy in an existing macOS base, without rebuilding.",
    )
    p.set_defaults(func=cmd_prepare)
    p = sub.add_parser("run", help="Run one scenario on a fresh machine and judge it.")
    p.add_argument("target", choices=sorted(MACHINES))
    p.add_argument("--mode", choices=["install", "control"], default="install")
    p.add_argument("--sabotage", choices=sorted(SABOTAGES))
    # Absolute: docker -v and a fresh host user resolve relative paths elsewhere.
    p.add_argument("--out", type=lambda value: Path(value).resolve())
    p.add_argument(
        "--no-judge",
        action="store_true",
        help="Only collect evidence (CI judges it on another machine).",
    )
    p.add_argument(
        "--gui-repo",
        type=Path,
        help="Test the app (macOS DMG, or Windows installer) built from this checkout.",
    )
    p.add_argument("--dmg", type=Path, help="With --gui-repo: test this DMG instead.")
    p.add_argument(
        "--gui-installer",
        type=Path,
        help="Test this Windows installer (*-setup.exe) instead of building one.",
    )
    p.add_argument(
        "--cli-from-tree",
        action="store_true",
        help="With --gui-repo: setup installs this CLI tree, not the pinned release.",
    )
    p.add_argument(
        "--install-dir",
        help="Install into this folder under $HOME instead of the README default, "
        'e.g. "WGS Extract" to test a path with a space.',
    )
    p.add_argument(
        "--update-from",
        metavar="TAG",
        help="Install this published release first, then update it to this tree.",
    )
    p.set_defaults(func=cmd_run)
    p = sub.add_parser("selftest", help="Every sabotage must make the verdict FAIL.")
    p.add_argument("target", choices=sorted(MACHINES))
    p.set_defaults(func=cmd_selftest)
    p = sub.add_parser("mutate", help="Corrupted copies of a passing run must FAIL.")
    p.add_argument("results", type=Path)
    p.set_defaults(func=cmd_mutate)
    p = sub.add_parser(
        "gc", help="Remove expired runs, images and leftovers (see housekeeping.py)."
    )
    housekeeping.add_arguments(p)
    p.set_defaults(func=lambda args: housekeeping.from_arguments(args))
    p = sub.add_parser("judge", help="Judge an existing results directory.")
    p.add_argument("results", type=Path)
    p.set_defaults(func=cmd_judge)
    args = parser.parse_args()
    if args.func not in (cmd_run, cmd_prepare, cmd_selftest):
        args.func(args)
        return
    try:
        args.func(args)
    finally:
        # Afterwards, so a base image this run just used is never the one expired.
        log("Removing expired lab runs, images and leftovers...")
        housekeeping.collect_garbage()


if __name__ == "__main__":
    main()
