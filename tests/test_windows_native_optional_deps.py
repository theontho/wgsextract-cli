import json
import platform
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


def test_pacman_setup_declares_native_optional_packages() -> None:
    script = (ROOT / "scripts" / "setup_pacman_runtime.ps1").read_text(encoding="utf-8")

    assert "$optionalRuntimePackages = @(" in script
    assert "mingw-w64-ucrt-x86_64-curl" in script
    assert "mingw-w64-ucrt-x86_64-gcc-libs" in script
    assert "mingw-w64-ucrt-x86_64-isa-l" in script
    assert "mingw-w64-ucrt-x86_64-libdeflate" in script
    assert "htsfile is provided by mingw-w64-ucrt-x86_64-htslib" in script


def test_pacman_setup_reports_native_optional_tools() -> None:
    script = (ROOT / "scripts" / "setup_pacman_runtime.ps1").read_text(encoding="utf-8")

    assert "$optionalPacmanTools = @(" in script
    assert '"curl"' in script
    assert '"htsfile"' in script
    assert '"minimap2"' in script
    assert '"samblaster"' in script
    assert '"fastp"' in script
    assert "Optional pacman runtime tools are present:" in script


def test_pacman_setup_can_build_and_install_minimap2_asset() -> None:
    script = (ROOT / "scripts" / "setup_pacman_runtime.ps1").read_text(encoding="utf-8")

    assert '[string]$Minimap2Version = "2.30"' in script
    assert '[string]$Minimap2BinaryUrl = ""' in script
    assert '[string]$Minimap2BinarySha256 = ""' in script
    assert "WGSEXTRACT_MINIMAP2_BINARY_URL" not in script
    assert "WGSEXTRACT_MINIMAP2_BINARY_SHA256" not in script
    assert 'Assert-SafeReleaseValue -Name "Minimap2Version"' in script
    assert '-Sha256ParameterName "Minimap2BinarySha256"' in script
    assert "Install-Minimap2BinaryPackage" in script
    assert "wgsextract-minimap2-$Minimap2Version-windows-ucrt64.zip" in script
    assert "Building minimap2 $Minimap2Version for MSYS2 UCRT64" in script
    assert "/ucrt64/bin/minimap2.exe --version" in script
    assert 'throw "Failed to build required minimap2 native runtime:' in script


def test_release_workflow_uploads_minimap2_asset() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release_windows_bwa.yml").read_text(
        encoding="utf-8"
    )

    assert "MINIMAP2_VERSION" in workflow
    assert "Resolve-SafeBuildInput" in workflow
    assert "INPUT_MINIMAP2_VERSION: ${{ inputs.minimap2_version }}" in workflow
    assert '$minimap2Version = "${{ inputs.minimap2_version }}"' not in workflow
    assert "Build native tools with MSYS2 UCRT64 GCC" in workflow
    assert "wgsextract-minimap2-$env:MINIMAP2_VERSION-windows-ucrt64" in workflow
    assert "minimap2.exe" in workflow
    assert "${{ env.MINIMAP2_ZIP }}" in workflow


