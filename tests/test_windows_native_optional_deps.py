import hashlib
import json
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]


def _pixi_feature_deps(feature: str, platform: str) -> dict[str, object]:
    """Conda deps a Pixi feature declares for a platform (feature-wide + targets)."""
    manifest = tomllib.loads((ROOT / "pixi.toml").read_text(encoding="utf-8"))
    table = manifest["feature"][feature]
    selectors = {platform, "unix" if not platform.startswith("win") else "win"}
    deps = dict(table.get("dependencies", {}))
    for selector, target in table.get("target", {}).items():
        if selector in selectors:
            deps.update(target.get("dependencies", {}))
    return deps


def test_pixi_declares_native_windows_optional_tools() -> None:
    assert "gatk4" in _pixi_feature_deps("gatk", "win-64")
    bio_tools = _pixi_feature_deps("bio-tools", "win-64")
    assert {"fastqc", "haplogrep"} <= set(bio_tools)


LEGACY_INSTALLER_FLAGS = (
    "--skip-msys2-install",
    "--skip-pacman-setup",
    "--skip-package-install",
    "--force-bwa-build",
    "--skip-fastp-download",
)
LEGACY_INSTALLER_OPTIONS = ("--msys2-root", "--bwa-binary-url", "--fastp-binary-sha256")


def test_windows_installer_only_needs_pixi() -> None:
    installer = (ROOT / "install_windows.bat").read_text(encoding="utf-8")
    prereqs = (ROOT / "scripts" / "bootstrap_windows_prereqs.ps1").read_text(
        encoding="utf-8"
    )
    # Tools come from the Pixi environment: no MSYS2, no runtime setting.
    for retired in (
        "setup_pacman_runtime",
        "pacman.exe",
        "tool_runtime",
        "save_config",
    ):
        assert retired not in installer
    assert "msys2" not in prereqs.lower()
    install_commands = [
        line.strip()
        for line in installer.splitlines()
        if line.strip().startswith("pixi install ")
    ]
    assert install_commands == [
        "pixi install --locked",
        "pixi install --locked -e gatk",
    ]
    assert "pixi run wgsextract deps check" in installer


def test_pixi_bootstrap_sha256_fails_closed(tmp_path) -> None:
    pwsh = shutil.which("pwsh") or shutil.which("powershell")
    if not pwsh:
        pytest.skip("PowerShell is not installed")
    payload = tmp_path / "pixi.exe"
    payload.write_bytes(b"fixture executable")
    bootstrap = (ROOT / "scripts/bootstrap_windows_prereqs.ps1").read_text()
    functions = bootstrap.rsplit("\nEnsure-Pixi", 1)[0]
    script = tmp_path / "verify.ps1"
    quoted = str(payload).replace("'", "''")
    digest = hashlib.sha256(payload.read_bytes()).hexdigest()
    for expected, succeeds in [(digest, True), ("0" * 64, False), ("", False)]:
        script.write_text(
            functions + f"\nAssert-FileSha256 -Path '{quoted}' "
            f"-ExpectedSha256 '{expected}' -Label 'Pixi executable'\n"
        )
        result = subprocess.run(
            [pwsh, "-NoProfile", "-File", str(script)], capture_output=True, text=True
        )
        assert (result.returncode == 0) is succeeds, result.stderr


def test_windows_installer_accepts_the_old_installers_options() -> None:
    # gui-for-cli and older docs still pass these: ignored, not an error.
    installer = (ROOT / "install_windows.bat").read_text(encoding="utf-8")
    switches = installer.split("rem accepted and ignored.", 1)[1].split(":args_done")[0]
    flags, valued = (
        set(part.split(")", 1)[0].split())
        for part in switches.split("for %%O in (")[1:]
    )
    assert set(LEGACY_INSTALLER_FLAGS) <= flags
    # Options with a value skip it too.
    assert set(LEGACY_INSTALLER_OPTIONS) <= valued


def test_windows_uninstaller_keeps_a_users_own_msys2() -> None:
    uninstaller = (ROOT / "uninstall_windows.bat").read_text(encoding="utf-8")
    prerequisites = uninstaller.split('"--remove-prerequisites" (', 1)[1].split(")")[0]
    assert "REMOVE_PIXI=1" in prerequisites
    assert "REMOVE_MSYS2" not in prerequisites
    # Clearing the config drops every old runtime setting.
    for key in ("tool_runtime", "pacman_ucrt64_bin", "runtime_directory"):
        assert key in uninstaller


def test_pixi_installs_runnable_yleaf_from_upstream_package_data_source() -> None:
    # Yleaf's source (Bioconda's build lacks the marker tables), as an archive
    # rather than a git checkout: a new Windows PC has no git, and the install
    # lab showed `pixi install` failing there on the git source.
    manifest = tomllib.loads((ROOT / "pixi.toml").read_text(encoding="utf-8"))
    yleaf = manifest["feature"]["bio-tools"]["pypi-dependencies"]["yleaf"]
    assert list(yleaf) == ["url"]
    assert re.fullmatch(
        r"https://github\.com/[\w-]+/Yleaf/archive/\S+\.tar\.gz#sha256=[0-9a-f]{64}",
        yleaf["url"],
    )
    lockfile = (ROOT / "pixi.lock").read_text(encoding="utf-8")
    _, marker, locked_entry = lockfile.partition(f"\n- pypi: direct+{yleaf['url']}\n")
    assert marker, "Yleaf's source archive must be present in the lockfile"
    # Pixi verifies this field, not a direct URL's hash fragment.
    digest = yleaf["url"].split("#sha256=", 1)[1]
    assert f"\n  sha256: {digest}\n" in locked_entry.split("\n- ", 1)[0]


def test_pixi_vep_runs_natively_on_apple_silicon() -> None:
    # osx-arm64's libdb is an x86_64 binary, so loading DB_File segfaults unless
    # PERL_DL_NONLAZY turns that into a load error BioPerl's AnyDBM_File skips.
    manifest = tomllib.loads((ROOT / "pixi.toml").read_text(encoding="utf-8"))
    vep = manifest["feature"]["vep"]
    assert "osx-arm64" in vep["platforms"]
    assert "ensembl-vep" in _pixi_feature_deps("vep", "osx-arm64")
    activation = vep["target"]["osx-arm64"]["activation"]
    assert activation["env"]["PERL_DL_NONLAZY"] == "1"


@pytest.mark.skipif(
    sys.platform != "darwin" or platform.machine() != "arm64", reason="Apple Silicon"
)
def test_native_vep_starts_on_apple_silicon() -> None:
    pixi = shutil.which("pixi")
    if not pixi:
        pytest.skip("pixi not found")
    manifest = ["--manifest-path", str(ROOT / "pixi.toml")]
    # Pixi's own prefix, which honours its detached-environments setting.
    info = subprocess.run(
        [pixi, "info", *manifest, "--json"], capture_output=True, text=True, timeout=60
    )
    envs = json.loads(info.stdout)["environments_info"]
    prefix = next(env["prefix"] for env in envs if env["name"] == "vep")
    if not (Path(prefix) / "bin" / "perl").exists():
        pytest.skip("vep Pixi environment not installed")
    result = subprocess.run(
        [pixi, "run", *manifest, "--frozen", "--no-install", "-e", "vep"]
        + ["vep", "--help"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "ENSEMBL VARIANT EFFECT PREDICTOR" in result.stdout
