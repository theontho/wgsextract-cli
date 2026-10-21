import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

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


def test_contradictory_native_evidence_is_an_error(
    policy: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = subprocess.CompletedProcess(
        ["ssh"], 0, stdout='{"assessments_enabled": true}', stderr=""
    )
    monkeypatch.setattr(policy.subprocess, "run", lambda *args, **kwargs: result)
    with pytest.raises(ValueError, match="Inconsistent Gatekeeper"):
        policy.check(["ssh", "admin@fixture"])


def test_boot_ssh_drop_is_retried(
    policy: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    healthy = subprocess.CompletedProcess(
        ["ssh"],
        0,
        stdout=json.dumps(dict.fromkeys(policy.POLICY_KEYS, True)),
        stderr="",
    )
    run = Mock(
        side_effect=[
            subprocess.CompletedProcess(
                ["ssh"], 255, stdout="", stderr="Connection reset"
            ),
            healthy,
        ]
    )
    sleep = Mock()
    monkeypatch.setattr(policy.subprocess, "run", run)
    monkeypatch.setattr(policy.time, "sleep", sleep)
    assert policy.check(["ssh", "admin@fixture"]) is healthy
    assert run.call_count == 2
    sleep.assert_called_once_with(10)


def test_confirmed_nonstandard_policy_is_not_retried(
    policy: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    rejected = subprocess.CompletedProcess(
        ["ssh"], 1, stdout='{"assessments_enabled": true}', stderr="Nonstandard policy"
    )
    run = Mock(return_value=rejected)
    sleep = Mock()
    monkeypatch.setattr(policy.subprocess, "run", run)
    monkeypatch.setattr(policy.time, "sleep", sleep)
    assert policy.check(["ssh", "admin@fixture"]) is rejected
    assert run.call_count == 1
    sleep.assert_not_called()


def test_persistent_ssh_failure_is_not_a_policy_success(
    policy: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = Mock(
        return_value=subprocess.CompletedProcess(
            ["ssh"], 255, stdout="", stderr="Connection refused"
        )
    )
    monkeypatch.setattr(policy.subprocess, "run", run)
    monkeypatch.setattr(policy.time, "sleep", Mock())
    with pytest.raises(subprocess.CalledProcessError):
        policy.check(["ssh", "admin@fixture"])
    assert run.call_count == 5


def test_healthy_policy_does_not_reconfigure_settings(
    policy: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = json.dumps(dict.fromkeys(policy.POLICY_KEYS, True))
    result = subprocess.CompletedProcess(["ssh"], 0, stdout=state, stderr="")
    monkeypatch.setattr(policy, "check", lambda ssh: result)
    policy.restore(["ssh"], tmp_path / "absent-vnc-log", tmp_path)
    assert (tmp_path / "policy-before.json").read_text() == state


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
