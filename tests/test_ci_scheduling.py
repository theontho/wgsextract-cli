import pytest

pytest_plugins = ["pytester"]


def test_shared_smoke_modules_run_on_one_xdist_worker(
    pytester: pytest.Pytester,
) -> None:
    pytester.makeconftest("from tests.conftest import pytest_collection_modifyitems")
    test = """
from pathlib import Path
from tests.smoke_utils import ensure_fake_data

def test_worker(request):
    with Path("workers.txt").open("a") as output:
        output.write(request.config.workerinput["workerid"] + "\\n")
"""
    pytester.makepyfile(test_first=test, test_second=test)
    (pytester.path / "out").mkdir()
    result = pytester.runpytest_subprocess(
        "-n",
        "2",
        "--dist",
        "loadgroup",
        "-vv",
        "--basetemp",
        str(pytester.path / "out" / "pytest"),
    )

    result.assert_outcomes(passed=2)
    workers = (pytester.path / "workers.txt").read_text().splitlines()
    assert len(workers) == 2 and len(set(workers)) == 1
    assert "@shared-smoke" in result.stdout.str()


def test_explicit_config_override_does_not_leak_between_tests(
    pytester: pytest.Pytester,
) -> None:
    pytester.makeconftest("from tests.conftest import _isolate_user_config")
    pytester.makepyfile(
        """
from wgsextract_cli.core import config

def test_first(tmp_path):
    own = tmp_path / "override.toml"
    own.write_text('reference_fasta = "/cfg/ref.fa"\\n')
    config.set_config_path(own)
    config.reload_settings()
    assert config.settings["reference_fasta"] == "/cfg/ref.fa"

def test_second():
    assert config.settings.get("reference_fasta") is None
"""
    )
    (pytester.path / "out").mkdir()
    result = pytester.runpytest_subprocess(
        "--basetemp", str(pytester.path / "out" / "pytest")
    )
    result.assert_outcomes(passed=2)
