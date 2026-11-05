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
if " " in tmp:
    sys.exit("htsjdk.tribble.TribbleException: Unable to automatically instantiate codec")
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

    Like GATK 4.6 it needs the reference's .dict, a --tmp-dir without a space
    and, when ``windows``, native paths; ``reads_cram=False`` is a GATK that can't read
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


_FAKE_CLAIR3 = """\
import json, os, select, subprocess, sys

if sys.argv[1:] in ([], ["--version"], ["-h"], ["--help"]):
    print("Clair3 v2.0.0")
    sys.exit(0)
opts = dict(arg[2:].split("=", 1) for arg in sys.argv[1:])
# Clair3's GNU parallel reads stdin: it must be closed, or the run waits on it.
ready, _, _ = select.select([0], [], [], 0.5)
record = {{
    "opts": opts,
    "bed": open(opts["bed_fn"]).read() if "bed_fn" in opts else None,
    "stdin_closed": bool(ready) and os.read(0, 1) == b"",
    "pythonpath": os.environ.get("PYTHONPATH"),
}}
with open({log!r}, "a") as log:
    log.write(json.dumps(record) + "\\n")
if not record["stdin_closed"]:
    sys.exit("parallel: stdin is still open")
if record["pythonpath"]:
    sys.exit("ImportError: Clair3's Python loaded modules from PYTHONPATH")
for name in ("pileup.pt", "full_alignment.pt"):
    if not os.path.isfile(os.path.join(opts["model_path"], name)):
        sys.exit(f"[ERROR] {{name}} not found in {{opts['model_path']}}")
out = opts["output"]
os.makedirs(out)
regions = ["-R", opts["bed_fn"]] if "bed_fn" in opts else []
calls = os.path.join(out, "calls.vcf.gz")
mpileup = subprocess.Popen(
    [{bcftools!r}, "mpileup", "-f", opts["ref_fn"], *regions, "-Ou", opts["bam_fn"]],
    stdout=subprocess.PIPE,
)
subprocess.run([{bcftools!r}, "call", "-mv", "-Oz", "-o", calls], stdin=mpileup.stdout, check=True)
with open(os.path.join(out, "sample.txt"), "w") as names:
    names.write(opts["sample_name"] + "\\n")
subprocess.run(
    [{bcftools!r}, "reheader", "-s", names.name, "-o",
     os.path.join(out, "merge_output.vcf.gz"), calls],
    check=True,
)
"""


class FakeClair3:
    """A `run_clair3.sh` in ``bindir`` that calls with bcftools, with its
    bundled ``models`` folder beside it, as Bioconda installs it.

    Like Clair3 it needs the model's weights, runs its own Python (it fails
    if PYTHONPATH leaks in) and waits on an open stdin (it fails instead).
    Each run's options, region BED, stdin state and PYTHONPATH go to ``runs``.
    """

    def __init__(self, bindir: Path, bcftools: str) -> None:
        self.log = bindir / "clair3-runs.jsonl"
        self.models = bindir / "models"
        self.models.mkdir(parents=True)
        python_tool(bindir, "run_clair3.sh", _FAKE_CLAIR3.format(
            log=str(self.log), bcftools=bcftools
        ))  # fmt: skip

    def runs(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]


# What DeepVariant's tools share: the fake's state and log, its options, and
# the failures a test asks for.
_DV_COMMON = r"""
import json, os, re, select, signal, subprocess, sys, time


def state():
    with open(os.path.join(ROOT, "state.json")) as source:
        return json.load(source)


def log(record):
    with open(os.path.join(ROOT, "runs.jsonl"), "a") as out:
        out.write(json.dumps(record) + "\n")


def options(argv):
    opts, i = {}, 0
    while i < len(argv):
        name, eq, value = argv[i][2:].partition("=")
        if eq:
            opts[name], i = value, i + 1
        else:
            opts[name], i = argv[i + 1], i + 2
    return opts


def own_python():
    # DeepVariant runs its own Python, which our PYTHONPATH would break.
    if os.environ.get("PYTHONPATH"):
        sys.exit("ImportError: DeepVariant's Python loaded modules from PYTHONPATH")


def shard_names(examples):
    stem, shards, suffix = re.fullmatch(r"(.*)@(\d+)(\..*)", examples).groups()
    return [f"{stem}-{i:05d}-of-{int(shards):05d}{suffix}" for i in range(int(shards))]


def fail_here(stage, task=None, write=lambda: None):
    failure = state()["fail"]
    if not failure or failure["stage"] != stage:
        return
    if failure.get("task") is not None and failure["task"] != task:
        return
    if failure["write"]:
        write()
    if failure["oom"]:
        open(os.path.join(os.environ["FAKE_OOM_DIR"], os.environ["FAKE_CONTAINER"]), "w").close()
    if failure["interrupt"]:
        os.kill(failure["interrupt"], signal.SIGINT)
        time.sleep(30)
    sys.exit(137)
"""

