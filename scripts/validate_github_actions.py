#!/usr/bin/env python3
"""Require allow-listed remote actions, immutable SHA pins and version comments."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_GLOB = ".github/workflows/*.y*ml"
USES_RE = re.compile(
    r"^(?P<indent>\s*)(?:-\s+)?uses:\s+(?P<target>['\"]?[^'\"\s#]+['\"]?)"
    r"(?:\s+#\s*(?P<comment>.*))?\s*$"
)
PINNED_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
VERSION_RE = re.compile(r"^v\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


# Each version/SHA lives in its workflow, so Dependabot need not update a
# duplicate table. Adding a new action source still requires an explicit review.
ALLOWED_ACTIONS = {
    "actions/cache",
    "actions/checkout",
    "actions/configure-pages",
    "actions/deploy-pages",
    "actions/download-artifact",
    "actions/setup-node",
    "actions/setup-python",
    "actions/upload-artifact",
    "actions/upload-pages-artifact",
    "github/codeql-action/analyze",
    "github/codeql-action/init",
    "gitleaks/gitleaks-action",
    "msys2/setup-msys2",
    "prefix-dev/setup-pixi",
    "softprops/action-gh-release",
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
            if normalized_action not in ALLOWED_ACTIONS:
                errors.append(
                    f"{workflow}:{line_number}: add {normalized_action} to "
                    "ALLOWED_ACTIONS after reviewing its source"
                )
                continue

            comment = match.group("comment")
            version = comment.split()[0] if comment else None
            if PINNED_SHA_RE.fullmatch(ref) is None:
                errors.append(
                    f"{workflow}:{line_number}: {action} is not pinned to a "
                    "full commit SHA"
                )

            if version is None or VERSION_RE.fullmatch(version) is None:
                found = version if version is not None else "no version comment"
                errors.append(
                    f"{workflow}:{line_number}: {action} has {found}; use "
                    "a version comment such as # v1.2.3"
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
