"""Stand-in executables written in Python that run on every platform."""

import json
import sys
from pathlib import Path

import pytest


def require_bare_name_fakes() -> None:
    """Skip on Windows, where a tool the CLI starts by bare name (`gatk`,
    `bcftools`) can't be faked: CreateProcess finds only .exe files, not
    python_tool's .cmd launcher, and killing a launcher leaves its Python
    child running."""
    if sys.platform == "win32":
        pytest.skip("a .cmd fake can't stand in for a tool started by bare name")


def python_tool(bindir: Path, name: str, source: str) -> Path:
    """An executable `name` in bindir that runs the Python ``source``.

    POSIX runs the script by its shebang; Windows can't, so it gets
    ``name.py`` and a ``name.cmd`` launcher, which PATH lookups find too.
    """
    bindir.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        path = bindir / name
        path.write_text(f"#!{sys.executable}\n{source}")
        path.chmod(0o755)
        return path
    (bindir / f"{name}.py").write_text(source)
    launcher = bindir / f"{name}.cmd"
    launcher.write_text(f'@"{sys.executable}" "%~dp0{name}.py" %*\r\n', newline="")
    return launcher


_FAKE_PIXI = """\
import json, os, shutil, subprocess, sys

state = json.load(open({state!r}))
sub, rest = sys.argv[1], sys.argv[2:]
manifest, env, flags, command = None, "default", set(), []
i = 0
while i < len(rest):
    arg = rest[i]
    if arg == "--manifest-path":
        manifest, i = rest[i + 1], i + 2
    elif arg.startswith("--manifest-path="):
        manifest, i = arg.split("=", 1)[1], i + 1
    elif arg in ("-e", "--environment"):
        env, i = rest[i + 1], i + 2
    elif arg.startswith("--"):
        flags.add(arg)
        i += 1
    else:
        command = rest[i:]
        break


def norm(path):
    return os.path.normcase(os.path.abspath(path))


def discover():
    here = os.getcwd()
    while True:
        if os.path.isfile(os.path.join(here, "pixi.toml")):
            return os.path.join(here, "pixi.toml")
        if os.path.dirname(here) == here:
            return None
        here = os.path.dirname(here)


manifest = manifest or discover()
if manifest is None or (
    state["manifest"] is not None and norm(manifest) != norm(state["manifest"])
):
    sys.exit("Error: could not find a pixi workspace")
if sub == "info":
    if state["info_stdout"] is not None:
        print(state["info_stdout"])
    else:
        envs = [{{"name": n, "prefix": e["prefix"]}} for n, e in state["envs"].items()]
        print(json.dumps({{"environments_info": envs}}))
    sys.exit(0)
if env not in state["envs"]:
    sys.exit(f"Error: unknown environment '{{env}}'")
prefix = state["envs"][env]["prefix"]
marker = os.path.join(prefix, "conda-meta", "pixi")
if sub in ("install", "list", "run") and "--no-install" not in flags:
    os.makedirs(os.path.dirname(marker), exist_ok=True)
    open(marker, "w").write("{{}}")
if sub == "list":
    if state["list_stdout"] is not None:
        print(state["list_stdout"])
        sys.exit(state["list_returncode"])
    print(json.dumps([{{"name": p}} for p in state["envs"][env]["locked"]]))
elif sub == "run":
    if state["run_stdout"] is not None:
        print(state["run_stdout"])
        sys.exit(0)
    # Activated even when missing: --no-install then runs the command with
    # the host's programs, as pixi does.
    bins = ["Library/bin", "Scripts", "bin", "."] if os.name == "nt" else ["bin"]
    bins = bins if os.path.isfile(marker) else []
    path = os.pathsep.join(
        [os.path.join(prefix, b) for b in bins] + [os.environ.get("PATH", "")]
    )
    exe = shutil.which(command[0], path=path)
    if exe is None and command[0] == "python":
        exe = sys.executable  # the host's Python
    if exe is None:
        sys.exit(f"pixi: {{command[0]}}: command not found")
    environ = dict(os.environ, PATH=path, CONDA_PREFIX=prefix, PIXI_ENVIRONMENT_NAME=env)
    sys.exit(subprocess.run([exe, *command[1:]], env=environ).returncode)
"""

# An environment's Python: this interpreter, reporting the environment as its prefix.
_ENV_PYTHON = """\
import runpy, sys

sys.prefix = {prefix!r}
args = sys.argv[1:]
if args[:1] == ["-c"]:
    sys.argv = ["-c", *args[2:]]
    exec(compile(args[1], "<string>", "exec"), {{"__name__": "__main__"}})
elif args:
    sys.argv = args
    runpy.run_path(args[0], run_name="__main__")
"""


