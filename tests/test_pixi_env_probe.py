"""Finding a tool in a Pixi environment: in this process when the CLI already
runs in that environment (`pixi run` takes seconds on Windows), else inside it
through pixi. Pixi is the fake from tests/fake_tools.py."""

import shlex
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.fake_tools import FakePixi, python_tool
from wgsextract_cli.core import (
    dependency_checks,
    native_support,
    pixi_tools,
    runtime,
    windows_arm64,
)
from wgsextract_cli.core.tool_commands import _tool_command_parts
from wgsextract_cli.core.utils import WGSExtractError

# The probe sent to another environment is a multi-line `python -c` argument,
# which a batch-file pixi stand-in can't pass on.
posix_pixi = pytest.mark.skipif(
    sys.platform == "win32", reason="a batch file can't take a multi-line argument"
)


@pytest.fixture(autouse=True)
def _x64_host(monkeypatch: pytest.MonkeyPatch) -> None:
    # Windows ARM64 hosts skip probing other environments, and Linux ARM64
    # ones refuse GATK.
    monkeypatch.setattr(windows_arm64, "windows_native_machine", lambda: 0x8664)
    monkeypatch.setattr(native_support.platform, "machine", lambda: "x86_64")


def _run(command: str, *args: str) -> str:
    """Run a resolved command, split as the CLI splits it, returning its output."""
    argv = [*_tool_command_parts(command), *args]
    return subprocess.run(argv, capture_output=True, text=True, check=True).stdout


