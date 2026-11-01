import hashlib
import importlib.util
import json
from pathlib import Path
from zipfile import ZipFile

import pytest

spec = importlib.util.spec_from_file_location(
    "stage_candidate", Path(__file__).parents[1] / "stage_candidate.py"
)
assert spec is not None and spec.loader is not None
candidate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(candidate)


def artifact(path: Path, build: str | None = None, stale: str | None = None) -> None:
    with ZipFile(path, "w") as archive:
        for name, (version, qualified_build) in candidate.EXPECTED.items():
            selected = (
                build
                if build is not None and (stale is None or stale == name)
                else qualified_build
            )
            archive.writestr(
                f"win-arm64/{name}-{version}-{selected}.conda", name.encode()
            )
        archive.writestr("python-3.13.17-wgse_arm64_0.conda", b"obsolete")


def test_staging_retains_pins_and_excludes_obsolete_python(tmp_path: Path) -> None:
    source = tmp_path / "artifact.zip"
    artifact(source)
    output = tmp_path / "candidate"
    candidate.stage_candidate([source], output, ["qualified-run"])
    manifest = json.loads((output / "manifest.json").read_text())
    assert len(manifest["packages"]) == 14
    for package in manifest["packages"]:
        assert (
            hashlib.sha256((output / package["file"]).read_bytes()).hexdigest()
            == (package["sha256"])
        )
    assert not (output / "python-3.13.17-wgse_arm64_0.conda").exists()
    with pytest.raises(ValueError, match="must not exist"):
        candidate.stage_candidate([source], output, ["qualified-run"])


def test_duplicate_and_foreign_packages_leave_no_destination(tmp_path: Path) -> None:
    source = tmp_path / "artifact.zip"
    artifact(source)
    output = tmp_path / "candidate"
    with pytest.raises(ValueError, match="Duplicate"):
        candidate.stage_candidate([source, source], output, ["qualified-run"])
    assert not output.exists()
    artifact(source, "wgse_x64_0")
    with pytest.raises(ValueError, match="Unqualified"):
        candidate.stage_candidate([source], output, ["qualified-run"])
    assert not output.exists()


@pytest.mark.parametrize(
    ("name", "build"),
    [
        ("samtools", "wgse_arm64_0"),
        ("samtools", "wgse_arm64_1"),
        ("htslib", "wgse_arm64_0"),
        ("bcftools", "wgse_arm64_0"),
        ("samtools", "wgse_arm64_3"),
    ],
)
def test_stale_or_unqualified_builds_leave_no_destination(
    name: str, build: str, tmp_path: Path
) -> None:
    source = tmp_path / "artifact.zip"
    artifact(source, build, name)
    output = tmp_path / "candidate"
    with pytest.raises(ValueError, match="Unqualified"):
        candidate.stage_candidate([source], output, ["qualified-run"])
    assert not output.exists()
