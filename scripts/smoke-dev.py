#!/usr/bin/env python3
"""Verify the documented development command and clean up its process tree."""

import argparse
import sys

from dev_setup import ROOT, smoke

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "--node", help="Probe this Node executable directly for compatibility."
)
args = parser.parse_args()
smoke(
    [
        args.node,
        "app/scripts/dev.mjs",
        "--bundle",
        str(ROOT / "examples/GitHelper"),
        "--port",
        "0",
    ]
    if args.node
    else [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        ".\\make.ps1",
        "dev",
        "BUNDLE=examples/GitHelper",
        "PORT=0",
    ]
    if sys.platform == "win32"
    else ["make", "dev", "BUNDLE=examples/GitHelper", "PORT=0"]
)
