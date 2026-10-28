"""Tests for the room's reader: what a message typed into its pane comes to.

The reader's transcript records and its author rule are checked by the chat app's tests, which
load the script the same way; what is checked here is the pane itself, a real pty.
"""

from __future__ import annotations

import importlib.util
import io
import os
import pty
import threading
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).with_name("room_agent.py")
_spec = importlib.util.spec_from_file_location("room_agent", _SCRIPT)
assert _spec is not None and _spec.loader is not None
room_agent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(room_agent)


def test_a_pasted_paragraph_longer_than_a_terminal_line_arrives_whole() -> None:
    """A tty in canonical mode holds at most 4095 bytes of one line and drops the rest; a
    paragraph `mngr message` pastes can be longer, so the reader takes the pane out of that
    mode before reading."""
    try:
        master_fd, slave_fd = pty.openpty()
    except OSError as e:
        pytest.skip(f"no pty here: {e}")
    paragraph = "x" * 6000
    received: list[str] = []
    with os.fdopen(slave_fd, "rb") as stdin:
        assert room_agent.unbuffer_tty(stdin) is not None
        reader = threading.Thread(
            target=lambda: received.append(next(room_agent.read_messages(stdin))),
            daemon=True,
        )
        reader.start()
        os.write(master_fd, (paragraph + "\n").encode())
        reader.join(timeout=10)
    os.close(master_fd)

    assert received == [paragraph]


def test_a_stream_that_is_not_a_tty_is_read_as_it_is() -> None:
    assert room_agent.unbuffer_tty(io.BytesIO(b"hello\n")) is None
