"""Stand-in executables written in Python that run on every platform."""

import sys
from pathlib import Path


def python_tool(bindir: Path, name: str, source: str) -> Path:
    """An executable `name` in bindir that runs the Python ``source``.

    POSIX runs the script by its shebang; Windows can't, so it gets
    ``name.py`` and a ``name.cmd`` launcher, which PATH lookups find too.
    """
    bindir.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        path = bindir / name
        path.write_text(f"#!{sys.executable}\n{source}")
        path.chmod(0o755)
        return path
    (bindir / f"{name}.py").write_text(source)
    launcher = bindir / f"{name}.cmd"
    launcher.write_text(f'@"{sys.executable}" "%~dp0{name}.py" %*\r\n', newline="")
    return launcher