_DV_RUN_DEEPVARIANT = r"""
opts = options([a for a in sys.argv[1:] if a != "--dry_run=true"])
ready, _, _ = select.select([0], [], [], 0.5)
stdin_closed = bool(ready) and os.read(0, 1) == b""
log({"tool": "run_deepvariant", "opts": opts, "stdin_closed": stdin_closed,
     "env": {k: os.environ.get(k) for k in ("HOME", "TMPDIR")}})
own_python()
# Its GNU parallel reads stdin: an open one would make the run wait.
if not stdin_closed:
    sys.exit("parallel: stdin is still open")
if "--dry_run=true" not in sys.argv:
    sys.exit("this fake only plans: give --dry_run=true")
image = os.environ.get("FAKE_DV_IN_IMAGE")
bindir = "/opt/deepvariant/bin" if image else os.path.dirname(os.path.abspath(sys.argv[0]))
work, shards = opts["intermediate_results_dir"], int(opts["num_shards"])
examples = f"{work}/make_examples.tfrecord@{shards}.gz"
outputs = f"{work}/call_variants_output.tfrecord.gz"
regions = f' --regions "{opts["regions"]}"' if "regions" in opts else ""
model = opts.get("customized_model", f"/opt/models/{opts['model_type'].lower()}")
extra = {}
for flag in ("call_variants_extra_args", "postprocess_variants_extra_args"):
    if flag in opts:
        extra[flag] = "".join(
            f' --{k} "{v}"' for k, v in (kv.split("=") for kv in opts[flag].split(","))
        )
commands = [
    f"time seq 0 {shards - 1} | parallel -q --halt 2 --line-buffer {bindir}/make_examples "
    f'--mode calling --ref "{opts["ref"]}" --reads "{opts["reads"]}" '
    f'--examples "{examples}"{regions} --task {{}}',
    f'time {bindir}/call_variants --outfile "{outputs}" --examples "{examples}" '
    f'--checkpoint "{model}"{extra.get("call_variants_extra_args", "")}',
    f'time {bindir}/postprocess_variants --ref "{opts["ref"]}" --infile "{outputs}" '
    f'--outfile "{opts["output_vcf"]}" --cpus "{opts.get("postprocess_cpus", shards)}"'
    f'{extra.get("postprocess_variants_extra_args", "")}',
]
print("".join(f"\n***** Running the command:*****\n{c}\n\n" for c in commands))
"""

_DV_PARALLEL = r"""
args, jobs, joblog, resume = sys.argv[1:], None, None, False
while args[0].startswith("-"):
    arg = args.pop(0)
    if arg == "--jobs":
        jobs = args.pop(0)
    elif arg == "--joblog":
        joblog = args.pop(0)
    elif arg == "--resume-failed":
        resume = True
    elif arg == "--halt":
        args.pop(0)
done = set()
if joblog and resume and os.path.exists(joblog):
    for line in open(joblog).read().splitlines()[1:]:
        fields = line.split("\t")
        if fields[6] == "0":
            done.add(fields[0])
elif joblog:
    with open(joblog, "w") as out:
        out.write("Seq\tHost\tStarttime\tJobRuntime\tSend\tReceive\tExitval\tSignal\tCommand\n")
ran, code = [], 0
for task in sys.stdin.read().split():
    if task in done:
        continue
    ran.append(task)
    command = [task if a == "{}" else a for a in args]
    code = subprocess.run(command, stdin=subprocess.DEVNULL).returncode
    if joblog:
        with open(joblog, "a") as out:
            out.write(f"{task}\t:\t0\t0\t0\t0\t{code}\t0\t{' '.join(command)}\n")
    if code:
        break  # --halt 2: stop at the first failure
log({"tool": "parallel", "jobs": jobs, "joblog": joblog, "resume_failed": resume, "tasks": ran})
sys.exit(code)
"""

