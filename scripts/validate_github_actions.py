#!/usr/bin/env python3
"""Validate GitHub Actions references against the repo's pinned version policy."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_GLOB = ".github/workflows/*.y*ml"
USES_RE = re.compile(
    r"^(?P<indent>\s*)(?:-\s+)?uses:\s+(?P<target>['\"]?[^'\"\s#]+['\"]?)"
    r"(?:\s+#\s*(?P<version>\S+))?\s*$"
)
PINNED_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class ExpectedAction:
    version: str
    sha: str


EXPECTED_ACTIONS = {
    # Popular first-party actions. Keep these current so generated workflows do
    # not regress to stale majors from older training data.
    "actions/cache": ExpectedAction(
        version="v5.0.5",
        sha="27d5ce7f107fe9357f9df03efb73ab90386fccae",
    ),
    "actions/checkout": ExpectedAction(
        version="v6.0.2",
        sha="de0fac2e4500dabe0009e67214ff5f5447ce83dd",
    ),
    "actions/configure-pages": ExpectedAction(
        version="v6.0.0",
        sha="45bfe0192ca1faeb007ade9deae92b16b8254a0d",
    ),
    "actions/deploy-pages": ExpectedAction(
        version="v5.0.0",
        sha="cd2ce8fcbc39b97be8ca5fce6e763baed58fa128",
    ),
    "actions/download-artifact": ExpectedAction(
        version="v8.0.1",
        sha="3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
    ),
    "actions/setup-node": ExpectedAction(
        version="v6.4.0",
        sha="48b55a011bda9f5d6aeb4c2d9c7362e8dae4041e",
    ),
    "actions/setup-python": ExpectedAction(
        version="v6.2.0",
        sha="a309ff8b426b58ec0e2a45f0f869d46889d02405",
    ),
    "actions/upload-artifact": ExpectedAction(
        version="v7.0.1",
        sha="043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
    ),
    "actions/upload-pages-artifact": ExpectedAction(
        version="v5.0.0",
        sha="fc324d3547104276b827a68afc52ff2a11cc49c9",
    ),
    "github/codeql-action/analyze": ExpectedAction(
        version="v4.36.0",
        sha="7211b7c8077ea37d8641b6271f6a365a22a5fbfa",
    ),
    "github/codeql-action/init": ExpectedAction(
        version="v4.36.0",
        sha="7211b7c8077ea37d8641b6271f6a365a22a5fbfa",
    ),
    # Third-party actions currently used by this repository.
    "gitleaks/gitleaks-action": ExpectedAction(
        version="v2.3.9",
        sha="ff98106e4c7b2bc287b24eaf42907196329070c7",
    ),
    "msys2/setup-msys2": ExpectedAction(
        version="v2.31.1",
        sha="e9898307ac31d1a803454791be09ab9973336e1c",
    ),
    "prefix-dev/setup-pixi": ExpectedAction(
        version="v0.9.6",
        sha="5185adfbffb4bd703da3010310260805d89ebb11",
    ),
    "softprops/action-gh-release": ExpectedAction(
        version="v3.0.0",
        sha="b4309332981a82ec1c5618f44dd2e27cc8bfbfda",
    ),
    "vampire/setup-wsl": ExpectedAction(
        version="v7.0.0",
        sha="d1da7f2c0322a5ee4f24975344f67fc0f5baf364",
    ),
}


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _is_local_or_docker_action(target: str) -> bool:
    return target.startswith(("./", "../", "docker://"))


def validate_workflows() -> list[str]:
    errors: list[str] = []
    workflows = sorted(ROOT.glob(WORKFLOW_GLOB))
    for workflow in workflows:
        for line_number, line in enumerate(
            workflow.read_text(encoding="utf-8").splitlines(), start=1
        ):
            match = USES_RE.match(line)
            if match is None:
                continue

            target = _strip_quotes(match.group("target"))
            if _is_local_or_docker_action(target):
                continue

            if "@" not in target:
                errors.append(
                    f"{workflow}:{line_number}: remote action is missing @ref"
                )
                continue

            action, ref = target.rsplit("@", 1)
            normalized_action = action.lower()
            expected = EXPECTED_ACTIONS.get(normalized_action)
            if expected is None:
                errors.append(
                    f"{workflow}:{line_number}: add {normalized_action} to "
                    "EXPECTED_ACTIONS after checking its latest stable tag"
                )
                continue

            version = match.group("version")
            if ref != expected.sha:
                if ref == expected.version:
                    errors.append(
                        f"{workflow}:{line_number}: {action} uses mutable tag "
                        f"{ref}; use {expected.sha} # {expected.version}"
                    )
                elif PINNED_SHA_RE.fullmatch(ref) is None:
                    errors.append(
                        f"{workflow}:{line_number}: {action} is not pinned to a "
                        f"full commit SHA; use {expected.sha} # {expected.version}"
                    )
                else:
                    errors.append(
                        f"{workflow}:{line_number}: {action} uses {ref}; expected "
                        f"{expected.sha} # {expected.version}"
                    )

            if version != expected.version:
                found = version if version is not None else "no version comment"
                errors.append(
                    f"{workflow}:{line_number}: {action} has {found}; expected "
                    f"version comment {expected.version}"
                )

    return errors


def main() -> int:
    errors = validate_workflows()
    if errors:
        print("GitHub Actions version policy failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    print("GitHub Actions version policy passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
