import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "installlab"


@pytest.fixture
def policy(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "macos_policy", SCRIPTS / "macos_policy.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "macos_policy", module)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "updates",
    [
        {"assessments_enabled": False},
        {"developer_id_enabled": False, "notarized_developer_id_enabled": False},
        {"notarized_developer_id_enabled": False},
        {"developer_id_enabled": "true"},
        {"developer_id_enabled": 1},
    ],
)
def test_master_switch_alone_does_not_prove_normal_policy(
    policy: ModuleType, updates: dict[str, object]
) -> None:
    state = dict.fromkeys(policy.POLICY_KEYS, True)
    assert policy.standard_policy(state)
    assert not policy.standard_policy({**state, **updates})
    assert not policy.standard_policy({})


_FAKE_SSH = """
import json, sys
replies = json.load(open(sys.argv[1]))
calls = sys.argv[1] + ".calls"
with open(calls, "a") as log:
    log.write(json.dumps({"command": sys.argv[2:], "stdin": sys.stdin.read()}) + "\\n")
code, out, err = replies[min(len(open(calls).readlines()), len(replies)) - 1]
sys.stdout.write(out)
sys.stderr.write(err)
sys.exit(code)
"""


class FakeSSH:
    """An `ssh` to the VM that answers each connection with the next of
    ``replies`` (exit code, stdout, stderr), the last one again after that."""

    def __init__(self, folder: Path, *replies: tuple[int, str, str]) -> None:
        self.replies = folder / "replies.json"
        self.replies.write_text(json.dumps(replies))
        script = folder / "ssh.py"
        script.write_text(_FAKE_SSH)
        self.command = [sys.executable, str(script), str(self.replies)]

    def calls(self) -> list[dict[str, object]]:
        log = Path(str(self.replies) + ".calls")
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]


@pytest.fixture
def no_wait(policy: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """The retry back-off is the clock's; don't wait it out."""
    monkeypatch.setattr(policy.time, "sleep", lambda _seconds: None)


def _healthy(policy: ModuleType) -> tuple[int, str, str]:
    return 0, json.dumps(dict.fromkeys(policy.POLICY_KEYS, True)), ""


def test_contradictory_native_evidence_is_an_error(
    policy: ModuleType, tmp_path: Path
) -> None:
    ssh = FakeSSH(tmp_path, (0, '{"assessments_enabled": true}', ""))
    with pytest.raises(ValueError, match="Inconsistent Gatekeeper"):
        policy.check(ssh.command)


def test_policy_probe_runs_the_guest_script_over_ssh(
    policy: ModuleType, tmp_path: Path
) -> None:
    ssh = FakeSSH(tmp_path, _healthy(policy))
    assert policy.check(ssh.command).returncode == 0
    [call] = ssh.calls()
    assert call["command"] == ["/usr/bin/python3 -"]
    guest = SCRIPTS / "guest" / "macos-policy.py"
    assert call["stdin"] == guest.read_text()


def test_boot_ssh_drop_is_retried(
    policy: ModuleType,
    tmp_path: Path,
    no_wait: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ssh = FakeSSH(tmp_path, (255, "", "Connection reset"), _healthy(policy))
    assert policy.check(ssh.command).returncode == 0
    assert len(ssh.calls()) == 2
    assert "Connection reset" in capsys.readouterr().err


def test_confirmed_nonstandard_policy_is_not_retried(
    policy: ModuleType, tmp_path: Path, no_wait: None
) -> None:
    ssh = FakeSSH(tmp_path, (1, '{"assessments_enabled": true}', "Nonstandard policy"))
    rejected = policy.check(ssh.command)
    assert (rejected.returncode, rejected.stderr) == (1, "Nonstandard policy")
    assert len(ssh.calls()) == 1


def test_persistent_ssh_failure_is_not_a_policy_success(
    policy: ModuleType, tmp_path: Path, no_wait: None
) -> None:
    ssh = FakeSSH(tmp_path, (255, "", "Connection refused"))
    with pytest.raises(subprocess.CalledProcessError):
        policy.check(ssh.command)
    assert len(ssh.calls()) == 5


def test_healthy_policy_does_not_reconfigure_settings(
    policy: ModuleType, tmp_path: Path
) -> None:
    ssh = FakeSSH(tmp_path, _healthy(policy))
    evidence = tmp_path / "evidence"
    policy.restore(ssh.command, tmp_path / "absent-vnc-log", evidence)
    assert (evidence / "policy-before.json").read_text() == _healthy(policy)[1]
    assert len(ssh.calls()) == 1


def test_ambiguous_ui_controls_fail_instead_of_clicking(
    policy: ModuleType,
) -> None:
    labels = [policy.Label("App Store", 100, 100), policy.Label("App Store", 200, 200)]
    with pytest.raises(ValueError, match="Ambiguous Settings controls"):
        policy.locate(labels, {"App Store"})


@pytest.mark.parametrize("state", [None, {}, {"assessments_enabled": True}])
def test_oracle_rejects_missing_or_incomplete_policy(
    policy: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    state: object,
) -> None:
    spec = importlib.util.spec_from_file_location("oracle", SCRIPTS / "oracle.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "oracle", module)
    spec.loader.exec_module(module)
    (tmp_path / "gatekeeper-status.txt").write_text("assessments enabled")
    if state is not None:
        (tmp_path / "gatekeeper-policy.json").write_text(json.dumps(state))
    oracle = module.Oracle(tmp_path, "nonce", {})
    oracle.check_gatekeeper_enabled()
    assert oracle.checks[0].ok
    assert not oracle.checks[1].ok