_DV_MAKE_EXAMPLES = r"""
opts = options(sys.argv[1:])
own_python()
task, reads, ref = int(opts["task"]), opts["reads"], opts["ref"]
with open(reads, "rb") as source:
    head = source.read(6)
version = list(head[4:6]) if head[:4] == b"CRAM" else None
if version and version >= [3, 1]:
    # The images' htslib 1.18 can't decode some valid CRAM 3.1 blocks.
    sys.exit("[E::rans_uncompress_O1_4x16] Failed to decode a CRAM 3.1 block")
if not any(os.path.exists(reads + s) for s in (".bai", ".crai", ".csi")):
    sys.exit(f"[E::idx_find_and_load] Could not retrieve index file for '{reads}'")
for selection in (opts.get("regions") or "").split():
    if selection.endswith((".bed", ".bed.gz", ".bedpe")) and not os.path.isfile(selection):
        sys.exit(f"ValueError: Not found: Could not open {selection}")
if task == 0:
    view = subprocess.run([SAMTOOLS, "view", "-T", ref, reads], capture_output=True, text=True, check=True)
    log({"tool": "make_examples", "reads": reads, "cram_version": version,
         "names": sorted(line.split("\t")[0] for line in view.stdout.splitlines()),
         "regions": opts.get("regions"), "tmpdir": os.environ.get("TMPDIR")})
shard = shard_names(opts["examples"])[task]


def write():
    with open(shard, "w") as out:
        json.dump({"ref": ref, "reads": reads}, out)


fail_here("make_examples", task, write)
write()
"""

_DV_CALL_VARIANTS = r"""
opts = options(sys.argv[1:])
own_python()
shards = shard_names(opts["examples"])
if not all(os.path.exists(s) for s in shards):
    sys.exit(f"ValueError: Cannot find matching files with the pattern {opts['examples']}")
log({"tool": "call_variants", "opts": opts})


def write():
    with open(shards[0]) as source, open(opts["outfile"], "w") as out:
        out.write(source.read())


fail_here("call_variants", None, write)
write()
"""

_DV_POSTPROCESS_VARIANTS = r"""
if sys.argv[1:] == ["--helpfull"]:
    print(state()["helpfull"])
    sys.exit(0)
opts = options(sys.argv[1:])
own_python()
log({"tool": "postprocess_variants", "opts": opts})
with open(opts["infile"]) as source:
    called = json.load(source)


def write():
    mpileup = subprocess.Popen(
        [BCFTOOLS, "mpileup", "-f", called["ref"], "-Ou", called["reads"]], stdout=subprocess.PIPE
    )
    subprocess.run([BCFTOOLS, "call", "-mv", "-Oz", "-o", opts["outfile"]], stdin=mpileup.stdout, check=True)
    subprocess.run([BCFTOOLS, "index", "-t", opts["outfile"]], check=True)


fail_here("postprocess_variants", None, write)
write()
"""


