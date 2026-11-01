import importlib.util
import struct
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "prepare_setuptools", Path(__file__).resolve().parents[1] / "prepare_setuptools.py"
)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def source_fixture(path: Path) -> Path:
    source = path / "source"
    package = source / "setuptools"
    package.mkdir(parents=True)
    for name in (*module.FOREIGN, "cli-arm64.exe", "gui-arm64.exe"):
        machine = 0xAA64 if "arm64" in name else 0x8664
        header = bytearray(88)
        header[:2] = b"MZ"
        struct.pack_into("<I", header, 60, 64)
        header[64:68] = b"PE\0\0"
        struct.pack_into("<H", header, 68, machine)
        (package / name).write_bytes(header)
    (source / "_distutils_hack").mkdir()
    (source / "distutils-precedence.pth").write_text("preserve\n")
    info = source / "setuptools-84.2.1.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("License: MIT\n")
    (info / "RECORD").write_text(
        "".join(f"setuptools/{name},,\n" for name in module.FOREIGN)
        + "setuptools/cli-arm64.exe,,\nsetuptools/gui-arm64.exe,,\n"
    )
    return source


def test_retains_native_launchers_and_consistent_metadata(tmp_path: Path) -> None:
    source = source_fixture(tmp_path)
    prefix = tmp_path / "prefix"
    module.prepare(source, prefix)
    installed = prefix / "Lib" / "site-packages"
    assert sorted(path.name for path in (installed / "setuptools").iterdir()) == [
        "cli-arm64.exe",
        "gui-arm64.exe",
    ]
    assert (installed / "setuptools-84.2.1.dist-info" / "RECORD").read_text() == (
        "setuptools/cli-arm64.exe,,\nsetuptools/gui-arm64.exe,,\n"
    )
    assert (installed / "setuptools-84.2.1.dist-info" / "METADATA").read_text() == (
        "License: MIT\n"
    )
    assert all((source / "setuptools" / name).exists() for name in module.FOREIGN)


def test_rejects_foreign_arm64_launcher_before_copying(tmp_path: Path) -> None:
    source = source_fixture(tmp_path)
    launcher = source / "setuptools" / "cli-arm64.exe"
    content = bytearray(launcher.read_bytes())
    struct.pack_into("<H", content, 68, 0x8664)
    launcher.write_bytes(content)
    with pytest.raises(ValueError, match="not native"):
        module.prepare(source, tmp_path / "prefix")
    assert not (tmp_path / "prefix").exists()
