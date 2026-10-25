"""The CLI's output is UTF-8 even when the platform's default isn't."""

import io

from wgsextract_cli.cli.bootstrap import _reconfigure_stream


def test_piped_output_becomes_utf8() -> None:
    # Piped stdout on Windows uses the ANSI code page, which has no emoji.
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252")
    _reconfigure_stream(stream)
    stream.write("ℹ️: done\n")
    stream.flush()
    assert raw.getvalue() == "ℹ️: done\n".encode()
