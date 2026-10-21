"""Restore the vanilla VM's standard policy through authenticated Settings UI."""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

HERE = Path(__file__).resolve().parent
POLICY_KEYS = (
    "assessments_enabled",
    "developer_id_enabled",
    "notarized_developer_id_enabled",
)


def standard_policy(state: object) -> bool:
    return isinstance(state, dict) and all(
        state.get(key) is True for key in POLICY_KEYS
    )


def require_ui_tools() -> None:
    if importlib.util.find_spec("vncdotool") is None or not shutil.which("swift"):
        raise SystemExit(
            "macOS policy provisioning requires Swift and the dev Pixi environment; "
            "use pixi run -e dev python scripts/installlab/lab.py prepare macos."
        )


def check(ssh: list[str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [*ssh, "/usr/bin/python3 -"],
        input=(HERE / "guest" / "macos-policy.py").read_text(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode not in (0, 1):
        result.check_returncode()
    state = json.loads(result.stdout)
    if (result.returncode == 0) != standard_policy(state):
        raise ValueError(f"Inconsistent Gatekeeper policy evidence: {result.stdout}")
    return result


@dataclass(frozen=True)
class Label:
    text: str
    x: float
    y: float


def locate(labels: list[Label], captions: set[str]) -> Label | None:
    names = {" ".join(caption.split()).casefold() for caption in captions}
    matches = [
        label for label in labels if " ".join(label.text.split()).casefold() in names
    ]
    if len(matches) > 1:
        raise ValueError(f"Ambiguous Settings controls: {matches}")
    return matches[0] if matches else None


def restore(ssh: list[str], vnc_log: Path, evidence: Path) -> None:
    before = check(ssh)
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "policy-before.json").write_text(before.stdout)
    if before.returncode == 0:
        return
    print(before.stderr.strip(), flush=True)
    require_ui_tools()
    from vncdotool import api  # type: ignore[import-untyped]

    match = re.search(r"vnc://[^\s]+", vnc_log.read_text())
    if match is None:
        raise RuntimeError("Tart did not provide an authenticated VNC endpoint.")
    endpoint = urlsplit(match[0])
    if endpoint.hostname != "127.0.0.1" or not endpoint.password or not endpoint.port:
        raise ValueError("Expected Tart's authenticated loopback VNC endpoint.")
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        desktop = subprocess.run(
            [*ssh, "pgrep -x Finder >/dev/null"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        (evidence / "desktop.log").write_text(desktop.stderr)
        if desktop.returncode == 0:
            break
        time.sleep(2)
    else:
        raise TimeoutError(f"The VM's signed-in desktop did not start; see {evidence}")
    subprocess.run(
        [
            *ssh,
            "open 'x-apple.systempreferences:"
            "com.apple.settings.PrivacySecurity.extension?Security'",
        ],
        check=True,
        timeout=30,
    )
    with api.connect(
        f"127.0.0.1::{endpoint.port}",
        password=unquote(endpoint.password),
        timeout=15,
    ) as client:

        def labels() -> list[Label]:
            image = evidence / "settings.png"
            client.captureScreen(str(image))
            result = subprocess.run(
                ["swift", str(HERE / "macos_policy_ocr.swift"), str(image)],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
            return [Label(**row) for row in json.loads(result.stdout)]

        def wait(captions: set[str]) -> Label:
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                found = locate(labels(), captions)
                if found:
                    return found
                time.sleep(1)
            raise TimeoutError(f"Settings control missing: {captions}; see {evidence}")

        def click(label: Label) -> None:
            client.mouseMove(round(label.x), round(label.y))
            client.mousePress(1)
            client.pause(0.5)

        click(wait({"App Store"}))
        click(
            wait(
                {
                    "App Store & Known Developers",
                    "App Store and identified developers",
                    "App Store & Identified Developers",
                }
            )
        )
        # Only the disposable vendor fixture account is used, never host credentials.
        wait({"Modify Settings"})
        click(wait({"Password"}))
        for character in "admin":
            client.keyPress(character)
            client.pause(0.05)
        client.keyPress("enter")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            after = check(ssh)
            (evidence / "policy-after.json").write_text(after.stdout)
            if after.returncode == 0:
                client.captureScreen(str(evidence / "settings-restored.png"))
                subprocess.run([*ssh, "sync && sleep 5"], check=True, timeout=30)
                return
            time.sleep(1)
        raise RuntimeError(f"Settings did not restore Gatekeeper; see {evidence}")