def test_pacman_setup_can_build_and_install_samblaster_and_fastp_assets() -> None:
    script = (ROOT / "scripts" / "setup_pacman_runtime.ps1").read_text(encoding="utf-8")

    assert '[string]$SamblasterVersion = "0.1.26"' in script
    assert '[string]$FastpVersion = "0.24.1"' in script
    assert "Install-SamblasterBinaryPackage" in script
    assert "Install-FastpBinaryPackage" in script
    assert "wgsextract-samblaster-$SamblasterVersion-windows-ucrt64.zip" in script
    assert "wgsextract-fastp-$FastpVersion-windows-ucrt64.zip" in script
    assert "Building samblaster $SamblasterVersion for MSYS2 UCRT64" in script
    assert "Building fastp $FastpVersion for MSYS2 UCRT64" in script
    assert "SamblasterVersion -notmatch '\\.(\\d+)$'" in script
    assert "BUILDNUM=$samblasterBuildNumber" in script
    assert "BUILDNUM=26" not in script
    assert "struct _stat64 status;" in script
    assert "_stat64( s.c_str(), \\&status );" in script
    assert "_stat64( path.c_str(), \\&status );" in script
    assert "fastp Windows large-file patch did not apply cleanly." in script
    assert "s/long double/WGSE_LONG_DOUBLE/g" in script
    assert "s/\\<long\\>/long long/g" in script
    assert "src/evaluator.h src/evaluator.cpp src/fastqreader.h" in script
    assert "src/seprocessor.h src/seprocessor.cpp src/stats.h src/stats.cpp" in script
    assert "as long long as" in script
    assert "long long mBases" in script
    assert "fastp Windows 64-bit stats patch did not apply cleanly." in script
    assert "#include <algorithm>" in script
    assert "/^    command = ss.str();$/a\\    replace(" in script
    assert "char(92), char(47)" in script
    assert "grep -c 'char(92), char(47)'" in script
    assert "fastp Windows report path patch did not apply cleanly." in script
    assert "/ucrt64/bin/samblaster.exe --version" in script
    assert "/ucrt64/bin/fastp.exe --version" in script
    assert 'throw "Failed to build required samblaster native runtime:' in script
    assert 'throw "Failed to build required fastp native runtime:' in script


def test_release_workflow_uploads_samblaster_and_fastp_assets() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release_windows_bwa.yml").read_text(
        encoding="utf-8"
    )

    assert "SAMBLASTER_VERSION" in workflow
    assert "FASTP_VERSION" in workflow
    assert "INPUT_SAMBLASTER_VERSION: ${{ inputs.samblaster_version }}" in workflow
    assert "INPUT_FASTP_VERSION: ${{ inputs.fastp_version }}" in workflow
    assert "wgsextract-samblaster-$env:SAMBLASTER_VERSION-windows-ucrt64" in workflow
    assert "wgsextract-fastp-$env:FASTP_VERSION-windows-ucrt64" in workflow
    assert "samblaster.exe" in workflow
    assert "fastp.exe" in workflow
    assert "${{ env.SAMBLASTER_ZIP }}" in workflow
    assert "${{ env.FASTP_ZIP }}" in workflow


def test_windows_installer_exposes_minimap2_checksum_passthrough() -> None:
    installer = (ROOT / "install_windows.bat").read_text(encoding="utf-8")

    assert "--minimap2-binary-url" in installer
    assert "--minimap2-binary-sha256" in installer
    assert "-Minimap2BinarySha256" in installer


def test_windows_installer_exposes_samblaster_fastp_checksum_passthrough() -> None:
    installer = (ROOT / "install_windows.bat").read_text(encoding="utf-8")

    assert "--samblaster-binary-url" in installer
    assert "--samblaster-binary-sha256" in installer
    assert "-SamblasterBinaryUrl" in installer
    assert "-SamblasterBinarySha256" in installer
    assert "--fastp-binary-url" in installer
    assert "--fastp-binary-sha256" in installer
    assert "-FastpBinaryUrl" in installer
    assert "-FastpBinarySha256" in installer


def test_windows_installer_persists_hybrid_runtime() -> None:
    installer = (ROOT / "install_windows.bat").read_text(encoding="utf-8")

    assert "'tool_runtime':'windows'" in installer
    assert "Runtime defaults were set to windows" in installer


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


def test_pixi_installs_runnable_yleaf_from_upstream_package_data_source() -> None:
    # Upstream's tagged source (Bioconda's build lacks the marker tables), as an
    # archive rather than a git checkout: a new Windows PC has no git, and the
    # install lab showed `pixi install` failing there on the git source.
    manifest = tomllib.loads((ROOT / "pixi.toml").read_text(encoding="utf-8"))
    yleaf = manifest["feature"]["bio-tools"]["pypi-dependencies"]["yleaf"]
    assert yleaf == {
        "url": "https://github.com/genid/Yleaf/archive/refs/tags/v4.1.6.tar.gz"
        "#sha256=ccdd984797320f87e63ab08e9953f4948dd01ddd067044f02279f5df1af83422"
    }
    assert "git" not in yleaf
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
