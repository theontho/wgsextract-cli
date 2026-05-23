#!/usr/bin/env python3
"""Run actionlint with a clear error when it is not available."""

from __future__ import annotations

import shutil
import subprocess
import sys


def main() -> int:
    actionlint = shutil.which("actionlint")
    if actionlint is None:
        print(
            "actionlint is not on PATH. Run this hook via `pixi run pre-commit ...` "
            "or enter `pixi shell` so the Pixi-provided actionlint is available.",
            file=sys.stderr,
        )
        return 127

    return subprocess.run([actionlint, *sys.argv[1:]], check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