class FakeDeepVariant:
    """DeepVariant's run_deepvariant and stage tools in ``root/bin``, as a
    local install has them and its image runs them (see FakeDocker).

    run_deepvariant --dry_run=true prints its stage commands; make_examples
    runs under a GNU parallel that keeps a job log, and like the images'
    htslib 1.18 can't read CRAM 3.1; postprocess_variants calls the reads
    with bcftools. Each tool fails if our PYTHONPATH leaks into it, and
    run_deepvariant if stdin is open. ``fail`` makes a stage die; ``runs``
    are what each tool was asked to do.
    """

    def __init__(self, root: Path, samtools: str, bcftools: str) -> None:
        self.root, self.bin = root, root / "bin"
        self.bin.mkdir(parents=True)
        self._state: dict = {"fail": None, "helpfull": "  --num_partitions: ..."}
        self._save()
        header = (
            f"ROOT = {str(root)!r}\nSAMTOOLS = {samtools!r}\nBCFTOOLS = {bcftools!r}\n"
            + _DV_COMMON
        )
        for name, body in (
            ("run_deepvariant", _DV_RUN_DEEPVARIANT),
            ("parallel", _DV_PARALLEL),
            ("make_examples", _DV_MAKE_EXAMPLES),
            ("call_variants", _DV_CALL_VARIANTS),
            ("postprocess_variants", _DV_POSTPROCESS_VARIANTS),
        ):
            python_tool(self.bin, name, header + body)

    def _save(self) -> None:
        (self.root / "state.json").write_text(json.dumps(self._state))

    def fail(
        self,
        stage: str | None,
        *,
        task: int | None = None,
        write: bool = True,
        oom: bool = False,
        interrupt: int | None = None,
    ) -> None:
        """Make ``stage`` (shard ``task`` of make_examples) exit 137, after
        writing its output if ``write``; ``oom`` as an OOM kill in its
        container, ``interrupt`` after sending SIGINT to that PID (Ctrl-C).
        None stops failing."""
        self._state["fail"] = stage and {
            "stage": stage, "task": task, "write": write, "oom": oom,
            "interrupt": interrupt,
        }  # fmt: skip
        self._save()

    def set_helpfull(self, text: str) -> None:
        """What postprocess_variants --helpfull prints."""
        self._state["helpfull"] = text
        self._save()

    def runs(self, tool: str | None = None) -> list[dict]:
        log = self.root / "runs.jsonl"
        records = (
            [json.loads(line) for line in log.read_text().splitlines()]
            if log.exists()
            else []
        )
        return [r for r in records if tool in (None, r["tool"])]

    def stages(self) -> list[str]:
        """The stages that started, in order (make_examples once per run)."""
        stage = {"parallel": "make_examples", "call_variants": "call_variants",
                 "postprocess_variants": "postprocess_variants"}  # fmt: skip
        return [stage[r["tool"]] for r in self.runs() if r["tool"] in stage]

    def clear(self) -> None:
        """Forget the runs so far."""
        (self.root / "runs.jsonl").unlink(missing_ok=True)


