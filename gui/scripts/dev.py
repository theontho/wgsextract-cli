#!/usr/bin/env python3
"""Developer workstation setup helpers."""

from __future__ import annotations

import argparse
import plistlib
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.devconfig import DEVCONFIG_PATH, reload_devconfig  # noqa: E402


@dataclass(frozen=True)
class Team:
    apple_id: str
    team_id: str
    team_name: str
    is_free: bool


@dataclass(frozen=True)
class SigningIdentity:
    hash: str
    name: str
    team_id: str | None
    kind: str
    status: str | None = None


TEAM_ID_PATTERN = re.compile(r"\(([A-Z0-9]{10})\)")
DIST_LOG_IDENTITY_PATTERN = re.compile(r"Developer ID Application: [^\n\r]+\([A-Z0-9]{10}\)")
SIGNING_IDENTITY_PATTERN = re.compile(r'\)\s+([0-9A-F]+)\s+"([^"]+)"(?:\s+\(([^)]+)\))?')
DELETE_EXPIRED_COMMAND = "uv run python scripts/dev.py signing delete-expired-identities"



def run(*args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(args, capture_output=True, text=True, check=False)
    except FileNotFoundError as error:
        return subprocess.CompletedProcess(args, 127, "", str(error))



def load_xcode_teams() -> list[Team]:
    plist_path = Path.home() / "Library/Preferences/com.apple.dt.Xcode.plist"
    if not plist_path.exists():
        return []
    with plist_path.open("rb") as handle:
        data = plistlib.load(handle)
    teams_by_account = data.get("IDEProvisioningTeams", {})
    teams: list[Team] = []
    for apple_id, entries in teams_by_account.items():
        for entry in entries:
            teams.append(
                Team(
                    apple_id=apple_id,
                    team_id=entry.get("teamID", ""),
                    team_name=entry.get("teamName", ""),
                    is_free=bool(entry.get("isFreeProvisioningTeam", False)),
                )
            )
    return teams



def load_last_selected_team_id() -> str | None:
    plist_path = Path.home() / "Library/Preferences/com.apple.dt.Xcode.plist"
    if not plist_path.exists():
        return None
    with plist_path.open("rb") as handle:
        data = plistlib.load(handle)
    value = data.get("IDEProvisioningTeamManagerLastSelectedTeamID")
    return value if isinstance(value, str) and value else None



def identity_kind(identity_name: str) -> str:
    if identity_name.startswith("Developer ID Application:"):
        return "developer_id_application"
    if identity_name.startswith("Developer ID Installer:"):
        return "developer_id_installer"
    if identity_name.startswith("Apple Distribution:"):
        return "apple_distribution"
    if identity_name.startswith("Apple Development:"):
        return "apple_development"
    return "other"



def parse_signing_identities(output: str) -> list[SigningIdentity]:
    identities: list[SigningIdentity] = []
    seen: set[tuple[str, str | None]] = set()
    for line in output.splitlines():
        match = SIGNING_IDENTITY_PATTERN.search(line)
        if not match:
            continue
        identity_hash, identity_name, status = match.groups()
        key = (identity_hash, status)
        if key in seen:
            continue
        seen.add(key)
        team_match = TEAM_ID_PATTERN.search(identity_name)
        team_id = team_match.group(1) if team_match else None
        identities.append(SigningIdentity(identity_hash, identity_name, team_id, identity_kind(identity_name), status))
    return identities



def load_signing_identities(*, valid_only: bool = True) -> list[SigningIdentity]:
    args = ["security", "find-identity", "-p", "codesigning"]
    if valid_only:
        args.insert(2, "-v")
    result = run(*args)
    if result.returncode != 0:
        return []
    return parse_signing_identities(result.stdout)



def expired_signing_identities(identities: list[SigningIdentity]) -> list[SigningIdentity]:
    return [identity for identity in identities if identity.status and "EXPIRED" in identity.status.upper()]



def invalid_signing_identities(identities: list[SigningIdentity]) -> list[SigningIdentity]:
    return [identity for identity in identities if identity.status]



def describe_signing_identity(identity: SigningIdentity) -> str:
    status = f" ({identity.status})" if identity.status else ""
    return f'{identity.hash} "{identity.name}"{status}'



def identity_noun(count: int) -> str:
    return "identity" if count == 1 else "identities"



def choose_team(teams: list[Team], preferred_team_id: str | None) -> Team | None:
    if not teams:
        return None
    if preferred_team_id:
        preferred = next((team for team in teams if team.team_id == preferred_team_id), None)
        if preferred is not None:
            return preferred
    non_free = [team for team in teams if not team.is_free]
    if non_free:
        return sorted(non_free, key=lambda team: (team.apple_id.lower(), team.team_name.lower()))[0]
    return sorted(teams, key=lambda team: (team.apple_id.lower(), team.team_name.lower()))[0]



def load_recent_distribution_identity(team_id: str | None) -> SigningIdentity | None:
    temp_root = Path(tempfile.gettempdir())
    logs = sorted(temp_root.glob("GUIForCLIMac_*.xcdistributionlogs/IDEDistribution.verbose.log"), reverse=True)
    for log_path in logs[:10]:
        try:
            text = log_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        matches = DIST_LOG_IDENTITY_PATTERN.findall(text)
        for name in reversed(matches):
            team_match = TEAM_ID_PATTERN.search(name)
            match_team_id = team_match.group(1) if team_match else None
            if team_id and match_team_id != team_id:
                continue
            return SigningIdentity(hash="", name=name, team_id=match_team_id, kind="developer_id_application")
    return None



def choose_signing_identity(identities: list[SigningIdentity], team_id: str | None) -> SigningIdentity | None:
    preferred_kinds = ["developer_id_application", "apple_distribution"]
    for kind in preferred_kinds:
        matching = [identity for identity in identities if identity.kind == kind]
        if team_id:
            team_matching = [identity for identity in matching if identity.team_id == team_id]
            if team_matching:
                return team_matching[0]
        if matching:
            return matching[0]
    recent_distribution_identity = load_recent_distribution_identity(team_id)
    if recent_distribution_identity is not None:
        return recent_distribution_identity
    matching = [identity for identity in identities if identity.kind == "apple_development"]
    if team_id:
        team_matching = [identity for identity in matching if identity.team_id == team_id]
        if team_matching:
            return team_matching[0]
    if matching:
        return matching[0]
    return None



def quote(value: str) -> str:
    escaped = value.replace('\\', '\\\\').replace('"', '\\"')
    return f'"{escaped}"'



def render_devconfig(team: Team | None, identity: SigningIdentity | None, existing: dict) -> str:
    packaging = existing.get("packaging", {}) if isinstance(existing.get("packaging"), dict) else {}
    signing = existing.get("apple", {}) if isinstance(existing.get("apple"), dict) else {}
    signing_section = signing.get("signing", {}) if isinstance(signing.get("signing"), dict) else {}

    lines = [
        "# Local developer configuration. This file is gitignored.",
        "",
        "[apple.signing]",
        f"apple_id = {quote((team.apple_id if team else signing_section.get('apple_id', '')) or '')}",
        f"development_team = {quote((team.team_id if team else signing_section.get('development_team', '')) or '')}",
        f"team_id = {quote((team.team_id if team else signing_section.get('team_id', '')) or '')}",
        f"signing_identity = {quote((identity.name if identity else signing_section.get('signing_identity', '')) or '')}",
        f"notary_profile = {quote(signing_section.get('notary_profile', '') or '')}",
        f"app_specific_password = {quote(signing_section.get('app_specific_password', '') or '')}",
        "",
        "[packaging]",
        f"embedded_bundle_path = {quote(packaging.get('embedded_bundle_path', '') or '')}",
        f"app_name = {quote(packaging.get('app_name', '') or '')}",
        "",
    ]
    return "\n".join(lines)



def signing_autosetup(_args: argparse.Namespace) -> int:
    existing = reload_devconfig()
    preferred_team_id = load_last_selected_team_id()
    teams = load_xcode_teams()
    identities = load_signing_identities()
    all_identities = load_signing_identities(valid_only=False)
    expired_identities = expired_signing_identities(all_identities)
    invalid_identities = [
        identity for identity in invalid_signing_identities(all_identities) if identity not in expired_identities
    ]
    chosen_team = choose_team(teams, preferred_team_id)
    chosen_identity = choose_signing_identity(identities, chosen_team.team_id if chosen_team else None)

    DEVCONFIG_PATH.write_text(render_devconfig(chosen_team, chosen_identity, existing), encoding="utf-8")

    print(f"Wrote {DEVCONFIG_PATH}")
    if chosen_team:
        print(f"Detected Apple account: {chosen_team.apple_id}")
        print(f"Detected team: {chosen_team.team_name} ({chosen_team.team_id})")
    else:
        print("No Xcode provisioning team information was found.")

    if expired_identities:
        count = len(expired_identities)
        print(f"Warning: detected {count} expired code-signing {identity_noun(count)}:")
        for identity in expired_identities:
            print(f"  - {describe_signing_identity(identity)}")
        print(f"Delete expired identities with: {DELETE_EXPIRED_COMMAND}")

    if invalid_identities:
        count = len(invalid_identities)
        print(f"Warning: detected {count} invalid code-signing {identity_noun(count)}:")
        for identity in invalid_identities:
            print(f"  - {describe_signing_identity(identity)}")

    if chosen_identity:
        print(f"Detected signing identity: {chosen_identity.name}")
    else:
        print("No usable macOS signing identity was found in the login keychain.")
        print("Open Xcode → Settings → Accounts, manage certificates, and create/download a Developer ID Application certificate.")
        return 2

    if chosen_identity.kind != "developer_id_application":
        print("Warning: detected identity is not a Developer ID Application certificate, so public distribution notarization may still fail.")
    return 0



def signing_delete_expired_identities(args: argparse.Namespace) -> int:
    expired_identities = expired_signing_identities(load_signing_identities(valid_only=False))
    if not expired_identities:
        print("No expired code-signing identities were found.")
        return 0

    if args.dry_run:
        print("Expired code-signing identities that would be deleted:")
        for identity in expired_identities:
            print(f"  - {describe_signing_identity(identity)}")
        return 0

    keychain_args = [args.keychain] if args.keychain else []
    failures = 0
    for identity in expired_identities:
        print(f"Deleting expired code-signing identity: {describe_signing_identity(identity)}")
        result = run("security", "delete-identity", "-Z", identity.hash, *keychain_args)
        if result.returncode != 0:
            failures += 1
            error = (result.stderr or result.stdout).strip()
            print(f"Failed to delete {identity.hash}: {error}", file=sys.stderr)

    if failures:
        print(f"Failed to delete {failures} expired code-signing {identity_noun(failures)}.", file=sys.stderr)
        return 1

    count = len(expired_identities)
    print(f"Deleted {count} expired code-signing {identity_noun(count)}.")
    return 0



def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    signing = subparsers.add_parser("signing", help="Signing and notarization helpers.")
    signing_subparsers = signing.add_subparsers(dest="signing_command", required=True)

    autosetup = signing_subparsers.add_parser("autosetup", help="Detect Xcode/keychain signing config and write .devconfig.toml.")
    autosetup.set_defaults(func=signing_autosetup)

    delete_expired = signing_subparsers.add_parser(
        "delete-expired-identities",
        help="Delete expired code-signing identities from the keychain search list.",
    )
    delete_expired.add_argument(
        "--dry-run",
        action="store_true",
        help="Print expired identities without deleting them.",
    )
    delete_expired.add_argument(
        "--keychain",
        help="Delete only from this keychain instead of the default keychain search list.",
    )
    delete_expired.set_defaults(func=signing_delete_expired_identities)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
