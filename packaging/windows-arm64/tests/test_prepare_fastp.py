import importlib.util
from pathlib import Path


def test_widen_counts_offsets_without_changing_literals() -> None:
    path = Path(__file__).resolve().parents[1] / "prepare_fastp.py"
    spec = importlib.util.spec_from_file_location("prepare_fastp", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = (
        "unsigned long bases; long long reads; long double mean;\n"
        'printf("long ftell()"); // long fseek()\n'
        "long offset = ftell(fp); fseek(fp, offset, SEEK_SET);\n"
    )
    assert module.widen_llp64(source) == (
        "unsigned long long bases; long long reads; long double mean;\n"
        'printf("long ftell()"); // long fseek()\n'
        "long long offset = ftello(fp); fseeko(fp, offset, SEEK_SET);\n"
    )