_FAKE_DOCKER = r"""
import json, os, re, signal, subprocess, sys, time


def state():
    with open(os.path.join(ROOT, "state.json")) as source:
        return json.load(source)


def log(record):
    with open(os.path.join(ROOT, "docker.jsonl"), "a") as out:
        out.write(json.dumps(record) + "\n")


def flag(args, name):
    return args[args.index(name) + 1]


args, st = sys.argv[1:], state()
running = os.path.join(ROOT, "running")
if args[0] != "run":
    log({"cmd": args[0], "args": args})
if args[0] == "info":
    if st["info_error"]:
        sys.exit(st["info_error"])
    print({"{{json .ServerVersion}}": '"27.3.1"', "{{.Architecture}}": st["arch"],
           "{{.MemTotal}}": str(st["mem_total"]),
           "{{json .Runtimes}}": st["runtimes"]}[flag(args, "--format")])
elif args[:2] == ["image", "inspect"]:
    image = st["images"].get(args[-1])
    if image is None:
        sys.exit(f"Error: No such image: {args[-1]}")
    if "--format" in args:
        print(image["id"] if flag(args, "--format") == "{{.Id}}" else image["platform"])
elif args[0] == "ps":
    if st["ps_error"]:
        sys.exit(st["ps_error"])
    label = flag(args, "--filter").removeprefix("label=")
    for name in sorted(os.listdir(running)):
        if json.load(open(os.path.join(running, name)))["label"] == label:
            print(name)
elif args[0] == "kill":
    # The daemon kills the container and, as it ran with --rm, removes it.
    record = os.path.join(running, args[1])
    try:
        pid = json.load(open(record))["pid"]
        os.unlink(record)
        if pid > 0:
            os.kill(pid, signal.SIGKILL)
    except (OSError, ValueError):
        pass
elif args[0] == "events":
    name = next(f.split("=", 1)[1] for f in args if f.startswith("container="))
    while not os.path.exists(os.path.join(ROOT, "oom", name)):
        time.sleep(0.05)
    print("oom", flush=True)
    time.sleep(60)
elif args[0] == "run":
    opts, i = {"-e": [], "-v": []}, 1
    while args[i].startswith("-"):
        if args[i] == "--rm" or args[i].startswith("--pull="):
            opts[args[i].split("=")[0]], i = args[i], i + 1
        elif args[i] in ("-e", "-v"):
            opts[args[i]].append(args[i + 1])
            i += 2
        else:
            opts[args[i]], i = args[i + 1], i + 2
    image, command = args[i], args[i + 1:]
    env = dict(e.split("=", 1) for e in opts["-e"])
    mounts = [v.split(":")[0] for v in opts["-v"]]
    name = opts.get("--name", "anonymous")
    log({"cmd": "run", "image": image, "name": name, "label": opts.get("--label"),
         "entrypoint": opts.get("--entrypoint"), "pull": opts.get("--pull"),
         "env": env, "mounts": mounts, "command": command})
    if image not in st["images"]:
        sys.exit(f"docker: Error response from daemon: No such image: {image}")
    if "--gpus" in opts and not st["gpus"]:
        sys.exit("docker: Error response from daemon: could not select device driver with capabilities: [[gpu]]")
    # Images run as root unless given the caller's ids, leaving outputs in
    # mounted folders the caller can't change (Docker Desktop on Windows maps
    # ownership itself).
    if mounts and hasattr(os, "getuid") and opts.get("-u") != f"{os.getuid()}:{os.getgid()}":
        sys.exit("fake docker: would run as root and leave root-owned outputs")
    if opts.get("--entrypoint"):
        command = [opts["--entrypoint"], *command]
    # The container sees only the folders mounted into it.
    paths = [p for arg in command for p in re.findall(r"/[^\s\"']+", arg)]
    paths += [env.get("HOME"), env.get("TMPDIR"), opts.get("-w")]
    for path in paths:
        # The image's own folders, and its /tmp (HOME when none is given).
        if path and path != "/tmp" and not path.startswith(("/opt/", "/bin/", "/usr/")) and not any(
            os.path.commonpath([path, m]) == m for m in mounts
        ):
            sys.exit(f"{path}: No such file or directory (not mounted in the container)")
    if command[0].startswith("/opt/deepvariant/bin/"):
        command[0] = os.path.join(DV_BIN, os.path.basename(command[0]))
    elif command[:2] == ["/bin/bash", "-c"]:
        command[2] = command[2].replace("/opt/deepvariant/bin/", DV_BIN + "/")
    container = {"PATH": f"{DV_BIN}:/usr/bin:/bin", **env, "FAKE_DV_IN_IMAGE": "1",
                 "FAKE_CONTAINER": name, "FAKE_OOM_DIR": os.path.join(ROOT, "oom")}
    record = os.path.join(running, name)
    process = subprocess.Popen(command, env=container, stdin=subprocess.DEVNULL, cwd=opts.get("-w"))
    with open(record, "w") as out:
        json.dump({"label": opts.get("--label"), "pid": process.pid}, out)
    try:
        code = process.wait()
    finally:
        os.unlink(record)
    sys.exit(code)
else:
    sys.exit(f"fake docker: unknown command {args}")
"""