def _broken_pixi(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A PIXI_EXE that can't be started."""
    pixi = tmp_path / "broken" / "pixi"
    pixi.parent.mkdir()
    pixi.write_text("not a program")
    monkeypatch.setenv("PIXI_EXE", str(pixi))


def _in_environment(monkeypatch, name: str, prefix: Path, *, windows: bool) -> None:
    """Run the CLI inside the Pixi environment `name` at prefix."""
    monkeypatch.setenv("PIXI_ENVIRONMENT_NAME", name)
    monkeypatch.setattr(sys, "prefix", str(prefix))
    monkeypatch.setattr(runtime, "is_windows_host", lambda: windows)


# Through pixi, in another environment.


@posix_pixi
def test_a_tool_on_its_environments_path_runs_through_pixi(fake_pixi):
    prefix = fake_pixi.env("default")
    python_tool(prefix / "bin", "samtools", 'print("samtools 1.21")')

    command = pixi_tools.pixi_env_tool("samtools")

    assert command is not None
    assert _run(command, "--version") == "samtools 1.21\n"


@posix_pixi
def test_haplogrep_runs_its_jar_with_the_environments_java(fake_pixi):
    prefix = fake_pixi.env("default")
    python_tool(prefix / "bin", "java", "import sys; print(sys.argv[1:])")
    jar = prefix / "bin" / "haplogrep.jar"
    jar.write_bytes(b"PK")

    command = pixi_tools.pixi_env_tool("haplogrep")

    assert command is not None
    args = eval(_run(command, "classify"))
    # contract: haplogrep ships as a jar, run by its environment's Java.
    assert args[args.index("-jar") + 1 :] == [str(jar.resolve()), "classify"]


@posix_pixi
def test_a_script_off_the_path_runs_with_the_environments_python(fake_pixi):
    """GATK's launcher is a Python script; run it with the environment's
    Python when it isn't an executable on PATH."""
    prefix = fake_pixi.env("gatk")
    (prefix / "bin" / "gatk").write_text("import sys; print(sys.prefix)")

    command = pixi_tools.pixi_env_tool("gatk")

    assert command is not None
    assert _run(command) == f"{prefix}\n"


@posix_pixi
@pytest.mark.parametrize("windows", [False, True])
@pytest.mark.parametrize(
    ("tool", "env", "package"),
    [
        ("gatk", "gatk", "gatk4"),
        ("vep", "vep", "ensembl-vep"),
        ("freebayes", "freebayes", "freebayes"),
        ("pbmm2", "pacbio", None),  # not in this platform's lockfile
        ("cyrius", "cyrius", "cyrius"),  # licensed: installed only on request
        ("cyrius_python", "cyrius", "cyrius"),
        ("pharmcat_python", "pharmcat", "pandas"),
    ],
)
def test_an_uninstalled_environment_is_unavailable_and_stays_uninstalled(
    fake_pixi, monkeypatch, windows, tool, env, package
):
    """A probe never installs an environment. `pixi run --no-install` runs a
    missing environment's command with the host's programs: this host has the
    tool and Python, and they must not pass for the environment's."""
    monkeypatch.setattr(runtime, "is_windows_host", lambda: windows)
    fake_pixi.env(env, locked=(package,) if package else (), installed=False)
    if not tool.endswith("_python"):  # the host's Python is this one
        python_tool(fake_pixi.host_bin, tool, 'print("host copy")')

    assert pixi_tools.pixi_env_tool(tool) is None
    assert not fake_pixi.installed(env)


@posix_pixi
def test_an_installed_environment_without_the_tool_is_unavailable(fake_pixi):
    fake_pixi.env("gatk", locked=("gatk4",))
    assert pixi_tools.pixi_env_tool("gatk") is None


@posix_pixi
@pytest.mark.parametrize(
    ("tool", "env", "package"),
    [("cyrius_python", "cyrius", "cyrius"), ("pharmcat_python", "pharmcat", "pandas")],
)
@pytest.mark.parametrize("current", ["default", "dev"])
def test_interpreter_alias_runs_its_dedicated_environments_python(
    fake_pixi, monkeypatch, tool, env, package, current
):
    """cyrius_python needs the cyrius environment's packages, even from `dev`
    (which has every tool of `default`, and a Python of its own)."""
    monkeypatch.setenv("PIXI_ENVIRONMENT_NAME", current)
    prefix = fake_pixi.env(env, locked=(package,))

    command = pixi_tools.pixi_env_tool(tool)

    assert command is not None
    assert _run(command, "-c", "import sys; print(sys.prefix)") == f"{prefix}\n"


@pytest.mark.parametrize("tool", ["cyrius_python", "pharmcat_python"])
def test_interpreter_alias_in_its_own_environment_is_this_python(
    tmp_path, monkeypatch, tool
):
    _broken_pixi(tmp_path, monkeypatch)
    python_tool(tmp_path / "bin", "python", "")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    env = tool.removesuffix("_python")
    _in_environment(monkeypatch, env, tmp_path, windows=sys.platform == "win32")

    command = pixi_tools.pixi_env_tool(tool)

    assert command is not None
    assert _run(command, "-c", "import sys; print(sys.executable)") == (
        f"{sys.executable}\n"
    )


@pytest.mark.parametrize("stdout", ["", "no separator", "script:"])
def test_probe_output_without_a_location_is_no_tool(fake_pixi, stdout):
    fake_pixi.env("default")
    fake_pixi.set(run_stdout=stdout)
    assert pixi_tools.pixi_env_tool("samtools") is None


def test_a_pixi_that_cannot_start_finds_nothing(tmp_path, monkeypatch):
    """Nor offers to install with it, or raises."""
    monkeypatch.delenv("PIXI_ENVIRONMENT_NAME", raising=False)
    _broken_pixi(tmp_path, monkeypatch)
    assert pixi_tools.pixi_env_tool("samtools") is None
    assert pixi_tools.pixi_env_tool("not-a-pixi-tool") is None
    assert pixi_tools.pixi_env_prefix("gatk") is None
    assert pixi_tools.pixi_install_command("gatk") is None


# In the CLI's own environment: no pixi at all.


@pytest.mark.parametrize("windows", [True, False])
def test_in_its_own_environment_the_cli_runs_tools_without_pixi(
    tmp_path, monkeypatch, windows
):
    """FastQC is a noarch script in opt/<package>/; Windows gets no usable
    link to it in bin/, so it runs the script with Python."""
    script = tmp_path / "env" / "opt" / "fastqc-0.13.0" / "fastqc"
    script.parent.mkdir(parents=True)
    script.write_text('print("FastQC v0.13.0")')
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    _broken_pixi(tmp_path, monkeypatch)
    _in_environment(monkeypatch, "default", tmp_path / "env", windows=windows)

    command = pixi_tools.pixi_env_tool("fastqc")

    if windows:
        assert command is not None
        assert _run(command) == "FastQC v0.13.0\n"
    else:
        assert command is None  # Unix links the script into bin/; nothing here


# Finding pixi, and the repair command it offers.


@posix_pixi
def test_the_pixi_running_the_cli_is_preferred(fake_pixi, tmp_path):
    """Over a system pixi on PATH, which may be another version with other
    global config: here it doesn't know this workspace."""
    system = FakePixi(tmp_path / "system", Path("/elsewhere/pixi.toml"))
    (fake_pixi.host_bin / "pixi").symlink_to(system.exe)
    prefix = fake_pixi.env("default")
    python_tool(prefix / "bin", "samtools", 'print("samtools 1.21")')

    assert pixi_tools.pixi_env_tool("samtools") is not None


@posix_pixi
@pytest.mark.parametrize("installed", [True, False])
def test_embedded_pixi_is_used_only_in_an_install_layout(
    tmp_path, monkeypatch, installed
):
    """install.sh puts pixi in <install>/.pixi beside app/; next to a plain
    checkout (~/wgsextract-cli) that path is the user's own ~/.pixi."""
    app = tmp_path / "app"
    app.mkdir()
    (app / "pixi.toml").write_text("")
    if installed:
        (tmp_path / "install-manifest.json").write_text("{}")
    monkeypatch.setattr(runtime, "source_checkout_root", lambda: app)
    embedded = FakePixi(tmp_path / "e", app / "pixi.toml", tmp_path / ".pixi" / "bin")
    on_path = FakePixi(tmp_path / "p", app / "pixi.toml")
    for pixi in (embedded, on_path):
        pixi.env("gatk", locked=("gatk4",), installed=False)
    monkeypatch.setenv("PATH", str(on_path.exe.parent))

    command = pixi_tools.pixi_install_command("gatk")

    assert command is not None
    expected = embedded if installed else on_path
    assert shlex.split(command)[0] == str(expected.exe)


@posix_pixi
def test_without_another_pixi_the_users_own_is_used(tmp_path, monkeypatch):
    if any(Path(p).exists() for p in ("/opt/homebrew/bin/pixi", "/usr/local/bin/pixi")):
        pytest.skip("a system pixi is installed")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(runtime, "source_checkout_root", lambda: None)
    assert pixi_tools.pixi_env_prefix("gatk") is None

    own = FakePixi(tmp_path / "own", None, Path.home() / ".pixi" / "bin")
    own.env("gatk", locked=("gatk4",), installed=False)
    command = pixi_tools.pixi_install_command("gatk")
    assert command is not None and shlex.split(command)[0] == str(own.exe)


def test_the_repair_command_installs_from_any_directory(tmp_path, monkeypatch):
    """It names the app's manifest when the app runs from a checkout."""
    app = tmp_path / "app"
    app.mkdir()
    (app / "pixi.toml").write_text("")
    monkeypatch.setattr(runtime, "source_checkout_root", lambda: app)
    pixi = FakePixi(tmp_path / "pixi", app / "pixi.toml")
    pixi.env("gatk", locked=("gatk4",), installed=False)
    monkeypatch.setenv("PIXI_EXE", str(pixi.exe))

    command = pixi_tools.pixi_install_command("gatk")
    assert command is not None
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    subprocess.run(_tool_command_parts(command), cwd=elsewhere, check=True)

    assert pixi.installed("gatk")


def test_without_a_checkout_the_repair_command_installs_into_this_workspace(
    tmp_path, monkeypatch
):
    """An installed wheel has no manifest of its own, so pixi finds the
    workspace from the current directory, as it does for the CLI itself."""
    workspace = tmp_path / "workspace"
    (workspace / "sub").mkdir(parents=True)
    (workspace / "pixi.toml").write_text("")
    monkeypatch.setattr(runtime, "source_checkout_root", lambda: tmp_path / "app")
    pixi = FakePixi(tmp_path / "pixi", workspace / "pixi.toml")
    pixi.env("gatk", locked=("gatk4",), installed=False)
    monkeypatch.setenv("PIXI_EXE", str(pixi.exe))
    monkeypatch.chdir(workspace)

    command = pixi_tools.pixi_install_command("gatk")
    assert command is not None
    subprocess.run(_tool_command_parts(command), cwd=workspace / "sub", check=True)

    assert pixi.installed("gatk")


@pytest.mark.parametrize(
    ("returncode", "stdout"), [(1, '[{"name": "gatk4"}]'), (0, "not json")]
)
def test_without_a_readable_lockfile_no_install_command_is_offered(
    fake_pixi, returncode, stdout
):
    fake_pixi.env("gatk", locked=("gatk4",), installed=False)
    fake_pixi.set(list_stdout=stdout, list_returncode=returncode)
    assert pixi_tools.pixi_install_command("gatk") is None


@pytest.mark.parametrize(
    "stdout",
    ["", "not json", '{"version": "0.40"}', '{"environments_info": [{"name": "vep"}]}'],
)
def test_pixi_env_prefix_without_a_usable_answer(fake_pixi, stdout):
    fake_pixi.env("gatk")
    assert pixi_tools.pixi_env_prefix("gatk") == fake_pixi.root / "envs" / "gatk"

    fake_pixi.set(info_stdout=stdout)
    assert pixi_tools.pixi_env_prefix("gatk") is None


# GATK on Windows: java with the native GKL sidecar on its classpath.

GKL = ("gkl_utils", "gkl_pairhmm", "gkl_pairhmm_omp", "gkl_smithwaterman")


def _gatk_prefix(tmp_path: Path, natives: tuple[str, ...] = GKL) -> Path:
    prefix = tmp_path / "profile with spaces"
    package = prefix / "share" / "gatk4-4.6.2.0-1"
    package.mkdir(parents=True)
    (package / "gatk-package-4.6.2.0-local.jar").write_bytes(b"PK")
    sidecar = prefix / "Library" / "share" / "gkl"
    sidecar.mkdir(parents=True)
    with zipfile.ZipFile(sidecar / "gkl-native-wgse.jar", "w") as native:
        for name in natives:
            native.writestr(f"com/intel/gkl/native/{name}.dll", b"MZ")
    java = prefix / "Library" / "bin"
    java.mkdir(parents=True)
    (java / "java.exe").write_bytes(b"MZ")
    return prefix


def _windows_gatk(tmp_path, monkeypatch, prefix: Path) -> str | None:
    """pixi_env_tool("gatk") on Windows, in the gatk environment at prefix,
    with an unrelated gatk on PATH."""
    python_tool(tmp_path / "host", "gatk", "")
    monkeypatch.setenv("PATH", str(tmp_path / "host"))
    _broken_pixi(tmp_path, monkeypatch)
    _in_environment(monkeypatch, "gatk", prefix, windows=True)
    return pixi_tools.pixi_env_tool("gatk")


def test_windows_gatk_runs_java_with_the_native_sidecar(tmp_path, monkeypatch):
    prefix = _gatk_prefix(tmp_path)

    command = _windows_gatk(tmp_path, monkeypatch, prefix)

    assert command is not None
    # The path has spaces: the command line must split back into these args.
    args = _tool_command_parts(command)
    # contract: the upstream launcher's `-jar` ignores an added classpath, so
    # the environment's Java runs GATK's Main with the GKL sidecar first.
    assert args[0] == str(prefix / "Library/bin/java.exe")
    assert "-jar" not in args
    assert args[-1] == "org.broadinstitute.hellbender.Main"
    assert args[args.index("-cp") + 1].split(";") == [
        str(prefix / "Library/share/gkl/gkl-native-wgse.jar"),
        str(prefix / "share/gatk4-4.6.2.0-1/gatk-package-4.6.2.0-local.jar"),
    ]
    # contract: GATK's packaged JVM options, and Java's default heap sizing.
    assert "-Dsamjdk.use_async_io_write_samtools=true" in args
    assert not any(arg.startswith("-Xmx") for arg in args)


@pytest.mark.parametrize(
    "missing_jar",
    [
        "Library/share/gkl/gkl-native-wgse.jar",
        "share/gatk4-4.6.2.0-1/gatk-package-4.6.2.0-local.jar",
    ],
)
def test_windows_gatk_missing_a_jar_has_repair_advice(
    tmp_path, monkeypatch, fake_pixi, missing_jar
):
    prefix = _gatk_prefix(tmp_path)
    (prefix / missing_jar).unlink()
    assert _windows_gatk(tmp_path, monkeypatch, prefix) is None

    # Elsewhere the gatk on PATH would do.
    monkeypatch.setattr(runtime, "is_windows_host", lambda: False)
    assert pixi_tools.pixi_env_tool("gatk") is not None

    monkeypatch.setattr(runtime, "is_windows_host", lambda: True)
    monkeypatch.setenv("PIXI_EXE", str(fake_pixi.exe))
    monkeypatch.setenv("PATH", str(fake_pixi.host_bin))
    fake_pixi.env("gatk", locked=("gatk4",))
    monkeypatch.setattr(dependency_checks, "sys", SimpleNamespace(platform="win32"))
    with pytest.raises(WGSExtractError) as exc:
        dependency_checks.verify_dependencies(["gatk"])
    assert "install_windows.bat" in str(exc.value)
    assert "pixi install --locked -e gatk" in str(exc.value)


def test_windows_gatk_rejects_an_unqualified_jar_abi(tmp_path, monkeypatch):
    prefix = _gatk_prefix(tmp_path)
    jar = prefix / "share/gatk4-4.6.2.0-1/gatk-package-4.6.2.0-local.jar"
    jar.rename(jar.with_name("gatk-package-4.7.0-local.jar"))
    assert _windows_gatk(tmp_path, monkeypatch, prefix) is None


@pytest.mark.parametrize("missing", ["gkl_pairhmm_omp", "gkl_smithwaterman"])
def test_windows_gatk_rejects_incomplete_older_sidecar(tmp_path, monkeypatch, missing):
    prefix = _gatk_prefix(tmp_path, tuple(n for n in GKL if n != missing))
    assert _windows_gatk(tmp_path, monkeypatch, prefix) is None
