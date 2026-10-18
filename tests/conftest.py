from collections.abc import Iterator

import pytest

from tests.smoke_utils import isolate_home
from wgsextract_cli.core.config import reload_settings, settings
from wgsextract_cli.core.dependencies import clear_tool_caches


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("wgsextract", "WGS Extract real-data test inputs")
    group.addoption(
        "--fake-data",
        action="store_true",
        help="Run the E2E suites against generated synthetic data even when "
        "input_path/reference_fasta are configured.",
    )
    group.addoption(
        "--full-data",
        action="store_true",
        help="Run real-data microarray checks over the whole genome instead of chr1.",
    )
    group.addoption("--real-input", help="Real BAM/CRAM input for real-data tests.")
    group.addoption("--real-vcf", help="Real VCF input for real-data smoke tests.")
    group.addoption(
        "--real-ref", help="Reference FASTA or library directory for real-data tests."
    )
    group.addoption(
        "--real-targets", help="Microarray *.targets.tab.gz for real-data tests."
    )
    group.addoption("--real-fastq-r1", help="Real FASTQ R1 for alignment smoke tests.")
    group.addoption("--real-fastq-r2", help="Real FASTQ R2 for alignment smoke tests.")
    group.addoption("--real-region", help="Region override for real-data smoke tests.")
    group.addoption("--cnv-map", help="Mappability map for the real CNV smoke test.")
    group.addoption(
        "--pet-species", default="dog", help="Species for the real pet-align test."
    )


@pytest.fixture(autouse=True)
def _isolate_user_config(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Hide the developer's config.toml from tests and the CLI subprocesses they
    spawn by pointing the home/config dirs at an empty per-test home. Tests
    marked ``real_config`` intentionally read the real config.
    """
    saved = dict(settings)
    if request.node.get_closest_marker("real_config") is None:
        isolate_home(monkeypatch, tmp_path_factory.mktemp("home"))
        reload_settings()
    try:
        yield
    finally:
        settings.clear()
        settings.update(saved)


@pytest.fixture(autouse=True)
def _fresh_tool_caches() -> Iterator[None]:
    """Tool resolution is cached per process; isolate tests that mock it."""
    clear_tool_caches()
    yield
    clear_tool_caches()
