"""Version probes, dependency verification and the `deps check` report, run
against small fake tools on the host's PATH and the fake pixi from
tests/fake_tools.py."""

import io
import logging
import os
import sys
from pathlib import Path

import pytest

from tests.fake_tools import FakePixi, python_tool
from wgsextract_cli import api
from wgsextract_cli.cli.parser import build_parser
from wgsextract_cli.core import dependency_checks, pixi_tools, windows_arm64
from wgsextract_cli.core.dependencies import get_tool_path
from wgsextract_cli.core.dependency_checks import (
    check_all_dependencies,
    get_tool_version,
    verify_dependencies,
)
from wgsextract_cli.core.tool_catalog import required_dependency_tools
from wgsextract_cli.core.utils import MissingDependencyError, WGSExtractError

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="NTSTATUS codes")
# Probing another environment sends pixi a multi-line `python -c` argument,
# which a batch-file pixi stand-in can't pass on.
posix_pixi = pytest.mark.skipif(
    sys.platform == "win32", reason="a batch file can't take a multi-line argument"
)


@pytest.fixture(autouse=True)
def _x64_host(monkeypatch: pytest.MonkeyPatch) -> None:
    # The fake CMD tools exercise probe parsing, not native ARM64 qualification.
    monkeypatch.setattr(windows_arm64, "windows_native_machine", lambda: 0x8664)


