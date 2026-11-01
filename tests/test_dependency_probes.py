"""Version probes, dependency verification, Pixi tool resolution and the
`deps check` report, run against small fake executables."""

import ast
import json
import logging
import os
import subprocess
import sys
from argparse import Namespace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests.fake_tools import python_tool
from wgsextract_cli.commands import _deps_status
from wgsextract_cli.core import dependency_checks, pixi_tools, windows_arm64
from wgsextract_cli.core.tool_catalog import JAVA_JAR
from wgsextract_cli.core.utils import WGSExtractError


@pytest.fixture(autouse=True)
def _outside_a_pixi_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests simulate the Pixi environment probe; under `pixi run` the CLI
    would look in its own environment first instead."""
    monkeypatch.delenv("PIXI_ENVIRONMENT_NAME", raising=False)
    # The fake CMD tools exercise probe parsing, not native ARM64 qualification.
    monkeypatch.setattr(windows_arm64, "windows_native_machine", lambda: 0x8664)


_PRELUDE = """\
import os, signal, sys
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
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")


def _tool(tmp_path: Path, name: str, body: str) -> str:
    """A fake tool running the Python body after _PRELUDE."""
    return str(python_tool(tmp_path / "bin", name, _PRELUDE + body + "\n"))


def _use_tools(monkeypatch, tools: dict[str, str | None]) -> list[tuple]:
    calls: list[tuple] = []

    def get_tool_path(tool):
        calls.append((tool,))
        return tools.get(tool)

    monkeypatch.setattr(dependency_checks, "get_tool_path", get_tool_path)
    monkeypatch.setattr(pixi_tools, "_resolve_pixi_command", lambda: "pixi")
    monkeypatch.setattr(pixi_tools, "_locked_package_available", lambda *args: True)
    monkeypatch.setattr(
        pixi_tools, "_pixi_manifest_args", lambda: ["--manifest-path", "/app/pixi.toml"]
    )
    return calls


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
        (
            "yleaf",
            'print("usage: Yleaf [-h]"); print("Yleaf: v3.2.1")',
            "Yleaf: v3.2.1",
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
def test_version_probe_reads_each_tools_way_of_reporting(
    tmp_path, monkeypatch, tool, body, expected
):
    _use_tools(monkeypatch, {tool: _tool(tmp_path, tool, body)})

    assert dependency_checks.get_tool_version(tool) == expected


@posix_only
def test_a_loader_abort_is_a_crash(tmp_path, monkeypatch):
    body = 'err("dyld[1]: Library not loaded: @rpath/libhts.3.dylib"); sys.exit(134)'
    _use_tools(monkeypatch, {"samtools": _tool(tmp_path, "samtools", body)})

    # macOS' loader aborts the process: SIGABRT, reported as a crash.
    assert dependency_checks.get_tool_version("samtools") == (
        "Error: samtools crashed (SIGABRT)"
    )


def test_version_probe_reports_a_missing_library(tmp_path, monkeypatch):
    body = 'err("dyld[1]: Library not loaded: @rpath/libhts.3.dylib"); sys.exit(1)'
    _use_tools(monkeypatch, {"samtools": _tool(tmp_path, "samtools", body)})
    assert dependency_checks.get_tool_version("samtools") == (
        "Error: dyld[1]: Library not loaded: @rpath/libhts.3.dylib"
    )


def test_a_wrapper_whose_target_is_not_found_is_an_error(tmp_path, monkeypatch):
    body = 'err("/env/bin/tabix: 2: exec: tabix.real: not found"); sys.exit(127)'
    _use_tools(monkeypatch, {"tabix": _tool(tmp_path, "tabix", body)})

    assert dependency_checks.get_tool_version("tabix") == (
        "Error: /env/bin/tabix: 2: exec: tabix.real: not found"
    )


def test_not_found_in_help_text_is_no_error(tmp_path, monkeypatch):
    help_text = 'print("usage: fastp"); print("  exits 3 if the adapter is not found")'
    _use_tools(
        monkeypatch, {"fastp": _tool(tmp_path, "fastp", f"{help_text}; sys.exit(1)")}
    )

    assert dependency_checks.get_tool_version("fastp") == "Available"


def test_version_probe_reports_a_missing_shared_library_on_linux(tmp_path, monkeypatch):
    body = (
        'err("samtools: error while loading shared libraries: libhts.so.3: '
        'cannot open shared object file: No such file or directory"); sys.exit(127)'
    )
    _use_tools(monkeypatch, {"samtools": _tool(tmp_path, "samtools", body)})

    version = dependency_checks.get_tool_version("samtools")

    assert version is not None and version.startswith("Error:")


def test_version_probe_without_the_tool_or_on_timeout(tmp_path, monkeypatch):
    _use_tools(monkeypatch, {})
    assert dependency_checks.get_tool_version("samtools") is None

    samtools = _tool(tmp_path, "samtools", "")
    _use_tools(monkeypatch, {"samtools": samtools})

    def hang(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(dependency_checks.subprocess, "run", hang)
    assert dependency_checks.get_tool_version("samtools") == (
        f"Error: Command '{[samtools, '--version']}' "
        f"timed out after {dependency_checks.VERSION_PROBE_TIMEOUT} seconds"
    )


@pytest.mark.parametrize(
    ("returncode", "expected"),
    [
        pytest.param(-6, "crashed (SIGABRT)", marks=posix_only),
        pytest.param(132, "crashed (SIGILL)", marks=posix_only),
        pytest.param(-77, "crashed (signal 77)", marks=posix_only),
        (0xC0000005, "crashed (access violation)"),
        (0xC0000135, "crashed (a DLL it needs was not found)"),
        (0xC0000417, "crashed (NTSTATUS 0xC0000417)"),
    ],
)
def test_crash_description(returncode, expected):
    assert dependency_checks._crash_description(returncode) == expected


@pytest.mark.parametrize("returncode", [0, 1, 127, 128, 160, 255])
def test_ordinary_exit_codes_are_not_crashes(returncode):
    assert dependency_checks._crash_description(returncode) is None


def test_verify_rejects_a_pre_1_0_samtools(tmp_path, monkeypatch):
    old = 'print("Program: samtools"); print("Version: 0.1.19-44428cd"); sys.exit(1)'
    _use_tools(
        monkeypatch,
        {
            "samtools": _tool(tmp_path, "samtools", old),
            "bcftools": _tool(tmp_path, "bcftools", 'print("bcftools 1.21")'),
        },
    )
    monkeypatch.setattr(dependency_checks, "check_dependencies", lambda tools: [])

    dependency_checks.verify_dependencies(["bcftools"])
    with pytest.raises(WGSExtractError, match=r"samtools version Version: 0\.1\.19"):
        dependency_checks.verify_dependencies(["samtools", "bcftools"])


def test_verify_rejects_a_samtools_that_crashes(tmp_path, monkeypatch):
    _use_tools(
        monkeypatch, {"samtools": _tool(tmp_path, "samtools", 'crash("SIGILL")')}
    )
    monkeypatch.setattr(dependency_checks, "check_dependencies", lambda tools: [])

    with pytest.raises(WGSExtractError):
        dependency_checks.verify_dependencies(["samtools"])


def test_check_all_dependencies_marks_crashing_tools_missing(tmp_path, monkeypatch):
    samtools = _tool(tmp_path, "samtools", 'print("samtools 1.21")')
    calls = _use_tools(
        monkeypatch,
        {
            "samtools": samtools,
            "bcftools": _tool(tmp_path, "bcftools", 'crash("SIGSEGV")'),
            "sniffles": _tool(tmp_path, "sniffles", 'print("Sniffles2 2.6.3")'),
        },
    )

    results = dependency_checks.check_all_dependencies(
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
    assert by_name["samtools"] == {
        "name": "samtools",
        "path": samtools,
        "runtime": "native",
        "version": "samtools 1.21",
        "alt_env": None,
    }
    assert by_name["bcftools"]["path"] is None
    assert by_name["bcftools"]["version"] == f"Error: bcftools {CRASHED['SIGSEGV']}"
    assert by_name["bcftools"]["runtime"] == "missing"
    assert by_name["minimap2"]["version"].startswith("Run: pixi install --locked")
    assert {tool for (tool,) in calls} == {
        "samtools",
        "bcftools",
        "sniffles",
        "minimap2",
    }


def test_log_dependency_info(tmp_path, monkeypatch, caplog):
    samtools = _tool(tmp_path, "samtools", 'print("samtools 1.21")')
    _use_tools(monkeypatch, {"samtools": samtools})
    caplog.set_level(logging.DEBUG)

    dependency_checks.log_dependency_info(["samtools", "bwa"])

    assert f"samtools -> {samtools} (samtools 1.21)" in caplog.text
    assert "bwa -> NOT FOUND" in caplog.text


# Version probes of tools in an optional Pixi environment.


def _fake_pixi(tmp_path: Path, prefix: Path, info_stdout: str | None = None) -> Path:
    """A pixi that logs its argv; `info --json` lists the pacbio env at prefix."""
    info = info_stdout or json.dumps(
        {"environments_info": [{"name": "pacbio", "prefix": str(prefix)}]}
    )
    (tmp_path / "info.json").write_text(info)
    return python_tool(
        tmp_path,
        "pixi",
        "import sys\n"
        f"open({str(tmp_path / 'argv.log')!r}, 'a').write(repr(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1] == 'info':\n"
        f"    print(open({str(tmp_path / 'info.json')!r}).read())\n"
        "else:\n"
        "    print('sniffles 2.6.3')\n",
    )


def _pixi_calls(tmp_path: Path) -> list[list[str]]:
    log = tmp_path / "argv.log"
    if not log.exists():
        return []
    return [ast.literal_eval(line) for line in log.read_text().splitlines()]


@pytest.mark.parametrize(
    "manifest",
    [["--manifest-path", "/app/pixi.toml"], ["--manifest-path=/app/pixi.toml"]],
)
def test_installed_pixi_env_is_probed_with_its_manifest(
    tmp_path, monkeypatch, manifest
):
    prefix = tmp_path / "envs" / "pacbio"
    (prefix / "conda-meta").mkdir(parents=True)
    (prefix / "conda-meta" / "pixi").write_text("{}")
    pixi = _fake_pixi(tmp_path, prefix)
    command = " ".join([str(pixi), "run", *manifest, "-e", "pacbio", "sniffles"])
    _use_tools(monkeypatch, {"sniffles": command})

    assert dependency_checks.get_tool_version("sniffles") == "sniffles 2.6.3"

    calls = _pixi_calls(tmp_path)
    assert calls[0] == ["info", "--json", *manifest]
    assert calls[1] == [
        "run",
        "--no-install",
        *manifest,
        "-e",
        "pacbio",
        "sniffles",
        "--version",
    ]


def test_unreadable_pixi_info_counts_the_env_as_not_installed(tmp_path, monkeypatch):
    pixi = _fake_pixi(tmp_path, tmp_path, info_stdout="WARN pixi is updating")
    _use_tools(monkeypatch, {"sniffles": f"{pixi} run -e pacbio sniffles"})

    version = dependency_checks.get_tool_version("sniffles")
    assert version.startswith(
        "Error: Pixi env 'pacbio' is not installed. Run: pixi install --locked"
    )
    assert version.endswith("-e pacbio")
    assert [c[0] for c in _pixi_calls(tmp_path)] == ["info"]


# pixi_tools: telling Pixi commands and paths apart, finding pixi, probing envs.


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


def test_resolve_pixi_command_falls_back_to_the_users_pixi(monkeypatch):
    if any(Path(p).exists() for p in ("/opt/homebrew/bin/pixi", "/usr/local/bin/pixi")):
        pytest.skip("a system pixi is installed")
    monkeypatch.setattr(pixi_tools.shutil, "which", lambda name: None)
    monkeypatch.setattr(pixi_tools.runtime, "source_checkout_root", lambda: None)

    assert pixi_tools._resolve_pixi_command() is None
    assert pixi_tools.pixi_env_prefix("gatk") is None

    own = Path.home() / ".pixi" / "bin" / "pixi"
    own.parent.mkdir(parents=True)
    own.write_text("")
    assert pixi_tools._resolve_pixi_command() == str(own)


def test_manifest_args_point_at_the_checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(pixi_tools.runtime, "source_checkout_root", lambda: tmp_path)
    assert pixi_tools._pixi_manifest_args() == []

    (tmp_path / "pixi.toml").write_text("")
    assert pixi_tools._pixi_manifest_args() == [
        "--manifest-path",
        str(tmp_path / "pixi.toml"),
    ]


@pytest.mark.parametrize(
    ("result", "error"),
    [
        (MagicMock(returncode=1, stdout='[{"name": "gatk4"}]'), None),
        (MagicMock(returncode=0, stdout="not json"), None),
        (None, OSError("pixi vanished")),
    ],
)
def test_lockfile_lookup_failures_mean_unavailable(monkeypatch, result, error):
    def run(cmd, **kwargs):
        if error:
            raise error
        return result

    monkeypatch.setattr(pixi_tools.subprocess, "run", run)
    assert not pixi_tools._locked_package_available("/usr/bin/pixi", "gatk", "gatk")


@pytest.mark.parametrize(
    "stdout",
    ["", "not json", '{"version": "0.40"}', '{"environments_info": [{"name": "vep"}]}'],
)
def test_pixi_env_prefix_without_a_usable_answer(monkeypatch, stdout):
    monkeypatch.setattr(pixi_tools, "_resolve_pixi_command", lambda: "/usr/bin/pixi")
    monkeypatch.setattr(
        pixi_tools.subprocess, "run", lambda cmd, **kw: MagicMock(stdout=stdout)
    )
    assert pixi_tools.pixi_env_prefix("gatk") is None


# Windows environments keep programs in Library\\bin and Scripts; see
# test_tool_resolution for those.
posix_env_layout = pytest.mark.skipif(
    sys.platform == "win32", reason="a POSIX environment's bin/ layout"
)


@pytest.fixture(scope="module")
def pixi_env(tmp_path_factory):
    """A real Python environment standing in for a Pixi env's prefix."""
    root = tmp_path_factory.mktemp("pixi-env")
    prefix = root / "env"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(prefix)], check=True
    )
    return prefix


def _probing_pixi(tmp_path: Path, prefix: Path, path_dir: Path) -> str:
    """A pixi whose `run ... -e ENV python -c PROBE NAMES` runs the probe in
    prefix's Python, with only path_dir on PATH."""
    script = tmp_path / "pixi"
    script.write_text(
        f"#!{sys.executable}\n"
        "import os, subprocess, sys\n"
        "args = sys.argv[1:]\n"
        "assert args[0] == 'run' and '--no-install' in args, args\n"
        "cmd = args[args.index('-e') + 2:]\n"
        f"cmd[0] = {str(prefix / 'bin' / 'python')!r}\n"
        f"env = dict(os.environ, PATH={str(path_dir)!r})\n"
        "sys.exit(subprocess.run(cmd, env=env).returncode)\n"
    )
    script.chmod(0o755)
    return str(script)


@posix_env_layout
def test_probe_finds_a_tool_on_the_envs_path(tmp_path, monkeypatch, pixi_env):
    _tool(tmp_path, "samtools", "")
    pixi = _probing_pixi(tmp_path, pixi_env, tmp_path / "bin")
    monkeypatch.setattr(pixi_tools, "_resolve_pixi_command", lambda: pixi)
    monkeypatch.setattr(pixi_tools, "_pixi_manifest_args", lambda: [])

    assert (
        pixi_tools.pixi_env_tool("samtools")
        == f"{pixi} run --no-install -e default samtools"
    )


@pytest.mark.parametrize(
    ("tool", "env_file", "expected_tail"),
    [
        ("haplogrep", "bin/haplogrep.jar", [*JAVA_JAR, "{file}"]),
        ("gatk", "bin/gatk", ["python", "{file}"]),
        ("delly", "bin/delly", ["{file}"]),
    ],
)
@posix_env_layout
def test_probe_finds_tools_inside_the_env_prefix(
    tmp_path, monkeypatch, pixi_env, tool, env_file, expected_tail
):
    file = pixi_env / env_file
    file.write_text("")
    empty_path = tmp_path / "empty"
    empty_path.mkdir()
    pixi = _probing_pixi(tmp_path, pixi_env, empty_path)
    monkeypatch.setattr(pixi_tools, "_resolve_pixi_command", lambda: pixi)
    monkeypatch.setattr(pixi_tools, "_pixi_manifest_args", lambda: [])
    try:
        command = pixi_tools.pixi_env_tool(tool)
    finally:
        file.unlink()

    env = pixi_tools.PIXI_TOOL_ENVS[tool]
    tail = [part.format(file=file.resolve()) for part in expected_tail]
    assert command == " ".join([pixi, "run", "--no-install", "-e", env, *tail])


@pytest.mark.parametrize("stdout", ["", "no separator", "script:"])
def test_probe_output_without_a_location_is_no_tool(monkeypatch, stdout):
    monkeypatch.setattr(pixi_tools, "_resolve_pixi_command", lambda: "/usr/bin/pixi")
    monkeypatch.setattr(
        pixi_tools.subprocess,
        "run",
        lambda cmd, **kw: MagicMock(returncode=0, stdout=stdout),
    )
    assert pixi_tools.pixi_env_tool("samtools") is None


def test_probe_that_cannot_start_pixi_is_no_tool(monkeypatch):
    monkeypatch.setattr(pixi_tools, "_resolve_pixi_command", lambda: "/usr/bin/pixi")

    def run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 10)

    monkeypatch.setattr(pixi_tools.subprocess, "run", run)
    assert pixi_tools.pixi_env_tool("samtools") is None
    assert pixi_tools.pixi_env_tool("not-a-pixi-tool") is None


