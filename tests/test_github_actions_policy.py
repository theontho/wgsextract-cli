from pathlib import Path

import pytest

from scripts import validate_github_actions as policy


def write_workflow(root: Path, uses: str) -> None:
    directory = root / ".github" / "workflows"
    directory.mkdir(parents=True)
    (directory / "test.yml").write_text(f"jobs:\n  test:\n    steps:\n      - {uses}\n")


def test_dependabot_can_update_pin_and_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_workflow(
        tmp_path,
        "uses: actions/setup-python@6d4fd22b20465ccc0f7354476588e8df7c9ca0e0 # v7.1.0",
    )
    monkeypatch.setattr(policy, "ROOT", tmp_path)
    assert policy.validate_workflows() == []


@pytest.mark.parametrize(
    ("uses", "error"),
    [
        ("uses: actions/checkout@v7.0.1 # v7.0.1", "full commit SHA"),
        ("uses: actions/checkout@3d3c42e # v7.0.1", "full commit SHA"),
        (
            "uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
            "version comment",
        ),
        (
            "uses: unknown/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1",
            "ALLOWED_ACTIONS",
        ),
    ],
)
def test_remote_action_policy_rejects_unsafe_or_unreviewed_refs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    uses: str,
    error: str,
) -> None:
    write_workflow(tmp_path, uses)
    monkeypatch.setattr(policy, "ROOT", tmp_path)
    assert any(error in item for item in policy.validate_workflows())
