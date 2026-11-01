"""Stage native qualification artifacts for an explicit fresh Pixi installation."""

import argparse
import hashlib
import json
from pathlib import Path
from zipfile import ZipFile

EXPECTED: dict[str, list[str]] = json.loads(
    Path(__file__).with_name("candidate-pins.json").read_text(encoding="utf-8")
)


def stage_candidate(
    artifacts: list[Path], output: Path, qualification_runs: list[str]
) -> None:
    if output.exists():
        raise ValueError("Candidate staging destination must not exist")
    packages: dict[str, dict[str, str]] = {}
    payloads: dict[str, bytes] = {}
    for artifact in artifacts:
        with ZipFile(artifact) as archive:
            for member in archive.namelist():
                if not member.endswith(".conda"):
                    continue
                filename = Path(member).name
                name, version, build = filename.removesuffix(".conda").rsplit("-", 2)
                if name == "python" and version == "3.13.17":
                    continue
                if EXPECTED.get(name) != [version, build]:
                    raise ValueError(f"Unqualified candidate package: {filename}")
                if name in packages:
                    raise ValueError(f"Duplicate candidate package: {name}")
                payload = archive.read(member)
                packages[name] = {
                    "name": name,
                    "version": version,
                    "build": build,
                    "file": filename,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
                payloads[filename] = payload
    if packages.keys() != EXPECTED.keys():
        raise ValueError(f"Incomplete candidate: {EXPECTED.keys() - packages.keys()}")
    output.mkdir(parents=True)
    for filename, payload in payloads.items():
        (output / filename).write_bytes(payload)
    (output / "manifest.json").write_text(
        json.dumps(
            {
                "qualification_runs": qualification_runs,
                "packages": [packages[name] for name in sorted(packages)],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--qualification-run", action="append", required=True)
    args = parser.parse_args()
    stage_candidate(args.artifact, args.output, args.qualification_run)


if __name__ == "__main__":
    main()