# `deps check`


def test_deps_check_tool_prints_its_path(monkeypatch, capsys):
    from wgsextract_cli.core import dependencies

    monkeypatch.setattr(
        dependencies,
        "get_tool_path",
        lambda tool: {"samtools": "/env/bin/samtools"}.get(tool),
    )

    _deps_status.run(Namespace(tool="samtools", debug=False))
    assert capsys.readouterr().out == "/env/bin/samtools\n"

    _deps_status.run(Namespace(tool="samtools", debug=True))
    assert capsys.readouterr().out == ""

    with pytest.raises(WGSExtractError, match="Tool not found: bwa"):
        _deps_status.run(Namespace(tool="bwa", debug=False))


def test_deps_check_report(tmp_path, monkeypatch, capsys):
    _use_tools(
        monkeypatch,
        {
            "samtools": _tool(tmp_path, "samtools", 'print("samtools 1.21")'),
            "bcftools": _tool(tmp_path, "bcftools", 'print("bcftools 1.21")'),
            "sniffles": f"{_fake_pixi(tmp_path, tmp_path / 'none')} run -e pacbio sniffles",
        },
    )
    monkeypatch.setattr(
        dependency_checks, "required_dependency_tools", lambda: ["samtools", "bcftools"]
    )
    monkeypatch.setattr(
        dependency_checks, "optional_dependency_tools", lambda: ["sniffles", "minimap2"]
    )

    _deps_status.run(Namespace(tool=None, debug=False))

    lines = capsys.readouterr().out.splitlines()
    row = {line.split()[1]: line for line in lines if line and line[0] in "✅⚠🔵❌"}
    assert (
        row["samtools"].startswith("✅")
        and "[native] (samtools 1.21)" in row["samtools"]
    )
    assert row["sniffles"].startswith("⚠️ ")
    assert (
        "[missing] (Error: Pixi env 'pacbio' is not installed. Run: pixi install --locked"
        in row["sniffles"]
    )
    assert (
        row["minimap2"].startswith("⚠️ ")
        and "[missing] (Run: pixi install --locked" in row["minimap2"]
    )


def test_deps_check_report_in_ascii(monkeypatch, capsys):
    monkeypatch.setattr(
        _deps_status,
        "check_all_dependencies",
        lambda: {
            "mandatory": [
                {"name": "samtools", "path": "/s", "runtime": "native", "version": None}
            ],
            "optional": [
                {
                    "name": "minimap2",
                    "path": None,
                    "runtime": "missing",
                    "version": None,
                }
            ],
        },
    )
    ascii_out = open(os.devnull, "w", encoding="ascii")
    monkeypatch.setattr(sys, "stdout", ascii_out)
    try:
        assert _deps_status._status_text("/s") == "OK"
        assert _deps_status._status_text(None) == "MISS"
        assert _deps_status._status_text(None, optional=True) == "WARN"
        _deps_status.run(
            Namespace(tool=None, debug=False)
        )  # prints without UnicodeEncodeError
    finally:
        ascii_out.close()


def test_stdout_without_an_encoding_takes_emoji(monkeypatch):
    monkeypatch.setattr(sys, "stdout", object())
    assert _deps_status._status_text("/s") == "✅"
