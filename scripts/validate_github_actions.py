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
    r"(?:\s+#\s*(?P<comment>.*))?\s*$"
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
        version="v6.1.0",
        sha="55cc8345863c7cc4c66a329aec7e433d2d1c52a9",
    ),
    "actions/checkout": ExpectedAction(
        version="v7.0.1",
        sha="3d3c42e5aac5ba805825da76410c181273ba90b1",
    ),
    "actions/configure-pages": ExpectedAction(
        version="v6.0.0",
        sha="45bfe0192ca1faeb007ade9deae92b16b8254a0d",
    ),
    "actions/deploy-pages": ExpectedAction(
        version="v5.0.1",
        sha="368f82528645a54fb793d4d04e342629a3f51346",
    ),
    "actions/download-artifact": ExpectedAction(
        version="v8.0.2",
        sha="9000827ccba6bdab643e8b6fd33ac0654aef8333",
    ),
    "actions/setup-node": ExpectedAction(
        version="v7.1.0",
        sha="949feb2413d6458794dcd2491c4babbbce0c15c1",
    ),
    "actions/setup-python": ExpectedAction(
        version="v7.0.0",
        sha="5fda3b95a4ea91299a34e894583c3862153e4b97",
    ),
    "actions/upload-artifact": ExpectedAction(
        version="v7.0.2",
        sha="cf430e030ddbb5b0abf93d22962f4752f3646cd9",
    ),
    "actions/upload-pages-artifact": ExpectedAction(
        version="v5.0.0",
        sha="fc324d3547104276b827a68afc52ff2a11cc49c9",
    ),
    "github/codeql-action/analyze": ExpectedAction(
        version="v4.39.0",
        sha="6f2ac81f5cd81fe351a197ba0b00d91d03cd2aee",
    ),
    "github/codeql-action/init": ExpectedAction(
        version="v4.39.0",
        sha="6f2ac81f5cd81fe351a197ba0b00d91d03cd2aee",
    ),
    # Third-party actions currently used by this repository.
    "gitleaks/gitleaks-action": ExpectedAction(
        version="v3.0.0",
        sha="e0c47f4f8be36e29cdc102c57e68cb5cbf0e8d1e",
    ),
    "msys2/setup-msys2": ExpectedAction(
        version="v2.33.0",
        sha="ec48f7c5447b3140e2b088413ae3a55687bccb6e",
    ),
    "prefix-dev/setup-pixi": ExpectedAction(
        version="v0.11.2",
        sha="ad1da6449d9e506fd127fd67ab2d054a769f6a94",
    ),
    "softprops/action-gh-release": ExpectedAction(
        version="v3.0.3",
        sha="efb35369e0ad2afab669f228072c1b0d510eae64",
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

            comment = match.group("comment")
            version = comment.split()[0] if comment else None
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