class FakeDocker:
    """A `docker` in ``bindir`` whose images run FakeDeepVariant's tools from
    ``dv_bin`` as /opt/deepvariant/bin.

    A container sees only the folders mounted into it (paths outside them
    fail), its ``-e`` environment and no stdin, and must run as the caller
    (``-u``) on POSIX when it mounts folders; ``docker events`` reports an
    OOM kill a stage recorded. ``set`` changes the engine's answers: an
    ``info_error``, its ``arch``, ``mem_total`` and ``runtimes``, a
    ``ps_error``, or ``gpus`` (whether ``--gpus`` finds a GPU).
    ``runs`` are the commands it was given.
    """

    def __init__(self, bindir: Path, dv_bin: Path) -> None:
        self.root = bindir.parent / "docker-state"
        for folder in ("running", "oom"):
            (self.root / folder).mkdir(parents=True)
        self._state: dict = {
            "info_error": None, "arch": "x86_64", "mem_total": 0,
            "ps_error": None, "images": {}, "runtimes": '{"runc":{}}',
            "gpus": False,
        }  # fmt: skip
        self._save()
        header = f"ROOT = {str(self.root)!r}\nDV_BIN = {str(dv_bin)!r}\n"
        python_tool(bindir, "docker", header + _FAKE_DOCKER)

    def _save(self) -> None:
        (self.root / "state.json").write_text(json.dumps(self._state))

    def set(self, **answers: object) -> None:
        self._state.update(answers)
        self._save()

    def add_image(
        self, image: str, *, image_id: str = "sha256:1", platform: str = "linux/amd64"
    ) -> None:
        self._state["images"][image] = {"id": image_id, "platform": platform}
        self._save()

    def keep_running(self, name: str, label: str | None) -> None:
        """List container ``name`` as running, as if its wgsextract was killed."""
        (self.root / "running" / name).write_text(
            json.dumps({"label": label, "pid": 0})
        )

    def stop(self, name: str) -> None:
        (self.root / "running" / name).unlink()

    def runs(self, cmd: str | None = "run") -> list[dict]:
        """Its ``cmd`` (run, kill, info, ...) commands, or all of them."""
        log = self.root / "docker.jsonl"
        records = (
            [json.loads(line) for line in log.read_text().splitlines()]
            if log.exists()
            else []
        )
        return [r for r in records if cmd in (None, r["cmd"])]

    def clear(self) -> None:
        """Forget the commands so far."""
        (self.root / "docker.jsonl").unlink(missing_ok=True)


