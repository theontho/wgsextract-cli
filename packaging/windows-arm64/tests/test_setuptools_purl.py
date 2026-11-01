import importlib.util
import io
import json
import tarfile
from pathlib import Path
from zipfile import ZipFile

import pytest
import zstandard

spec = importlib.util.spec_from_file_location(
    "setuptools_purl", Path(__file__).parents[1] / "add_setuptools_purl.py"
)
assert spec is not None and spec.loader is not None
purl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(purl)


def package(path: Path, subdir: str = "win-arm64") -> None:
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as archive:
        index = json.dumps(
            {"name": "setuptools", "version": "84.2.1", "subdir": subdir}
        ).encode()
        member = tarfile.TarInfo("info/index.json")
        member.size = len(index)
        archive.addfile(member, io.BytesIO(index))
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "info-native.tar.zst",
            zstandard.ZstdCompressor().compress(payload.getvalue()),
        )
        archive.writestr("pkg-native.tar.zst", b"unchanged-qualified-runtime")


def test_standard_identity_preserves_runtime_archive(tmp_path: Path) -> None:
    original = tmp_path / "original.conda"
    modified = tmp_path / "modified.conda"
    package(original)
    purl.add_purl(original, modified)
    with ZipFile(original) as source, ZipFile(modified) as target:
        assert source.read("pkg-native.tar.zst") == target.read("pkg-native.tar.zst")
        information = zstandard.ZstdDecompressor().decompress(
            target.read("info-native.tar.zst")
        )
    with tarfile.open(fileobj=io.BytesIO(information)) as archive:
        stream = archive.extractfile("info/index.json")
        assert stream is not None
        assert json.load(stream)["purls"] == ["pkg:pypi/setuptools"]


def test_foreign_platform_rejected_before_output(tmp_path: Path) -> None:
    original = tmp_path / "foreign.conda"
    modified = tmp_path / "modified.conda"
    package(original, "win-64")
    with pytest.raises(ValueError, match="qualified native setuptools"):
        purl.add_purl(original, modified)
    assert not modified.exists()
