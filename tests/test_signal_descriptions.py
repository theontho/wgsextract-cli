import signal

from wgsextract_cli.main import describe_signal


def test_describe_signal_explains_keyboard_interrupt():
    assert describe_signal(signal.SIGINT) == (
        "signal 2 (SIGINT: interrupt from keyboard, usually Ctrl+C)"
    )


def test_describe_signal_explains_termination_request():
    assert describe_signal(signal.SIGTERM) == (
        "signal 15 (SIGTERM: normal termination request from kill or a process manager)"
    )


def test_describe_signal_handles_unknown_signal():
    assert describe_signal(9999) == "signal 9999: unknown signal"
