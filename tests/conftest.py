from collections.abc import Iterator

import pytest

from tests.smoke_utils import ensure_fake_data, isolate_home
from wgsextract_cli.core.config import reload_settings, settings
from wgsextract_cli.core.dependencies import clear_tool_caches


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Keep module-owned outputs and shared synthetic-data generation serialized."""
    for item in items:
        module = getattr(item, "module", None)
        if module is not None:
            group = (
                "shared-smoke"
                if getattr(module, "ensure_fake_data", None) is ensure_fake_data
                else module.__name__
            )
            item.add_marker(pytest.mark.xdist_group(group))


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("wgsextract", "WGS Extract real-data test inputs")
    group.addoption(
        "--yleaf-upstream", help="Local tagged Yleaf checkout for optional parity."
    )
    group.addoption(
        "--yleaf-parity-output",
        help="Existing upstream output directory for QC parity.",
    )
    group.addoption(
        "--yleaf-library", help="Extracted Yleaf data root for optional parity."
    )
    group.addoption(
        "--yleaf-parity-input", help="Original input for optional extraction parity."
    )
    group.addoption(
        "--yleaf-parity-build",
        choices=("hg19", "hg38", "t2t"),
        help="Reference build of --yleaf-parity-input.",
    )
    group.addoption(
        "--yleaf-parity-ref", help="Matching FASTA for CRAM/VCF extraction parity."
    )
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
    group.addoption(
        "--pet-species", default="dog", help="Species for the real pet-align test."
    )


def pytest_sessionstart() -> None:
    """Initialize real/session test defaults explicitly, not during config import."""
    reload_settings()


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
        monkeypatch.setattr("wgsextract_cli.core.config._config_path_override", None)
        isolate_home(monkeypatch, tmp_path_factory.mktemp("home"))
        reload_settings()
    try:
        yield
    finally:
        settings.clear()
        settings.update(saved)


@pytest.fixture(autouse=True)
def _isolate_run_state(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Commands record the last run, and report-issue saves reports, in the data
    folder, which is the source checkout here; keep tests out of it."""
    root = tmp_path_factory.mktemp("data")
    monkeypatch.setattr(
        "wgsextract_cli.core.last_run.state_directory", lambda: root / "state"
    )
    monkeypatch.setattr(
        "wgsextract_cli.api.support.reports_directory", lambda: root / "reports"
    )


@pytest.fixture(autouse=True)
def _no_segmented_size_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """download_file asks a server for the file's size (HEAD) before a large
    download; keep tests that mock urlopen off the network. Tests of the
    segmented path call it directly."""
    from wgsextract_cli.core.ref_library import segmented

    monkeypatch.setattr(segmented, "ranged_size", lambda url: None)


@pytest.fixture(autouse=True)
def _fresh_tool_caches(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Tool resolution is cached per process; isolate tests that mock it.

    `pixi run` exports PIXI_EXE, which pixi resolution prefers; hide it so tests
    that mock pixi lookup don't depend on how the test runner was launched.
    """
    monkeypatch.delenv("PIXI_EXE", raising=False)
    clear_tool_caches()
    yield
    clear_tool_caches()
