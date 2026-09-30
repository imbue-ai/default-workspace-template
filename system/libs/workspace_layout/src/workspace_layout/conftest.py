from collections.abc import Iterator

import pytest
from loguru import logger

from workspace_layout.client import ENV_MINDS_CHAT_ID
from workspace_layout.client import ENV_MNGR_AGENT_ID
from workspace_layout.testing import LoopbackShell


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
    shell = LoopbackShell()
    shell.start()
    try:
        yield shell
    finally:
        shell.close()
