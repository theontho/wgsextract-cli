import os
import signal
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from tests.fake_tools import FakePixi
from tests.smoke_utils import ensure_fake_data, isolate_home, state_directory_setting
from wgsextract_cli.core.config import get_config_path, reload_settings, settings
from wgsextract_cli.core.dependencies import clear_tool_caches
from wgsextract_cli.core.runtime import repo_root, source_checkout_root


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


_CHECKOUT_STATE = repo_root() / "state"
_CHECKOUT_STATE_EXISTED = _CHECKOUT_STATE.exists()


def pytest_sessionstart() -> None:
    """Initialize real/session test defaults explicitly, not during config import."""
    reload_settings()


def pytest_sessionfinish(session: pytest.Session) -> None:
    """Fail the run if a test recorded a run in the checkout's state/."""
    if not _CHECKOUT_STATE_EXISTED and _CHECKOUT_STATE.exists():
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        print(f"\nA test wrote {_CHECKOUT_STATE}; point state_directory at tmp_path.")


_REAL_CONFIG = get_config_path()


def _isolate(
    monkeypatch: pytest.MonkeyPatch, home: Path, config_text: str = ""
) -> None:
    """Point the home/config dirs at ``home``, with ``config_text`` as its
    config.toml. Commands record the last run (and report-issue saves reports)
    in the data folder, which is this checkout; keep them in ``home``, from
    CLI subprocesses through the config and in-process also when a test
    replaces the config."""
    monkeypatch.setattr("wgsextract_cli.core.config._config_path_override", None)
    isolate_home(monkeypatch, home)
    config = get_config_path()
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(config_text + state_directory_setting(home))
    monkeypatch.setattr(
        "wgsextract_cli.core.last_run._default_state_directory",
        lambda: home / "state",
    )
    reload_settings()


@pytest.fixture(scope="session", autouse=True)
def _isolate_session_config(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[None]:
    """The same isolation for module and session fixtures, which run before
    a test's own."""
    saved = dict(settings)
    with pytest.MonkeyPatch.context() as monkeypatch:
        _isolate(monkeypatch, tmp_path_factory.mktemp("session-home"))
        yield
    settings.clear()
    settings.update(saved)


@pytest.fixture(autouse=True)
def _isolate_user_config(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Hide the developer's config.toml from tests and the CLI subprocesses they
    spawn by pointing the home/config dirs at a per-test home. Tests marked
    ``real_config`` get a copy of the real config.
    """
    saved = dict(settings)
    real = ""
    if request.node.get_closest_marker("real_config") is not None:
        real = _REAL_CONFIG.read_text() if _REAL_CONFIG.is_file() else ""
        real = "".join(
            line + "\n"
            for line in real.splitlines()
            if not line.lstrip().startswith("state_directory")
        )
    _isolate(monkeypatch, tmp_path_factory.mktemp("home"), real)
    try:
        yield
    finally:
        settings.clear()
        settings.update(saved)


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


@pytest.fixture(autouse=True)
def _restore_signal_handlers() -> Iterator[None]:
    """Tests that run the CLI's entry point install its SIGINT/SIGTERM
    handlers (exit 128+N); put Python's back so a later test sees Ctrl-C as
    KeyboardInterrupt whatever ran before it on the same worker."""
    saved = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    yield
    for sig, handler in saved.items():
        signal.signal(sig, handler)


@pytest.fixture
def fake_pixi(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakePixi:
    """The pixi running the CLI (as `pixi run` exports PIXI_EXE), outside any
    of its environments, for this checkout's workspace. PATH holds only
    ``host_bin``: the host's own programs."""
    root = source_checkout_root()
    pixi = FakePixi(tmp_path / "pixi", root / "pixi.toml" if root else None)
    monkeypatch.setenv("PIXI_EXE", str(pixi.exe))
    monkeypatch.setenv("PATH", str(pixi.host_bin))
    monkeypatch.delenv("PIXI_ENVIRONMENT_NAME", raising=False)
    return pixi


@pytest.fixture
def ram(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Set this computer's RAM: ``ram(total, available=total)`` in bytes, as
    psutil reports it, with no Linux cgroup cap below it (the test runner's
    own cgroup can be capped)."""
    import psutil

    from wgsextract_cli.core import alignment_memory

    def set_ram(total: int, available: int | None = None) -> None:
        memory = psutil.virtual_memory()._replace(
            total=total, available=total if available is None else available
        )
        monkeypatch.setattr(psutil, "virtual_memory", lambda: memory)
        monkeypatch.setattr(alignment_memory, "_cgroup_available_memory", lambda: None)

    return set_ram


@pytest.fixture
def open_stdin() -> Iterator[None]:
    """Our stdin is an open pipe with nothing in it, like a terminal."""
    read, write = os.pipe()
    saved = os.dup(0)
    os.dup2(read, 0)
    yield
    os.dup2(saved, 0)
    for fd in (read, write, saved):
        os.close(fd)
