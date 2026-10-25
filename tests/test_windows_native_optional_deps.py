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
