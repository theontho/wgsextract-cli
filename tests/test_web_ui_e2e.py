import signal
from collections.abc import AsyncGenerator
from unittest.mock import patch

import pytest

pytest.importorskip("nicegui")

from nicegui import ui
from nicegui.testing import User, user_simulation

from wgsextract_cli.core.messages import GUI_LABELS
from wgsextract_cli.ui.web_gui import main_page
from wgsextract_cli.ui.web_gui_parts.controller import controller
from wgsextract_cli.ui.web_gui_parts.state import state


def _ignore_signal(*_: object) -> None:
    return None


def _clear_client_signal_handlers() -> None:
    import engineio.base_client
    import socketio.base_client

    engineio.base_client.original_signal_handler = _ignore_signal
    socketio.base_client.original_signal_handler = _ignore_signal
    signal.signal(signal.SIGINT, _ignore_signal)


@pytest.fixture
async def user(
    caplog: pytest.LogCaptureFixture, reset_web_state: None
) -> AsyncGenerator[User, None]:
    try:
        async with user_simulation(root=main_page) as simulated_user:
            yield simulated_user

            logs = [
                record
                for record in caplog.get_records("call")
                if record.levelname == "ERROR"
            ]
            if logs:
                pytest.fail("There were unexpected ERROR logs.", pytrace=False)
    finally:
        _clear_client_signal_handlers()


@pytest.fixture
def reset_web_state(monkeypatch: pytest.MonkeyPatch) -> None:
    state.bam_path = ""
    state.vcf_path = ""
    state.fastq_path = ""
    state.ref_path = ""
    state.out_dir = ""
    state.active_tab = "flow"
    state.logs = {"Main": []}
    state.log_tabs = ["Main"]
    state.current_log_tab = "Main"
    monkeypatch.setattr(state, "get_info", lambda path: None)


@pytest.mark.asyncio
async def test_navigation(user: User):
    """Test that clicking sidebar buttons changes the active tab."""
    await user.open("/")
    assert state.active_tab == "flow"

    user.find(GUI_LABELS["tab_vcf"]).click()
    assert state.active_tab == "vcf"

    user.find(GUI_LABELS["tab_fastq"]).click()
    assert state.active_tab == "fastq"


@pytest.mark.asyncio
async def test_settings_interaction(user: User):
    """Test that input fields in settings bind correctly to state."""
    state.ref_path = ""
    state.active_tab = "settings"
    await user.open("/")

    user.find(ui.input).type("/tmp/test_ref")
    assert state.ref_path == "/tmp/test_ref"


@patch("wgsextract_cli.ui.web_gui_parts.common.run_generic_cmd")
@pytest.mark.asyncio
async def test_command_trigger(mock_run, user: User):
    """Test that clicking a command button triggers the command."""
    # Ensure button is enabled
    state.fastq_path = "test.bam"
    state.active_tab = "fastq"
    await user.open("/")

    # Standard label 'Index' should work now that logic in fastq.py is fixed
    user.find("Index").click()

    mock_run.assert_called_once()
    assert mock_run.call_args[0][0]["cmd"] == "index"


@pytest.mark.asyncio
async def test_log_display(user: User):
    """Test that log messages appear in the UI."""
    await user.open("/")
    controller.log("Test log message", tab="Main")
    await user.should_see("Test log message")
