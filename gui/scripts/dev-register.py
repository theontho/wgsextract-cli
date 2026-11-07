#!/usr/bin/env python3
"""Pick a Git/GitHub identity and write it to .dev_id (and optionally git config)."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass


@dataclass
class Identity:
    source: str
    name: str
    email: str
    login: str | None
    active: bool


def run(command: str, args: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            [command, *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        return 1, str(exc)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def git_config(key: str) -> str:
    status, output = run("git", ["config", key])
    return output.strip() if status == 0 else ""


def parse_github_accounts(output: str) -> list[tuple[str, bool]]:
    accounts: list[list] = []
    current_index: int | None = None
    for line in output.splitlines():
        marker = line.find(" account ")
        if marker >= 0:
            suffix = line[marker + len(" account ") :]
            login = ""
            for ch in suffix:
                if ch in (" ", "("):
                    break
                login += ch
            if login:
                accounts.append([login, False])
                current_index = len(accounts) - 1
            continue
        if "Active account:" in line and "true" in line and current_index is not None:
            accounts[current_index][1] = True
    return [(login, active) for login, active in accounts]


def github_identity(username: str, active: bool) -> Identity:
    status, output = run("gh", ["api", f"users/{username}"])
    user_info: dict = {}
    if status == 0:
        try:
            user_info = json.loads(output)
        except json.JSONDecodeError:
            user_info = {}
    login = user_info.get("login") or username
    name = user_info.get("name") or login
    public_email = user_info.get("email")
    user_id = user_info.get("id")
    if public_email:
        email = public_email
    elif user_id is not None:
        email = f"{user_id}+{login}@users.noreply.github.com"
    else:
        email = f"{login}@users.noreply.github.com"
    source = (
        f"GitHub active account ({username})"
        if active
        else f"GitHub account ({username})"
    )
    return Identity(source=source, name=name, email=email, login=login, active=active)


def github_identities() -> list[Identity]:
    if run("gh", ["--version"])[0] != 0:
        return []
    _, status_output = run("gh", ["auth", "status", "--hostname", "github.com"])
    accounts = parse_github_accounts(status_output)
    accounts.sort(key=lambda a: 0 if a[1] else 1)
    seen: set[str] = set()
    identities: list[Identity] = []
    for login, active in accounts:
        if login in seen:
            continue
        seen.add(login)
        identities.append(github_identity(login, active))
    return identities


def available_identities() -> list[Identity]:
    identities = github_identities()
    name = git_config("user.name")
    email = git_config("user.email")
    if name or email:
        identities.append(
            Identity(
                source="Local Git Config",
                name=name,
                email=email,
                login=None,
                active=False,
            )
        )
    return identities


def select_identity(identities: list[Identity], choice: int | None) -> Identity:
    default_choice = next(
        (i + 1 for i, ident in enumerate(identities) if ident.active),
        1,
    )
    if choice is not None:
        selected_choice = choice
    elif len(identities) > 1 and sys.stdin.isatty():
        prompt = (
            f"Choose an identity to register in .dev_id "
            f"(1-{len(identities)}) [{default_choice}]: "
        )
        try:
            raw = input(prompt).strip()
        except EOFError:
            raw = ""
        try:
            selected_choice = int(raw) if raw else default_choice
        except ValueError:
            selected_choice = default_choice
    else:
        selected_choice = default_choice

    if not 1 <= selected_choice <= len(identities):
        sys.stderr.write("Invalid choice.\n")
        sys.exit(1)
    return identities[selected_choice - 1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--choice", type=int, help="Pre-select identity index (1-based)."
    )
    parser.add_argument(
        "--local-only",
        action="store_true",
        help="Register the existing Git identity without querying GitHub accounts.",
    )
    parser.add_argument(
        "--no-update-git-config",
        dest="update_git_config",
        action="store_false",
        help="Skip updating repo git config user.name / user.email.",
    )
    args = parser.parse_args()

    if args.local_only:
        name, email = git_config("user.name"), git_config("user.email")
        identities = (
            [Identity("Local Git Config", name, email, None, False)]
            if name and email
            else []
        )
    else:
        identities = available_identities()
    if not identities:
        sys.stderr.write(
            "No git or GitHub identity found. Configure git or login to gh.\n"
        )
        return 1

    print("Available Identities:")
    for index, identity in enumerate(identities, start=1):
        print(f"{index}) {identity.source}: {identity.name} <{identity.email}>")

    selected = select_identity(identities, args.choice)
    with open(".dev_id", "w", encoding="utf-8") as handle:
        handle.write(f"name={selected.name}\nemail={selected.email}\n")
    print(f"Registered in .dev_id using {selected.source}")

    if args.update_git_config:
        run("git", ["config", "user.name", selected.name])
        run("git", ["config", "user.email", selected.email])
        print("Updated repository git identity to match .dev_id")
    return 0


if __name__ == "__main__":
    sys.exit(main())
