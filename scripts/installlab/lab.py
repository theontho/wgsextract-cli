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

Targets: `macos` (Tart VM from a vanilla macOS image + Command Line Tools),
`linux` (fresh ubuntu:24.04 amd64 container + curl/git), `host` (the current
machine, for disposable CI runners only).
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import hashlib
import http.server
import json
import os
import secrets
import shlex
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
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
    "noop-installer": ["step.install", "workload.help"],
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
    return subprocess.run(cmd, text=True, check=True, **kwargs)  # type: ignore[call-overload]


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

    dataset = fetch_dataset(REPO / "out" / "installlab" / "cache")
    shutil.copy2(dataset, serve / "dataset.tar.gz")

    guest = work / "guest"
    shutil.copytree(HERE / "guest", guest)
    return {"serve": serve, "guest": guest, "dataset": dataset}


def fetch_dataset(cache: Path) -> Path:
    """Download (once) and verify the mini genome; repackage as tar.gz for guests
    without unzip."""
    cache.mkdir(parents=True, exist_ok=True)
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
    fetch_dataset(REPO / "out" / "installlab" / "cache")
    return REPO / "out" / "installlab" / "cache" / "unpacked" / "hg19-mini-hg00096"


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
    def exec(self, env: dict[str, str], command: str) -> int: ...


SSH_KEY = REPO / "out" / "installlab" / "ssh" / "id_ed25519"
SSH_OPTS = [
    "-i", str(SSH_KEY),
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=5",
]  # fmt: skip


def boot_vm(name: str, dirs: list[str]) -> tuple[subprocess.Popen[bytes], str]:
    """Start a Tart VM headless; return the process and its IP once SSH answers."""
    vm = subprocess.Popen(
        ["tart", "run", name, "--no-graphics", *(f"--dir={d}" for d in dirs)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        ip = subprocess.run(["tart", "ip", name], capture_output=True, text=True)
        if ip.returncode == 0 and ip.stdout.strip():
            address = ip.stdout.strip()
            probe = subprocess.run(["ssh", *SSH_OPTS, f"admin@{address}", "true"])
            if probe.returncode == 0:
                return vm, address
        time.sleep(3)
    vm.terminate()
    raise SystemExit(f"VM {name} did not accept SSH")


class TartMachine(Machine):
    serve_host = "192.168.64.1"
    guest_host = "192.168.64.1"
    results_in_guest = "/Volumes/My Shared Files/results"
    guest_scripts_in_guest = "/Volumes/My Shared Files/guest"

    def create(self) -> None:
        run(["tart", "clone", MACOS_BASE, self.name])
        self.vm, self.ip = boot_vm(
            self.name, [f"results:{self.results}", f"guest:{self.guest}:ro"]
        )

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
                "-y -qq curl ca-certificates git >/dev/null && useradd -m -s /bin/bash "
                "tester && chown tester /results",
            ]  # fmt: skip
        )

    def destroy(self) -> None:
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


