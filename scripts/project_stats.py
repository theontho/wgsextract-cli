from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TextIO

HEADER = "========================================================"
TITLE = "  WGS Extract CLI: Project Statistics"
PROJECT_EXCLUDES = r"\.git|\.venv|out|tmp|\.pixi|\.mypy_cache|\.pytest_cache|\.ruff_cache|external/yleaf"


class Reporter:
    def __init__(self, report_path: Path | None) -> None:
        self.report_path = report_path
        self._report_file: TextIO | None = None

    def __enter__(self) -> Reporter:
        if self.report_path is not None:
            self.report_path.parent.mkdir(parents=True, exist_ok=True)
            self._report_file = self.report_path.open("w", encoding="utf-8")
        return self

    def __exit__(self, *args: object) -> None:
        if self._report_file is not None:
            self._report_file.close()

    def write(self, text: str = "") -> None:
        print(text)
        if self._report_file is not None:
            print(text, file=self._report_file)


def run_cloc(args: list[str], reporter: Reporter) -> int:
    process = subprocess.run(
        ["cloc", *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    output = process.stdout.rstrip("\n")
    if output:
        reporter.write(output)
    return process.returncode


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run cloc with the standard WGS Extract project exclusions."
    )
    parser.add_argument(
        "report_path",
        nargs="?",
        type=Path,
        help="Optional path to write a copy of the stats report.",
    )
    return parser.parse_args()


def missing_cloc_message() -> str:
    if platform.system() == "Windows":
        return (
            "Error: 'cloc' is not available. cloc is a developer tool and is not "
            "managed by Pixi on Windows. Install it with:\n"
            "  winget install --id AlDanial.Cloc --exact --scope user\n"
            "Then rerun 'pixi run stats' or 'pixi run stats-report'."
        )

    return (
        "Error: 'cloc' is not available. Install cloc with your system package "
        "manager, then rerun 'pixi run stats' or 'pixi run stats-report'."
    )


def main() -> int:
    args = parse_args()

    if shutil.which("cloc") is None:
        print(missing_cloc_message(), file=sys.stderr)
        return 1

    with Reporter(args.report_path) as reporter:
        reporter.write(HEADER)
        reporter.write(TITLE)
        reporter.write(HEADER)

        sections = [
            (
                "--- Full Project (Excluding generated data and external deps) ---",
                [
                    ".",
                    "--fullpath",
                    f"--not-match-d={PROJECT_EXCLUDES}",
                    "--vcs=git",
                ],
            ),
            (
                "--- Production Code (src/wgsextract_cli) ---",
                ["src/wgsextract_cli", "--vcs=git"],
            ),
            (
                "--- Test Code (tests/ and smoke_test_scripts/) ---",
                ["tests", "smoke_test_scripts", "--vcs=git"],
            ),
        ]

        exit_code = 0
        for title, cloc_args in sections:
            reporter.write()
            reporter.write(title)
            exit_code = max(exit_code, run_cloc(cloc_args, reporter))

        reporter.write()
        reporter.write(HEADER)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