class FakePixi:
    """A pixi stand-in for one workspace, with environments under ``root/envs``.

    Like pixi, ``run``, ``list`` and ``install`` install a missing environment
    (marked by ``conda-meta/pixi``) unless given ``--no-install``, and
    ``run --no-install`` of a missing one runs the host's programs. Without
    ``--manifest-path`` it finds the workspace in the current directory or a
    parent. It knows only the workspace at ``manifest``; ``None`` is any.
    """

    def __init__(
        self, root: Path, manifest: Path | None, bindir: Path | None = None
    ) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self._state = root / "pixi-state.json"
        self._data: dict = {
            "manifest": str(manifest) if manifest else None,
            "envs": {},
            "info_stdout": None,
            "list_stdout": None,
            "list_returncode": 0,
            "run_stdout": None,
        }
        self._save()
        self.host_bin = root / "host-bin"
        self.host_bin.mkdir()
        self.exe = python_tool(bindir or root / "pixi-bin", "pixi", "")
        source = _FAKE_PIXI.format(state=str(self._state))
        if sys.platform == "win32":
            (self.exe.parent / "pixi.py").write_text(source)
        else:
            self.exe.write_text(f"#!{sys.executable}\n{source}")

    def _save(self) -> None:
        self._state.write_text(json.dumps(self._data))

    def set(self, **outputs: object) -> None:
        """Replace an answer: info_stdout, list_stdout/list_returncode or run_stdout."""
        self._data.update(outputs)
        self._save()

    def env(
        self, name: str, *, locked: tuple[str, ...] = (), installed: bool = True
    ) -> Path:
        """An environment whose lockfile has ``locked`` packages; returns its prefix."""
        prefix = self.root / "envs" / name
        self._data["envs"][name] = {"prefix": str(prefix), "locked": list(locked)}
        self._save()
        python_tool(prefix / "bin", "python", _ENV_PYTHON.format(prefix=str(prefix)))
        if installed:
            self.install(name)
        return prefix

    def install(self, name: str) -> None:
        marker = self.root / "envs" / name / "conda-meta" / "pixi"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("{}")

    def uninstall(self, name: str) -> None:
        (self.root / "envs" / name / "conda-meta" / "pixi").unlink()

    def installed(self, name: str) -> bool:
        return (self.root / "envs" / name / "conda-meta" / "pixi").is_file()


_FAKE_GATK = """\
import json, os, subprocess, sys, time

state = json.load(open({state!r}))
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("The Genome Analysis Toolkit (GATK) v4.6.2.0")
    sys.exit(0)
if args[:1] == ["--java-options"]:
    args = args[2:]
tool, rest = args[0], args[1:]
opts = dict(zip(rest[::2], rest[1::2]))
with open(state["log"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")


def local(flag):
    value = opts[flag]
    if state["windows"] and "/" in value:
        sys.exit(f"A USER ERROR has occurred: {{flag}} {{value}} is not a Windows path")
    return value.replace("\\\\", "/") if os.sep == "/" else value


ref, alignment, out, tmp = (local(f) for f in ("-R", "-I", "-O", "--tmp-dir"))
stem = ref[: -len(".gz")] if ref.endswith(".gz") else ref
if not os.path.isfile(stem.rsplit(".", 1)[0] + ".dict"):
    sys.exit(f"A USER ERROR has occurred: Fasta dict file for {{ref}} does not exist")
if not os.path.isdir(tmp):
    sys.exit(f"A USER ERROR has occurred: --tmp-dir {{tmp}} does not exist")
if alignment.endswith(".cram") and not state["reads_cram"]:
    sys.exit("htsjdk.samtools.cram.CRAMException: CRAM version 3.1 is not supported")
intervals = open(local("-L")).read().split() if "-L" in opts else []
if state["fail"]:
    sys.exit(f"A USER ERROR has occurred: {{state['fail']}}")
for contig, action in state["on_contig"].items():
    if not any(i.split(":")[0] == contig for i in intervals):
        continue
    if action == "fail":
        # Fail only once a hanging peer is running, so it has something to stop.
        while "hang" in state["on_contig"].values() and not os.path.exists(state["pids"]):
            time.sleep(0.05)
        sys.exit(f"{{contig}} failed")
    if action == "slow":
        time.sleep(1)
    if action == "hang":
        java = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        with open(state["pids"], "a") as pids:
            pids.write(f"{{os.getpid()}} {{java.pid}}\\n")
        time.sleep(60)
regions = ["-r", ",".join(intervals)] if intervals else []
mpileup = subprocess.Popen(
    [{bcftools!r}, "mpileup", "-f", ref, *regions, "-Ou", alignment],
    stdout=subprocess.PIPE,
)
call = subprocess.run(
    [{bcftools!r}, "call", "-mv", "-Oz", "-o", out], stdin=mpileup.stdout
)
sys.exit(mpileup.wait() or call.returncode)
"""


class FakeGatk:
    """A `gatk` (HaplotypeCaller) in ``bindir`` that calls with bcftools.

    Like GATK it needs the reference's .dict, its --tmp-dir and, when
    ``windows``, native paths; ``reads_cram=False`` is a GATK that can't read
    CRAM 3.1. It calls the -L intervals (or the whole file). ``fail`` makes it
    fail with that message, and ``on_contig`` maps contigs in its intervals to
    "fail", "slow" (1 s) or "hang" (with a child "java" process; both PIDs go
    to ``pids``). Each run's arguments are appended to ``log`` as JSON.
    """

    def __init__(self, bindir: Path, bcftools: str, **state: object) -> None:
        self.log = bindir / "gatk-runs.jsonl"
        self.pids = bindir / "gatk-pids.txt"
        self._state = bindir / "gatk-state.json"
        self._data: dict = {
            "log": str(self.log),
            "pids": str(self.pids),
            "windows": False,
            "reads_cram": True,
            "fail": None,
            "on_contig": {},
        }
        python_tool(bindir, "gatk", _FAKE_GATK.format(
            state=str(self._state), bcftools=bcftools
        ))  # fmt: skip
        self.set(**state)

    def set(self, **state: object) -> None:
        self._data.update(state)
        self._state.write_text(json.dumps(self._data))

    def runs(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]
