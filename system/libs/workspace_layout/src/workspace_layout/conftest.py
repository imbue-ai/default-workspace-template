import socket
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest
from loguru import logger

from workspace_layout.cli import LayoutCliContext
from workspace_layout.client import ENV_MINDS_CHAT_ID
from workspace_layout.client import ENV_MNGR_AGENT_ID
from workspace_layout.testing import LoopbackShell
from workspace_layout.testing import write_registry

# How long the cut-off listener waits for the one request it answers.
_CUT_OFF_ACCEPT_TIMEOUT_SECONDS: Final[float] = 5.0


@pytest.fixture(autouse=True)
def _isolate_agent_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide the ambient agent identity from every test: an op's requester is read from ``MINDS_CHAT_ID``, falling
    back to ``MNGR_AGENT_ID``, so a test asserting on a requester reads its own inputs only if both are cleared."""
    monkeypatch.delenv(ENV_MINDS_CHAT_ID, raising=False)
    monkeypatch.delenv(ENV_MNGR_AGENT_ID, raising=False)


@pytest.fixture
def loguru_records() -> Iterator[list[str]]:
    """Every loguru message logged while the test runs, as ``"<LEVEL> <message>"``."""
    messages: list[str] = []
    handler_id = logger.add(
        lambda message: messages.append(f"{message.record['level'].name} {message.record['message']}"),
        level="DEBUG",
        format="{message}",
    )
    try:
        yield messages
    finally:
        logger.remove(handler_id)


@pytest.fixture
def loopback_shell() -> Iterator[LoopbackShell]:
    with LoopbackShell() as shell:
        yield shell


@pytest.fixture
def registry(tmp_path: Path) -> Path:
    """A registry holding the built-in apps a command names."""
    return write_registry(tmp_path / "apps.toml", ["files", "terminal", "chat", "browser"])


@pytest.fixture
def layout_context(loopback_shell: LoopbackShell, registry: Path) -> LayoutCliContext:
    """The command pointed at the loopback shell and the registry, asking as nobody."""
    return LayoutCliContext(
        shell_url=loopback_shell.url,
        apps_file=registry,
        requester=None,
        registration_timeout_seconds=0.0,
        read_timeout_seconds=5.0,
        op_timeout_seconds=5.0,
    )


@pytest.fixture
def cut_off_shell_url() -> Iterator[str]:
    """The URL of a listener that answers one request with a 200 promising more body than it sends, then hangs up,
    as a shell that stops mid-answer does."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(_CUT_OFF_ACCEPT_TIMEOUT_SECONDS)

    def answer_once() -> None:
        connection, _address = listener.accept()
        with connection:
            # The whole request is read first, so the hang-up is a clean end of the answer rather than a reset.
            received = b""
            while b"\r\n\r\n" not in received:
                chunk = connection.recv(4096)
                if not chunk:
                    return
                received += chunk
            connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n" + b'{"desktops": [')

    thread = threading.Thread(target=answer_once, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    finally:
        thread.join(timeout=_CUT_OFF_ACCEPT_TIMEOUT_SECONDS)
        listener.close()


@pytest.fixture
def silent_shell_url() -> Iterator[str]:
    """The URL of a listener that takes connections and never answers, as a wedged shell does."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    try:
        yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    finally:
        listener.close()
