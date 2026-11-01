"""Exercise installed native numerical extensions and the default Yleaf entry."""

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

import numpy as np
import pandas as pd
import psutil
import six
import yleaf
import yleaf.Yleaf

from wgsextract_cli.core.windows_arm64 import (
    IMAGE_FILE_MACHINE_ARM64,
    assert_arm64_pe,
    assert_native_windows_python,
    windows_native_machine,
)


def qualify(output: Path, prefix: Path) -> None:
    if windows_native_machine() != IMAGE_FILE_MACHINE_ARM64:
        raise RuntimeError("This qualification requires native Windows ARM64")
    assert_native_windows_python()
    matrix = np.array([[3.0, 1.0], [1.0, 2.0]])
    solution = np.linalg.solve(matrix, np.array([9.0, 8.0]))
    np.testing.assert_allclose(solution, [2.0, 3.0], rtol=0, atol=1e-14)
    variants = pd.read_csv(
        io.StringIO("POS\tREF\tALT\n263\tA\tG\n73\tA\tG\n"), sep="\t"
    ).sort_values("POS")
    assert variants.to_csv(index=False, sep="\t", lineterminator="\n") == (
        "POS\tREF\tALT\n73\tA\tG\n263\tA\tG\n"
    )
    assert six.__version__ == "1.17.0"
    assert psutil.Process().memory_info().rss > 0
    markers = Path(yleaf.__file__).parent / "data" / "hg38"
    assert markers.is_dir() and any(markers.iterdir()), "Missing Yleaf marker tables"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from yleaf.Yleaf import main; main()",
            "--help",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert "usage:" in result.stdout.lower(), result.stdout
    output.mkdir(parents=True, exist_ok=True)
    (output / "yleaf-help.txt").write_text(result.stdout, encoding="utf-8")
    (output / "libraries.json").write_text(
        json.dumps(
            {
                "python": sys.version,
                "packages": {
                    package: version(package)
                    for package in (
                        "numpy",
                        "pandas",
                        "psutil",
                        "six",
                        "Yleaf",
                        "setuptools",
                    )
                },
                "linear_solve": solution.tolist(),
                "marker_directory": str(markers),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    shell = Path(shutil.which("pwsh") or "pwsh")
    assert_arm64_pe(shell)
    subprocess.run(
        [
            str(shell),
            "-NoProfile",
            "-File",
            str(
                Path(__file__).resolve().parents[3]
                / "scripts"
                / "audit_windows_arm64.ps1"
            ),
            "-Prefix",
            str(prefix),
            "-ProcessIds",
            str(os.getpid()),
            "-Report",
            str(output / "process.json"),
        ],
        check=True,
        timeout=60,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prefix", type=Path, required=True)
    args = parser.parse_args()
    qualify(args.output, args.prefix)


if __name__ == "__main__":
    main()