@pytest.fixture
def host(fake_pixi: FakePixi, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The host's programs folder, alone on PATH. The CLI runs in the
    workspace's default environment, which has none of them; its lockfile has
    minimap2 and bwa."""
    prefix = fake_pixi.env("default", locked=("minimap2", "bwa"))
    monkeypatch.setenv("PIXI_ENVIRONMENT_NAME", "default")
    monkeypatch.setattr(sys, "prefix", str(prefix))
    return fake_pixi.host_bin


_PRELUDE = """\
import os, signal, sys, time
args = sys.argv[1:]  # the probe argument (--version, --help), if any
def err(text):
    print(text, file=sys.stderr)
def crash(name):
    # Windows has no such signals: it ends a crashed process with an NTSTATUS.
    if sys.platform == "win32":
        sys.stdout.flush()
        status = {"SIGILL": 0xC000001D, "SIGSEGV": 0xC0000005}[name]
        os._exit(status - (1 << 32))  # as a C int
    os.kill(os.getpid(), getattr(signal, name))
"""

# What crash(name) is reported as.
CRASHED = (
    {"SIGILL": "crashed (illegal instruction)", "SIGSEGV": "crashed (access violation)"}
    if sys.platform == "win32"
    else {"SIGILL": "crashed (SIGILL)", "SIGSEGV": "crashed (SIGSEGV)"}
)


def _tool(host: Path, name: str, body: str) -> str:
    """A fake tool on PATH running the Python body after _PRELUDE."""
    return str(python_tool(host, name, _PRELUDE + body + "\n"))


def _same_path(a: str | None, b: str) -> bool:
    return a is not None and os.path.normcase(a) == os.path.normcase(b)


def _runs(log: Path) -> int:
    return len(log.read_text().splitlines()) if log.exists() else 0


def _logging_runs(log: Path) -> str:
    """Tool body: record each start in log."""
    return f"open({str(log)!r}, 'a').write('run\\n')\n"


# Version probes.


@pytest.mark.parametrize(
    ("tool", "body", "expected"),
    [
        (
            "bcftools",
            'print("bcftools 1.21"); print("Using htslib 1.21")',
            "bcftools 1.21",
        ),
        # bwa has no --version: it prints its usage and fails, bare or not.
        (
            "bwa",
            'err("Program: bwa (alignment)"); err("Version: 0.7.18-r1243"); sys.exit(1)',
            "Version: 0.7.18-r1243",
        ),
        # bwa-mem2 prints its usage for --version; `version` prints the version.
        (
            "bwa-mem2",
            'if args != ["version"]: print("Usage: bwa-mem2 <command>"); sys.exit(1)\n'
            'err("Launching executable bwa-mem2.avx2"); print("2.3")',
            "2.3",
        ),
        (
            "delly",
            'print("DEBUG: probing"); print("Delly version: v1.2.6")',
            "Delly version: v1.2.6",
        ),
        (
            "vep",
            "if args: sys.exit(2)\n"
            'print("#----#"); print("Versions:"); print("  ensembl-vep : 113.0")',
            "ensembl-vep : 113.0",
        ),
        ("sniffles", "sys.exit(1)", "Available"),
        (
            "pbsv",
            'if args: sys.exit(2)\nprint("Usage: pbsv <tool>"); print("Tools: discover call")',
            "Available",
        ),
    ],
)
def test_version_probe_reads_each_tools_way_of_reporting(host, tool, body, expected):
    _tool(host, tool, body)

    assert get_tool_version(tool) == expected


def test_version_probe_without_the_tool(host):
    assert get_tool_version("samtools") is None


@posix_only
def test_a_loader_abort_is_a_crash(host):
    _tool(
        host,
        "samtools",
        'err("dyld[1]: Library not loaded: @rpath/libhts.3.dylib"); sys.exit(134)',
    )

    # macOS' loader aborts the process: SIGABRT, reported as a crash.
    assert get_tool_version("samtools") == "Error: samtools crashed (SIGABRT)"


@pytest.mark.parametrize(
    ("body", "crashed"),
    [
        pytest.param(
            "os.kill(os.getpid(), signal.SIGABRT)", "SIGABRT", marks=posix_only
        ),
        # A wrapper shell reports its child's death by signal N as 128 + N.
        pytest.param("sys.exit(132)", "SIGILL", marks=posix_only),
        pytest.param(
            "os.kill(os.getpid(), 35)",
            "signal 35",
            marks=pytest.mark.skipif(
                not sys.platform.startswith("linux"), reason="Linux's real-time signals"
            ),
        ),
        ('crash("SIGSEGV")', CRASHED["SIGSEGV"].removeprefix("crashed (")[:-1]),
        pytest.param(
            "os._exit(0xC0000135 - (1 << 32))",
            "a DLL it needs was not found",
            marks=windows_only,
        ),
        pytest.param(
            "os._exit(0xC0000417 - (1 << 32))",
            "NTSTATUS 0xC0000417",
            marks=windows_only,
        ),
    ],
)
def test_a_tool_that_dies_is_reported_crashed(host, body, crashed):
    _tool(host, "samtools", body)

    assert get_tool_version("samtools") == f"Error: samtools crashed ({crashed})"


@pytest.mark.parametrize("code", [0, 1, 127, 128, 160, 255])
def test_ordinary_exit_codes_are_not_crashes(host, code):
    _tool(host, "samtools", f'print("samtools 1.21"); sys.exit({code})')

    version = get_tool_version("samtools")

    assert version is not None and "crashed" not in version


def test_version_probe_reports_a_missing_library(host):
    body = 'err("dyld[1]: Library not loaded: @rpath/libhts.3.dylib"); sys.exit(1)'
    _tool(host, "samtools", body)

    assert get_tool_version("samtools") == (
        "Error: dyld[1]: Library not loaded: @rpath/libhts.3.dylib"
    )


def test_version_probe_reports_a_missing_shared_library_on_linux(host):
    body = (
        'err("samtools: error while loading shared libraries: libhts.so.3: '
        'cannot open shared object file: No such file or directory"); sys.exit(127)'
    )
    _tool(host, "samtools", body)

    version = get_tool_version("samtools")

    assert version is not None and version.startswith("Error: samtools: error while")


def test_a_wrapper_whose_target_is_not_found_is_an_error(host):
    body = 'err("/env/bin/tabix: 2: exec: tabix.real: not found"); sys.exit(127)'
    _tool(host, "tabix", body)

    assert get_tool_version("tabix") == (
        "Error: /env/bin/tabix: 2: exec: tabix.real: not found"
    )


def test_not_found_in_help_text_is_no_error(host):
    help_text = 'print("usage: fastp"); print("  exits 3 if the adapter is not found")'
    _tool(host, "fastp", f"{help_text}; sys.exit(1)")

    assert get_tool_version("fastp") == "Available"


def test_a_timed_out_probe_is_retried_with_a_longer_limit(host, tmp_path, monkeypatch):
    """A tool's first start on a fresh Windows PC can take far longer than
    later ones; this one takes longer than the first limit, but not the
    second, every time."""
    log = tmp_path / "runs.log"
    _tool(
        host, "samtools", _logging_runs(log) + 'time.sleep(4); print("samtools 1.21")'
    )
    # The clock: the real limits are a minute, then three.
    monkeypatch.setattr(dependency_checks, "VERSION_PROBE_TIMEOUT", 2)
    monkeypatch.setattr(dependency_checks, "VERSION_PROBE_RETRY_TIMEOUT", 60)

    assert get_tool_version("samtools") == "samtools 1.21"
    assert _runs(log) == 2
    # Cached: a second probe in the same process runs nothing.
    assert get_tool_version("samtools") == "samtools 1.21"
    assert _runs(log) == 2


def test_a_probe_that_times_out_twice_is_slow_to_start(host, monkeypatch):
    _tool(host, "samtools", "time.sleep(3)")
    monkeypatch.setattr(dependency_checks, "VERSION_PROBE_TIMEOUT", 1)
    monkeypatch.setattr(dependency_checks, "VERSION_PROBE_RETRY_TIMEOUT", 2)

    with pytest.raises(MissingDependencyError) as error:
        verify_dependencies(["samtools"])

    assert str(error.value).startswith("samtools is slow to start: ")
    assert str(error.value).endswith("did not finish in 1 seconds, nor in 2 on a retry")
    # Not cached: once the tool starts promptly, the same process finds it.
    _tool(host, "samtools", 'print("samtools 1.21")')
    assert get_tool_version("samtools") == "samtools 1.21"


@posix_only
def test_a_probe_gives_the_tool_no_input(host, monkeypatch):
    """A tool that reads its input must not wait on the user's terminal: here,
    a pipe that never closes."""
    _tool(host, "samtools", 'sys.stdin.read(); print("samtools 1.21")')
    monkeypatch.setattr(dependency_checks, "VERSION_PROBE_TIMEOUT", 2)
    monkeypatch.setattr(dependency_checks, "VERSION_PROBE_RETRY_TIMEOUT", 2)
    read_end, write_end = os.pipe()
    stdin = os.dup(0)
    os.dup2(read_end, 0)
    try:
        version = get_tool_version("samtools")
    finally:
        os.dup2(stdin, 0)
        for fd in (stdin, read_end, write_end):
            os.close(fd)

    assert version == "samtools 1.21"


# Tools in another Pixi environment.


@posix_pixi
def test_a_tool_in_another_installed_environment_is_probed_there(host, fake_pixi):
    prefix = fake_pixi.env("pacbio", locked=("sniffles",))
    python_tool(prefix / "bin", "sniffles", 'print("Sniffles2 2.6.3")')

    assert get_tool_version("sniffles") == "Sniffles2 2.6.3"


@posix_pixi
@pytest.mark.parametrize("change", ["uninstalled", "unreadable pixi info"])
def test_a_probe_never_installs_an_environment(host, fake_pixi, tmp_path, change):
    """An environment can go after the tool was found: the probe reports it,
    and the command that installs it, without running the tool (which
    `pixi run` would install the environment for)."""
    log = tmp_path / "runs.log"
    prefix = fake_pixi.env("pacbio", locked=("sniffles",))
    python_tool(prefix / "bin", "sniffles", _logging_runs(log) + 'print("2.6.3")')
    assert get_tool_path("sniffles") is not None
    if change == "uninstalled":
        fake_pixi.uninstall("pacbio")
    else:
        fake_pixi.set(info_stdout="WARN pixi is updating")

    version = get_tool_version("sniffles")

    assert version is not None
    assert version.startswith("Error: Pixi env 'pacbio' is not installed. Run: ")
    assert "install --locked" in version and version.endswith("-e pacbio")
    assert _runs(log) == 0
    assert fake_pixi.installed("pacbio") == (change != "uninstalled")


# Verifying dependencies.


def test_verify_rejects_a_pre_1_0_samtools(host):
    old = 'print("Program: samtools"); print("Version: 0.1.19-44428cd"); sys.exit(1)'
    _tool(host, "samtools", old)
    _tool(host, "bcftools", 'print("bcftools 1.21")')

    verify_dependencies(["bcftools"])
    with pytest.raises(WGSExtractError, match=r"samtools version Version: 0\.1\.19"):
        verify_dependencies(["samtools", "bcftools"])


def test_verify_rejects_a_samtools_that_crashes(host):
    _tool(host, "samtools", 'crash("SIGILL")')

    with pytest.raises(WGSExtractError) as error:
        verify_dependencies(["samtools"])
    assert str(error.value) == f"samtools does not run: samtools {CRASHED['SIGILL']}"


def test_check_all_dependencies_marks_crashing_tools_missing(host):
    samtools = _tool(host, "samtools", 'print("samtools 1.21")')
    _tool(host, "bcftools", 'crash("SIGSEGV")')
    _tool(host, "sniffles", 'print("Sniffles2 2.6.3")')

    results = check_all_dependencies(
        ["python3", "samtools", "bcftools"], ["sniffles", "minimap2"]
    )

    by_name = {r["name"]: r for r in results["mandatory"] + results["optional"]}
    assert list(by_name) == [
        "Python Runtime",
        "samtools",
        "bcftools",
        "sniffles",
        "minimap2",
    ]
    assert by_name["Python Runtime"]["path"] == sys.executable
    assert _same_path(by_name["samtools"]["path"], samtools)
    assert by_name["samtools"]["runtime"] == "native"
    assert by_name["samtools"]["version"] == "samtools 1.21"
    assert by_name["bcftools"]["path"] is None
    assert by_name["bcftools"]["runtime"] == "missing"
    assert by_name["bcftools"]["version"] == f"Error: bcftools {CRASHED['SIGSEGV']}"
    assert by_name["sniffles"]["version"] == "Sniffles2 2.6.3"
    assert by_name["minimap2"]["path"] is None
    assert by_name["minimap2"]["version"].startswith("Run: ")
    assert "install --locked" in by_name["minimap2"]["version"]


def test_log_dependency_info(host, caplog):
    samtools = _tool(host, "samtools", 'print("samtools 1.21")')
    caplog.set_level(logging.DEBUG)

    dependency_checks.log_dependency_info(["samtools", "bwa"])

    assert f"samtools -> {get_tool_path('samtools')} (samtools 1.21)" in caplog.text
    assert _same_path(get_tool_path("samtools"), samtools)
    assert "bwa -> NOT FOUND" in caplog.text


# `deps check`


def test_deps_check_tool_prints_its_path(host, capsys):
    samtools = _tool(host, "samtools", "")

    found = api.deps.check(api.DepsCheckParams(tool="samtools"))
    assert _same_path(found.text.removesuffix("\n"), samtools)
    assert _same_path(found.details["path"], samtools)

    with pytest.raises(api.CommandFailed) as error:
        api.deps.check(api.DepsCheckParams(tool="bwa"))
    assert error.value.exit_code == 3
    assert str(error.value).startswith("Tool not found: bwa. Run: ")


def test_deps_check_tool_with_debug_logs_its_path_instead(host, capsys):
    samtools = _tool(host, "samtools", "")

    parser, _ = build_parser()
    for argv, printed in (([], True), (["--debug"], False)):
        args = parser.parse_args(["deps", "check", "--tool", "samtools", *argv])
        args.func(args)
        out = capsys.readouterr().out.removesuffix("\n")
        assert _same_path(out, samtools) if printed else out == ""


# The report lists these, but a user can run most commands without them.
OPTIONAL_TOOLS = ("fastqc", "fastp", "curl")


def _mandatory_tools(host: Path, *, without: tuple[str, ...] = ()) -> None:
    for tool in required_dependency_tools():
        if tool not in without + OPTIONAL_TOOLS:
            _tool(host, tool, f'print("{tool} 1.21")')


def _report(monkeypatch, encoding: str | None) -> tuple[dict[str, str], str | None]:
    """Run `deps check` with stdout in this encoding (None: a StringIO, which
    has none). Returns its rows by tool name, and the error it ends with."""
    stdout = (
        io.StringIO()
        if encoding is None
        else io.TextIOWrapper(io.BytesIO(), encoding=encoding)
    )
    monkeypatch.setattr(sys, "stdout", stdout)
    result = api.deps.check(api.DepsCheckParams(), check=False)
    error = result.error.message if result.error else None
    rows = {line.split()[1]: line for line in result.text.splitlines() if " " in line}
    return rows, error


@pytest.mark.parametrize(
    ("encoding", "found", "warning", "missing"),
    [
        ("utf-8", "✅", "⚠️ ", "❌"),
        (None, "✅", "⚠️ ", "❌"),
        # A Windows console in a legacy code page.
        ("ascii", "OK", "WARN", "MISS"),
    ],
    ids=["utf-8", "no encoding", "ascii"],
)
def test_deps_check_report(host, monkeypatch, encoding, found, warning, missing):
    _mandatory_tools(host, without=("bwa",))
    _tool(host, "fastp", 'print("fastp 0.24.0")')

    rows, error = _report(monkeypatch, encoding)

    assert rows["samtools"].startswith(f"{found} ")
    assert rows["samtools"].endswith("[native] (samtools 1.21)")
    assert rows["fastp"].startswith(f"{found} ")
    assert rows["bwa"].startswith(f"{missing} ")
    assert rows["minimap2"].startswith(f"{warning} ")
    for tool in ("bwa", "minimap2"):
        assert "[missing] (Run: " in rows[tool]
        assert rows[tool].endswith("-e default)")
    assert error == (
        "Missing mandatory tool(s): bwa. Install them to ensure full functionality."
    )


@posix_pixi
@pytest.mark.parametrize(
    ("encoding", "marker"), [("utf-8", "🔵"), ("ascii", "ALT")], ids=["utf-8", "ascii"]
)
def test_deps_check_report_marks_a_tool_from_another_environment(
    host, fake_pixi, monkeypatch, encoding, marker
):
    _mandatory_tools(host)
    prefix = fake_pixi.env("pacbio", locked=("sniffles",))
    python_tool(prefix / "bin", "sniffles", 'print("Sniffles2 2.6.3")')

    rows, error = _report(monkeypatch, encoding)

    assert rows["sniffles"].startswith(f"{marker} ")
    assert rows["sniffles"].endswith("[pixi alt env: pacbio] (Sniffles2 2.6.3)")
    assert error is None  # every mandatory tool is there


# pixi_tools: telling Pixi commands and paths apart.


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("/home/me/.pixi/bin/pixi run -e gatk gatk", True),
        ("pixi.exe run --manifest-path a/pixi.toml -e vep vep", True),
        ("C:/tools/pixi.cmd run -e vep vep", True),
        ("pixi info", False),
        ("/usr/bin/samtools", False),
        ("", False),
        ('pixi run "unbalanced', False),
    ],
)
def test_is_pixi_tool_command(command, expected):
    assert pixi_tools.is_pixi_tool_command(command) is expected


def test_is_pixi_tool_path_needs_an_active_pixi_env(tmp_path, monkeypatch):
    env = tmp_path / "env"
    (env / "bin").mkdir(parents=True)
    tool = env / "bin" / "samtools"
    # Every PIXI_* variable: tests themselves may run under `pixi run`.
    for name in [n for n in os.environ if n.startswith("PIXI_")] + ["CONDA_PREFIX"]:
        monkeypatch.delenv(name, raising=False)

    assert not pixi_tools.is_pixi_tool_path(str(tool))
    monkeypatch.setenv("CONDA_PREFIX", str(env))
    assert not pixi_tools.is_pixi_tool_path(str(tool))  # a plain conda env
    monkeypatch.setenv("PIXI_PROJECT_ROOT", str(tmp_path))
    assert pixi_tools.is_pixi_tool_path(str(tool))
    assert not pixi_tools.is_pixi_tool_path("/usr/bin/samtools")

    monkeypatch.delenv("CONDA_PREFIX")
    monkeypatch.delenv("PIXI_PROJECT_ROOT")
    monkeypatch.setenv("PIXI_PREFIX", str(env))
    assert pixi_tools.is_pixi_tool_path(str(tool))
