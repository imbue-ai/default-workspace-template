import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from loguru import logger
from workspace_bare_scripts import message_chat

from screen.messenger import ChatAgentMessenger


def _warnings_during(action: Callable[[], None]) -> list[str]:
    seen: list[str] = []
    handle = logger.add(lambda message: seen.append(message.record["message"]), level="WARNING")
    try:
        action()
    finally:
        logger.remove(handle)
    return seen


def test_a_delivered_send_is_silent_and_carries_the_fleet_sentinel(tmp_path: Path) -> None:
    sent: list[tuple[str, str, Path]] = []

    def send(agent_id: str, text: str, repo_root: Path) -> int:
        sent.append((agent_id, text, repo_root))
        return message_chat.EXIT_DELIVERED

    messenger = ChatAgentMessenger(repo_root=tmp_path, send=send)

    warnings = _warnings_during(lambda: messenger.message_agent("agent-1", "Main", "your turn"))

    assert sent == [("agent-1", "your turn", tmp_path)]
    assert warnings == []


def test_an_undelivered_send_is_a_warning_naming_the_agent(tmp_path: Path) -> None:
    messenger = ChatAgentMessenger(repo_root=tmp_path, send=lambda agent_id, text, root: message_chat.EXIT_FAILED)

    warnings = _warnings_during(lambda: messenger.message_agent("agent-1", "Main", "your turn"))

    assert len(warnings) == 1
    assert "Main" in warnings[0] and "may not have landed" in warnings[0]


def test_a_send_that_cannot_reach_the_chat_app_is_a_warning_naming_the_error(tmp_path: Path) -> None:
    def send(agent_id: str, text: str, root: Path) -> int:
        raise ConnectionRefusedError("nothing listens on 8000")

    messenger = ChatAgentMessenger(repo_root=tmp_path, send=send)

    warnings = _warnings_during(lambda: messenger.message_agent("agent-1", "Main", "your turn"))

    assert len(warnings) == 1
    assert "Main" in warnings[0] and "nothing listens on 8000" in warnings[0]


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_send_that_dies_of_another_error_is_a_warning_and_never_raises(tmp_path: Path) -> None:
    def send(agent_id: str, text: str, root: Path) -> int:
        raise RuntimeError("the chat app answered nonsense")

    messenger = ChatAgentMessenger(repo_root=tmp_path, send=send)

    warnings = _warnings_during(lambda: messenger.message_agent("agent-1", None, "your turn"))

    assert len(warnings) == 1
    assert "agent-1" in warnings[0] and "failed" in warnings[0]


def test_a_slow_send_does_not_hold_the_caller_past_the_timeout(tmp_path: Path) -> None:
    released = threading.Event()

    def send(agent_id: str, text: str, root: Path) -> int:
        released.wait(timeout=5.0)
        return message_chat.EXIT_DELIVERED

    messenger = ChatAgentMessenger(repo_root=tmp_path, send=send, timeout_seconds=0.05)
    started = time.monotonic()

    warnings = _warnings_during(lambda: messenger.message_agent("agent-1", None, "your turn"))

    released.set()
    assert time.monotonic() - started < 2.0
    assert len(warnings) == 1
    assert "has not answered" in warnings[0] and "agent-1" in warnings[0]
