import importlib.util
import io
import tarfile
from pathlib import Path
from zipfile import ZipFile

import zstandard


def _package(path: Path, member: str, data: bytes) -> None:
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as archive:
        info = tarfile.TarInfo(member)
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    with ZipFile(path, "w") as package:
        package.writestr(
            "pkg-test.tar.zst", zstandard.ZstdCompressor().compress(payload.getvalue())
        )


def test_excludes_both_python_versions_but_keeps_hyphenated_tool(
    tmp_path: Path,
) -> None:
    source, prefix = tmp_path / "source", tmp_path / "prefix"
    source.mkdir()
    for version in ("3.13.17", "3.13.18"):
        _package(
            source / f"python-{version}-wgse_arm64_0.conda",
            "python.exe",
            version.encode(),
        )
    _package(
        source / "bwa-mem2-2.3-wgse_arm64_0.conda",
        "Library/bin/bwa-mem2.exe",
        b"native-tool",
    )
    path = Path(__file__).resolve().parents[1] / "unpack_packages.py"
    spec = importlib.util.spec_from_file_location("unpack_packages", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.unpack(source, prefix, ("python",))
    assert not (prefix / "python.exe").exists()
    assert (prefix / "Library/bin/bwa-mem2.exe").read_bytes() == b"native-tool"
