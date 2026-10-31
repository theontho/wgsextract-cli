"""Apply the same LLP64 source widening as the existing Windows x64 recipe."""

import argparse
import re
from pathlib import Path

_TOKENS = re.compile(
    r'("(?:[^"\\]|\\.)*")|(//[^\n]*)|'
    r"(?<!long )\blong\b(?!\s+(?:long|double)\b)|\bftell\(|\bfseek\("
)


def widen_llp64(source: str) -> str:
    def replace(match: re.Match[str]) -> str:
        if match.group(1) is not None or match.group(2) is not None:
            return match.group(0)
        return {"long": "long long", "ftell(": "ftello(", "fseek(": "fseeko("}[
            match.group(0)
        ]

    return _TOKENS.sub(replace, source)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    for file in (args.source / "src").iterdir():
        if file.suffix in (".cpp", ".h"):
            file.write_text(
                widen_llp64(file.read_text(encoding="utf-8")),
                encoding="utf-8",
                newline="\n",
            )
    if "long long mBases;" not in (args.source / "src/stats.h").read_text(
        encoding="utf-8"
    ):
        raise RuntimeError("fastp LLP64 base counters were not widened")


if __name__ == "__main__":
    main()
