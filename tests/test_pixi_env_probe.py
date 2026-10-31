"""Finding a tool in a Pixi environment: in this process when the CLI already
runs in that environment (`pixi run` takes seconds on Windows), else inside it."""

import json
import shlex
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from wgsextract_cli.core import pixi_tools


def _prefix(tmp_path: Path) -> Path:
    (tmp_path / "opt" / "fastqc-0.13.0").mkdir(parents=True)
    (tmp_path / "opt" / "fastqc-0.13.0" / "fastqc").write_text(
        "#!/usr/bin/env python3\n"
    )
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "haplogrep.jar").write_bytes(b"PK")
    (tmp_path / "bin" / "tool.sh").write_text("#!/bin/sh\n")
    return tmp_path


def test_probe_finds_scripts_jars_and_bin(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    prefix = str(_prefix(tmp_path))
    probe = pixi_tools._probe_environment
    fastqc = tmp_path / "opt" / "fastqc-0.13.0" / "fastqc"
    assert probe(prefix, ["fastqc"], windows=True) == f"script:{fastqc}"
    assert probe(prefix, ["haplogrep"], windows=False).startswith("haplogrep:")
    assert probe(prefix, ["tool.sh"], windows=False).endswith("tool.sh")
    assert probe(prefix, ["no-such-tool-anywhere"], windows=False) is None


def test_the_probe_runs_inside_another_environment(tmp_path, monkeypatch):
    # The source sent to the other environment's Python must run on its own.
    run = subprocess.run

    def fake_pixi(cmd, **kwargs):
        script, names = cmd[cmd.index("-c") + 1], cmd[cmd.index("-c") + 2 :]
        return run([sys.executable, "-c", script, *names], **kwargs)

    monkeypatch.setattr(pixi_tools.subprocess, "run", fake_pixi)
    found = pixi_tools._probe_environment_with_pixi("pixi", "default", ["python"])
    assert found is not None and found.stdout.strip() == "direct-name:python"
    missing = pixi_tools._probe_environment_with_pixi(
        "pixi", "default", ["no-such-tool-anywhere"]
    )
    assert missing is not None and missing.returncode == 1


@pytest.mark.parametrize("windows", [True, False])
def test_in_its_own_environment_the_cli_skips_pixi(tmp_path, monkeypatch, windows):
    prefix = _prefix(tmp_path)
    monkeypatch.setenv("PIXI_ENVIRONMENT_NAME", "default")
    monkeypatch.setattr(pixi_tools.sys, "prefix", str(prefix))
    monkeypatch.setattr(pixi_tools.runtime, "is_windows_host", lambda: windows)
    monkeypatch.setattr(pixi_tools, "_resolve_pixi_command", lambda: "pixi")
    monkeypatch.setattr("shutil.which", lambda name: None)

    def no_subprocess(*args, **kwargs):
        raise AssertionError("ran a subprocess")

    monkeypatch.setattr(pixi_tools.subprocess, "run", no_subprocess)
    command = pixi_tools._host_pixi_tool_command("fastqc")
    if windows:
        assert command is not None and " run " not in command
        assert command.startswith(sys.executable) and "fastqc-0.13.0" in command
    else:
        assert command is None  # Unix links the script into bin/; nothing here


@pytest.mark.parametrize(
    "tool,env", [("cyrius_python", "cyrius"), ("pharmcat_python", "pharmcat")]
)
@pytest.mark.parametrize("current", ["default", "dev", "dedicated"])
@pytest.mark.parametrize("installed", [True, False])
def test_interpreter_alias_uses_its_dedicated_environment(
    monkeypatch, tool, env, current, installed
):
    current = env if current == "dedicated" else current
    monkeypatch.setenv("PIXI_ENVIRONMENT_NAME", current)
    monkeypatch.setattr(pixi_tools.runtime, "is_windows_host", lambda: False)
    monkeypatch.setattr(pixi_tools, "_resolve_pixi_command", lambda: "pixi")
    monkeypatch.setattr(pixi_tools, "_pixi_manifest_args", lambda: [])
    monkeypatch.setattr(
        "shutil.which", lambda name: sys.executable if name == "python" else None
    )
    probes = []

    def probe(pixi_cmd, target_env, names):
        probes.append((target_env, names))
        return subprocess.CompletedProcess(
            [], 0 if installed else 1, "direct-name:python\n" if installed else ""
        )

    monkeypatch.setattr(pixi_tools, "_probe_environment_with_pixi", probe)
    command = pixi_tools.pixi_env_tool(tool)
    if current == env:
        assert command is not None and shlex.split(command) == [sys.executable]
        assert not probes
    else:
        assert probes == [(env, ["python"])]
        if installed:
            assert command is not None and shlex.split(command) == [
                "pixi",
                "run",
                "--no-install",
                "-e",
                env,
                "python",
            ]
        else:
            assert command is None


def _gatk_prefix(tmp_path: Path) -> Path:
    prefix = tmp_path / "profile with spaces"
    package = prefix / "share" / "gatk4-4.6.2.0-1"
    package.mkdir(parents=True)
    (package / "gatk-package-4.6.2.0-local.jar").write_bytes(b"PK")
    sidecar = prefix / "Library" / "share" / "gkl"
    sidecar.mkdir(parents=True)
    with zipfile.ZipFile(sidecar / "gkl-native-wgse.jar", "w") as native:
        for name in (
            "gkl_utils",
            "gkl_pairhmm",
            "gkl_pairhmm_omp",
            "gkl_smithwaterman",
        ):
            native.writestr(f"com/intel/gkl/native/{name}.dll", b"MZ")
    java = prefix / "Library" / "bin"
    java.mkdir(parents=True)
    (java / "java.exe").write_bytes(b"MZ")
    return prefix


def test_windows_gatk_uses_sidecar_classpath_and_environment_java(
    tmp_path, monkeypatch
):
    prefix = _gatk_prefix(tmp_path)
    monkeypatch.setattr("shutil.which", lambda name: "/unrelated/gatk")
    found = pixi_tools._probe_environment(str(prefix), ["gatk"], windows=True)
    assert found is not None
    args = json.loads(found.removeprefix("gatk:"))
    assert args[0] == str(prefix / "Library/bin/java.exe")
    assert "-jar" not in args
    assert args[-1] == "org.broadinstitute.hellbender.Main"
    assert args[args.index("-cp") + 1].split(";") == [
        str(prefix / "Library/share/gkl/gkl-native-wgse.jar"),
        str(prefix / "share/gatk4-4.6.2.0-1/gatk-package-4.6.2.0-local.jar"),
    ]
    assert "-Dsamjdk.use_async_io_write_samtools=true" in args
    assert not any(arg.startswith("-Xmx") for arg in args)


@pytest.mark.parametrize(
    "missing_jar",
    [
        "Library/share/gkl/gkl-native-wgse.jar",
        "share/gatk4-4.6.2.0-1/gatk-package-4.6.2.0-local.jar",
    ],
)
def test_windows_gatk_missing_native_jar_has_repair_advice(
    tmp_path, monkeypatch, missing_jar
):
    from wgsextract_cli.core import dependency_checks
    from wgsextract_cli.core.utils import WGSExtractError

    prefix = _gatk_prefix(tmp_path)
    (prefix / missing_jar).unlink()
    monkeypatch.setattr("shutil.which", lambda name: "/unrelated/gatk")
    assert pixi_tools._probe_environment(str(prefix), ["gatk"], windows=True) is None
    assert (
        pixi_tools._probe_environment(str(prefix), ["gatk"], windows=False)
        == "direct-name:gatk"
    )
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(dependency_checks, "check_dependencies", lambda tools: ["gatk"])
    with pytest.raises(WGSExtractError) as exc:
        dependency_checks.verify_dependencies(["gatk"])
    assert "install_windows.bat" in str(exc.value)
    assert "pixi install --locked -e gatk" in str(exc.value)


def test_windows_gatk_rejects_an_unqualified_jar_abi(tmp_path, monkeypatch):
    prefix = _gatk_prefix(tmp_path)
    jar = prefix / "share/gatk4-4.6.2.0-1/gatk-package-4.6.2.0-local.jar"
    jar.rename(jar.with_name("gatk-package-4.7.0-local.jar"))
    monkeypatch.setattr("shutil.which", lambda name: "/unrelated/gatk")
    assert pixi_tools._probe_environment(str(prefix), ["gatk"], windows=True) is None


@pytest.mark.parametrize("missing", ["gkl_pairhmm_omp", "gkl_smithwaterman"])
def test_windows_gatk_rejects_incomplete_older_sidecar(tmp_path, missing):
    prefix = _gatk_prefix(tmp_path)
    with zipfile.ZipFile(
        prefix / "Library/share/gkl/gkl-native-wgse.jar", "w"
    ) as native:
        for name in (
            "gkl_utils",
            "gkl_pairhmm",
            "gkl_pairhmm_omp",
            "gkl_smithwaterman",
        ):
            if name != missing:
                native.writestr(f"com/intel/gkl/native/{name}.dll", b"MZ")
    assert pixi_tools._probe_environment(str(prefix), ["gatk"], windows=True) is None


def test_windows_gatk_command_quotes_classpath_without_a_script(tmp_path, monkeypatch):
    prefix = _gatk_prefix(tmp_path)
    monkeypatch.setenv("PIXI_ENVIRONMENT_NAME", "gatk")
    monkeypatch.setattr(pixi_tools.sys, "prefix", str(prefix))
    monkeypatch.setattr(pixi_tools.runtime, "is_windows_host", lambda: True)
    monkeypatch.setattr(pixi_tools, "_resolve_pixi_command", lambda: "pixi")
    monkeypatch.setattr("shutil.which", lambda name: None)
    found = pixi_tools.pixi_env_tool("gatk")
    assert found is not None
    probed = pixi_tools._probe_environment(str(prefix), ["gatk"], True)
    assert probed is not None
    args = json.loads(probed.removeprefix("gatk:"))
    assert found == subprocess.list2cmdline(args)


def test_windows_gatk_does_not_install_or_launch_a_first_use_script(monkeypatch):
    monkeypatch.delenv("PIXI_ENVIRONMENT_NAME", raising=False)
    monkeypatch.setattr(pixi_tools.runtime, "is_windows_host", lambda: True)
    monkeypatch.setattr(pixi_tools, "_resolve_pixi_command", lambda: "pixi")
    monkeypatch.setattr(
        pixi_tools,
        "_probe_environment_with_pixi",
        lambda *_: subprocess.CompletedProcess([], 1, ""),
    )

    def no_install_probe(*_):
        raise AssertionError("Tried optional-environment first-use installation")

    monkeypatch.setattr(pixi_tools, "_locked_package_available", no_install_probe)
    assert pixi_tools.pixi_env_tool("gatk") is None