class HostMachine(Machine):
    """A brand-new user account on a disposable CI runner (never a dev machine).

    GitHub's macOS runners can't nest VMs, and they come with Homebrew, Python,
    Java and more. A fresh user with only the system PATH can't see any of that
    unless the installer wrongly depends on it.
    """

    user = "wgselab"

    def create(self) -> None:
        if not os.environ.get("CI"):
            raise SystemExit("The host target is only for disposable CI runners.")
        self.results_in_guest = str(self.results)
        self.guest_scripts_in_guest = str(self.guest)
        password = secrets.token_hex(16)
        if sys.platform == "darwin":
            run(["sudo", "sysadminctl", "-addUser", self.user, "-password", password,
                 "-home", f"/Users/{self.user}"])  # fmt: skip
            run(["sudo", "createhomedir", "-c", "-u", self.user], capture_output=True)
        else:
            run(["sudo", "useradd", "-m", "-s", "/bin/bash", self.user])
        # The results folder must be writable by the new user; nothing else is shared.
        os.chmod(self.results, 0o777)
        for parent in (self.results, *self.results.parents):
            if parent == Path("/"):
                break
            os.chmod(parent, os.stat(parent).st_mode | 0o011)

    def exec(self, env: dict[str, str], command: str) -> int:
        home = (
            f"/Users/{self.user}" if sys.platform == "darwin" else f"/home/{self.user}"
        )
        shell = "/bin/zsh" if sys.platform == "darwin" else "/bin/bash"
        clean_env = [
            f"HOME={home}",
            f"USER={self.user}",
            "PATH=/usr/bin:/bin:/usr/sbin:/sbin",
            *(f"{k}={v}" for k, v in env.items()),
        ]
        return subprocess.run(
            [
                "sudo",
                "-u",
                self.user,
                "env",
                "-i",
                *clean_env,
                shell,
                "-l",
                "-c",
                command,
            ],
            cwd="/",
        ).returncode


MACHINES = {"macos": TartMachine, "linux": DockerMachine, "host": HostMachine}


# --- commands -------------------------------------------------------------------


def noise_patterns(target: str) -> list[str]:
    noise_file = HERE / "noise" / f"{target}.txt"
    patterns = list(GENERIC_NOISE)
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
    checks = Oracle(results, meta["nonce"], find_tools()).run(
        dataset_dir(), noise_patterns(target)
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


def run_scenario(target: str, mode: str, sabotage: str, out: Path | None) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    work = (
        out
        or REPO / "out" / "installlab" / "runs" / f"{target}-{sabotage or mode}-{stamp}"
    )
    results = work / "results"
    results.mkdir(parents=True)
    artifacts = build_artifacts(work)
    nonce = secrets.token_hex(16)
    (results / "lab-run.json").write_text(
        json.dumps(
            {
                "target": target,
                "mode": mode,
                "sabotage": sabotage,
                "nonce": nonce,
                "archive_sha256": sha256(artifacts["serve"] / "wgsextract-cli.tar.gz"),
            }
        )
    )
    machine_cls = MACHINES[target]
    name = f"wgse-lab-{secrets.token_hex(4)}"
    # The machine first: a VM's host-side bridge address only exists once it runs.
    with machine_cls(name, results, artifacts["guest"]) as machine:
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
            log(f"Running the {mode} scenario on {target} machine {name}...")
            scenario = shlex.quote(f"{machine.guest_scripts_in_guest}/scenario.sh")
            machine.exec(env, f"sh {scenario}")
    return results


def cmd_prepare(args: argparse.Namespace) -> None:
    """Clone the vanilla image, authorize the lab SSH key, install the CLT."""
    if not SSH_KEY.exists():
        SSH_KEY.parent.mkdir(parents=True, exist_ok=True)
        run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "wgse-lab",
             "-f", str(SSH_KEY)])  # fmt: skip
    log(f"Building {MACOS_BASE} from {MACOS_VANILLA} (+ Xcode Command Line Tools)...")
    subprocess.run(["tart", "delete", MACOS_BASE], capture_output=True)
    run(["tart", "clone", MACOS_VANILLA, MACOS_BASE])
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
            raise SystemExit("The base VM never got an IP address.")
        # The vanilla image only has password login (admin/admin); use it once,
        # through macOS's built-in expect, to authorize the lab key.
        pub = SSH_KEY.with_suffix(".pub").read_text().strip()
        expect = f"""
set timeout 300
spawn ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o PubkeyAuthentication=no admin@{ip} "mkdir -p ~/.ssh && chmod 700 ~/.ssh && echo '{pub}' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
expect {{
    "assword:" {{ send "admin\\r"; exp_continue }}
    "Connection refused" {{ sleep 3; exit 2 }}
    eof
}}
"""
        for _ in range(60):
            if subprocess.run(["/usr/bin/expect", "-c", expect]).returncode == 0 and (
                subprocess.run(["ssh", *SSH_OPTS, f"admin@{ip}", "true"]).returncode
                == 0
            ):
                break
            time.sleep(5)
        else:
            raise SystemExit("Could not authorize the lab SSH key in the base VM.")
        # The installer requires the Command Line Tools, a documented user
        # prerequisite; install them headlessly the way softwareupdate allows.
        # The host's DNS forwarder for Tart's bridge doesn't answer on some hosts
        # (seen alongside OrbStack); use public resolvers inside the lab VM.
        run(["ssh", *SSH_OPTS, f"admin@{ip}",
             "echo admin | sudo -S networksetup -setdnsservers Ethernet 1.1.1.1 8.8.8.8"])  # fmt: skip
        script = (
            "touch /tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress; "
            "label=$(softwareupdate -l 2>/dev/null | sed -n 's/^\\* Label: //p' | "
            "grep 'Command Line Tools' | sort -V | tail -1); echo \"Installing $label\"; "
            'echo admin | sudo -S softwareupdate -i "$label" --verbose; '
            "rm -f /tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress; "
            "xcode-select -p && xcrun --find clang"
        )
        run(["ssh", *SSH_OPTS, f"admin@{ip}", f"/bin/zsh -lc {shlex.quote(script)}"])
    finally:
        subprocess.run(["tart", "stop", MACOS_BASE])
        vm.wait(timeout=300)
    log(f"{MACOS_BASE} is ready.")