_FAKE_PBRUN = r"""
import json, os, subprocess, sys

tool, args = sys.argv[1], sys.argv[2:]
with open(LOG, "a") as out:
    out.write(json.dumps({"tool": tool, "args": args}) + "\n")


def opt(name):
    return args[args.index(name) + 1] if name in args else None


def every(name):
    return [args[i + 1] for i, arg in enumerate(args) if arg == name]


def pipe(stages, out):
    # Each stage reads the one before it; samtools sort writes out.
    procs = []
    for stage in stages:
        stdin = procs[-1].stdout if procs else subprocess.DEVNULL
        procs.append(subprocess.Popen(stage, stdin=stdin, stdout=subprocess.PIPE))
    cram = ["-O", "cram", "--reference", ref] if out.endswith(".cram") else []
    sort = subprocess.run(
        [TOOLS["samtools"], "sort", *cram, "-o", out, "-"], stdin=procs[-1].stdout
    )
    if any(p.wait() for p in procs) or sort.returncode:
        sys.exit(f"pbrun {tool}: alignment failed")
    subprocess.run([TOOLS["samtools"], "index", out], check=True)
    with open(out.rsplit(".", 1)[0] + "_chrs.txt", "w") as chrs:
        chrs.write("chr1\n")


ref = opt("--ref")
if not os.path.isdir(opt("--tmp-dir")):
    sys.exit("pbrun: --tmp-dir isn't a directory")
if tool == "fq2bam":
    paired = "--in-fq" in args
    at = args.index("--in-fq" if paired else "--in-se-fq") + 1
    reads, rg = args[at : at + 1 + paired], args[at + 1 + paired]
    if "\\tPU:" not in rg:
        sys.exit("pbrun fq2bam: the read group needs a PU")
    pipe([[TOOLS["bwa"], "mem", "-R", rg, ref, *reads]], opt("--out-bam"))
elif tool == "minimap2":
    preset = {"map-hifi": "map-hifi", "map-ont": "map-ont", "map-pbmm2": "map-hifi"}
    mm2 = [TOOLS["minimap2"], "-a", "-y", "-x", preset[opt("--preset")]]
    if "--in-bam" in args:
        # Parabricks keeps an unaligned BAM's own read groups.
        header = subprocess.run([TOOLS["samtools"], "view", "-H", opt("--in-bam")],
                                capture_output=True, text=True, check=True).stdout
        rg = next(l for l in header.splitlines() if l.startswith("@RG"))
        pipe([[TOOLS["samtools"], "fastq", "-T", "RG", opt("--in-bam")],
              [*mm2, "-R", rg.replace("\t", "\\t"), ref, "-"]], opt("--out-bam"))
    else:
        rg = (f"@RG\\tID:{opt('--read-group-id-prefix')}\\tSM:{opt('--read-group-sm')}"
              f"\\tLB:{opt('--read-group-lb')}\\tPL:{opt('--read-group-pl')}")
        pipe([[*mm2, "-R", rg, ref, opt("--in-fq")]], opt("--out-bam"))
elif tool in ("deepvariant", "haplotypecaller"):
    # A homozygous call at the start of each interval it was asked to call.
    starts = []
    for interval in every("-L"):
        if os.path.isabs(interval) or interval.endswith(".bed"):
            sys.exit(f"pbrun {tool}: invalid interval {interval}; "
                     "pass interval files with --interval-file")
        chrom, _, span = interval.partition(":")
        starts.append((chrom, int(span.split("-")[0]) if span else 1))
    for bed in every("--interval-file"):
        with open(bed) as source:
            for line in source:
                chrom, start = line.split()[:2]
                starts.append((chrom, int(start) + 1))
    if not os.path.exists(opt("--in-bam")):
        sys.exit(f"pbrun {tool}: no such input {opt('--in-bam')}")
    with open(ref + ".fai") as fai:
        contigs = [line.split("\t")[:2] for line in fai]
    order = [name for name, _ in contigs]
    lines = ["##fileformat=VCFv4.2"]
    lines += [f"##contig=<ID={name},length={length}>" for name, length in contigs]
    lines += ['##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">',
              "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS"]
    for chrom, pos in sorted(set(starts), key=lambda s: (order.index(s[0]), s[1])):
        base = subprocess.run([TOOLS["samtools"], "faidx", ref, f"{chrom}:{pos}-{pos}"],
                              capture_output=True, text=True, check=True).stdout
        base = base.splitlines()[1].upper()
        alt = "C" if base != "C" else "G"
        lines.append(f"{chrom}\t{pos}\t.\t{base}\t{alt}\t50\tPASS\t.\tGT\t1/1")
    subprocess.run([TOOLS["bcftools"], "view", "-Oz", "-o", opt("--out-variants")],
                   input="\n".join(lines) + "\n", text=True, check=True)
else:
    sys.exit(f"fake pbrun: unknown tool {tool}")
"""


class FakeParabricks:
    """A GPU host: ``bin`` holds an ``nvidia-smi`` and a FakeDocker with the
    NVIDIA runtime and ``image``, whose ``pbrun`` aligns with the real bwa and
    minimap2 (map-pbmm2 as map-hifi) and calls a homozygous variant at the
    start of each interval it's given (-L literals, --interval-file BEDs).
    ``runs`` are the pbrun commands, as (tool, args)."""

    def __init__(self, root: Path, image: str) -> None:
        import shutil

        tools = {
            tool: shutil.which(tool)
            for tool in ("bwa", "minimap2", "samtools", "bcftools")
        }
        self.bin = root / "bin"
        self.log = root / "pbrun.jsonl"
        python_tool(self.bin, "nvidia-smi", "print('NVIDIA-SMI (fake)')")
        header = f"LOG = {str(self.log)!r}\nTOOLS = {tools!r}\n"
        python_tool(root / "image", "pbrun", header + _FAKE_PBRUN)
        self.docker = FakeDocker(self.bin, root / "image")
        self.docker.set(gpus=True, runtimes='{"nvidia":{},"runc":{}}')
        self.docker.add_image(image)

    def runs(self) -> list[tuple[str, list[str]]]:
        if not self.log.exists():
            return []
        records = [json.loads(line) for line in self.log.read_text().splitlines()]
        return [(r["tool"], r["args"]) for r in records]
