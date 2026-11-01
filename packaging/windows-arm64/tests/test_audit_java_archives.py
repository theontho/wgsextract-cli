import importlib.util
import io
from pathlib import Path
from zipfile import ZipFile


def test_native_payloads_cannot_hide_in_nested_jars() -> None:
    path = Path(__file__).resolve().parents[1] / "audit_java_archives.py"
    spec = importlib.util.spec_from_file_location("audit_java_archives", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    nested_data = io.BytesIO()
    with ZipFile(nested_data, "w") as archive:
        archive.writestr("native/kernel.dll", b"MZ" + b"\0" * 64)
        archive.writestr("payload.dat", b"\x7fELF" + b"\0" * 64)
        archive.writestr("Example.class", b"\xca\xfe\xba\xbe\0\0\0\x3d")
    outer_data = io.BytesIO()
    with ZipFile(outer_data, "w") as archive:
        archive.writestr("lib/inner.jar", nested_data.getvalue())
    with ZipFile(outer_data) as archive:
        assert module.native_members(archive) == [
            "lib/inner.jar!native/kernel.dll",
            "lib/inner.jar!payload.dat",
        ]