def cmd_run(args: argparse.Namespace) -> None:
    results = run_scenario(args.target, args.mode, args.sabotage or "", args.out)
    ok = judge(results, args.target)
    if args.sabotage:
        expected = SABOTAGES[args.sabotage]
        verdict = json.loads((results / "verdict.json").read_text())
        failed = {c["name"] for c in verdict["checks"] if not c["ok"]}
        caught = all(name in failed for name in expected)
        print(
            f"Negative control {args.sabotage}: "
            f"{'caught' if caught else 'NOT CAUGHT'} (expected failing: {expected})"
        )
        sys.exit(0 if caught and not ok else 1)
    sys.exit(0 if ok else 1)


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
        r / "inventory-after_uninstall.txt", "dir\t/home/tester/.pixi"
    ),
    "invisible-install": lambda r: shutil.copy2(
        r / "inventory-baseline.txt", r / "inventory-after_install.txt"
    ),
    "contaminated-baseline": lambda r: _append(
        r / "preexisting-tools.txt", "samtools\t/opt/homebrew/bin/samtools"
    ),
    "silent-y-failure": lambda r: (
        r / "workload" / "evidence" / "y_haplo.log"
    ).write_text(""),
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
    for name, mutate in MUTATIONS.items():
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
    sys.exit(0 if judge(args.results, meta["target"]) else 1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(required=True)
    p = sub.add_parser("prepare", help="Build the clean base image for a target.")
    p.add_argument("target", choices=["macos"])
    p.set_defaults(func=cmd_prepare)
    p = sub.add_parser("run", help="Run one scenario on a fresh machine and judge it.")
    p.add_argument("target", choices=sorted(MACHINES))
    p.add_argument("--mode", choices=["install", "control"], default="install")
    p.add_argument("--sabotage", choices=sorted(SABOTAGES))
    p.add_argument("--out", type=Path)
    p.set_defaults(func=cmd_run)
    p = sub.add_parser("selftest", help="Every sabotage must make the verdict FAIL.")
    p.add_argument("target", choices=sorted(MACHINES))
    p.set_defaults(func=cmd_selftest)
    p = sub.add_parser("mutate", help="Corrupted copies of a passing run must FAIL.")
    p.add_argument("results", type=Path)
    p.set_defaults(func=cmd_mutate)
    p = sub.add_parser("judge", help="Judge an existing results directory.")
    p.add_argument("results", type=Path)
    p.set_defaults(func=cmd_judge)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
