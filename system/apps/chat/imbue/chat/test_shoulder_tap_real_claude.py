"""A real Claude chat's shoulder tap, read off the session ledger the pinned binary itself writes.

``mngr create`` starts the pinned ``claude`` on a private tmux server, the message is queued through mngr while a
turn runs, and the tap is the chat app's own executor pressing the real chord through mngr. Only the model is a
stand-in (``StandInAnthropicApi``), which lets each test hold a request open for exactly as long as it needs a turn
to stay in flight.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from imbue.chat.agent_discovery import AgentInfo
from imbue.chat.agent_manager import AgentManager
from imbue.chat.harnesses.claude.tap import ClaudeAtomicShoulderTap
from imbue.chat.harnesses.claude.tap import RECOVERY_MESSAGE
from imbue.chat.harnesses.claude.tap import TAP_CHORD
from imbue.chat.harnesses.session_watcher import AgentSessionWatcher
from imbue.chat.state import ChatAppState
from imbue.chat.testing import StandInToolUse
from imbue.chat.testing import build_test_state
from imbue.chat.testing import seed_agent_state
from imbue.chat.testing import stand_in_real_claude_agent
from imbue.chat.ws_broadcaster import WebSocketBroadcaster
from imbue.mngr.primitives import AgentId
from imbue.mngr.utils.polling import wait_for

pytestmark = [pytest.mark.real_claude]

_STAND_IN_API_KEY = "sk-ant-api03-stand-in-shoulder-tap"
_HOLD_SECONDS = 120.0
_WAIT_SECONDS = 120.0


def _is_main_loop_request(body: dict[str, Any]) -> bool:
    """The agent's own turn requests carry its tools; the side requests Claude Code makes (titles, topics) do not."""
    return bool(body.get("tools"))


def _last_user_message_text(body: dict[str, Any]) -> str:
    """The latest ``user`` message, which a ``system`` message carrying hook output can follow."""
    user_messages = [message for message in body.get("messages") or [] if message.get("role") == "user"]
    return json.dumps(user_messages[-1]) if user_messages else ""


def _answers_tool_call(body: dict[str, Any], tool_use_id: str) -> bool:
    return f'"tool_use_id": "{tool_use_id}"' in _last_user_message_text(body)


class _ScriptedReplies:
    """Answers the agent's turn requests by what each one carries, holding the ones a test needs in flight."""

    def __init__(self, script: Callable[[dict[str, Any]], str | StandInToolUse | None]) -> None:
        self._script = script
        self.bodies: list[dict[str, Any]] = []
        self._exchanges: list[str] = []
        self._lock = threading.Lock()

    def __call__(self, body: dict[str, Any]) -> str | StandInToolUse:
        is_main_loop = _is_main_loop_request(body)
        if is_main_loop:
            with self._lock:
                self.bodies.append(body)
        scripted = self._script(body) if is_main_loop else None
        reply = "ok" if scripted is None else scripted
        with self._lock:
            self._exchanges.append(
                f"{'turn' if is_main_loop else 'side'} request whose latest user message ends"
                f" {_last_user_message_text(body)[-300:]!r}"
                f" -> {reply!r}"
            )
        return reply

    def was_sent(self, *needles: str) -> bool:
        """Whether one turn request carried every one of ``needles``."""
        with self._lock:
            return any(all(needle in json.dumps(body.get("messages")) for needle in needles) for body in self.bodies)

    def describe(self) -> str:
        """Every request answered so far and the reply it got, for a failure message."""
        with self._lock:
            return "\n".join(self._exchanges)


@contextmanager
def _chat_state_over(agent: AgentInfo) -> Generator[tuple[ChatAppState, AgentManager], None, None]:
    manager = AgentManager.build(WebSocketBroadcaster())
    seed_agent_state(manager, agent.id, name=agent.name, labels=agent.labels, harness=agent.harness)
    state = build_test_state(agent_manager=manager)
    try:
        yield state, manager
    finally:
        state.shutdown()


@contextmanager
def _real_claude_chat(
    tmp_path: Path, project_dir: Path, monkeypatch: pytest.MonkeyPatch, replies: _ScriptedReplies
) -> Generator[tuple[AgentInfo, AgentManager, AgentSessionWatcher], None, None]:
    with (
        stand_in_real_claude_agent(
            project_dir, tmp_path / "mngr.log", f"tap-probe-{uuid4().hex}", replies, _STAND_IN_API_KEY, monkeypatch
        ) as (_api, agent),
        _chat_state_over(agent) as (state, manager),
    ):
        yield agent, manager, state.get_or_create_watcher(agent)


def _send(manager: AgentManager, agent: AgentInfo, text: str) -> None:
    failure = manager.send_message_to_agent(AgentId(agent.id), text)
    assert failure is None, f"mngr could not deliver {text!r}: {failure}"


def _wait_until_queued(watcher: AgentSessionWatcher, text: str) -> None:
    def is_queued() -> bool:
        watcher.get_all_events()
        return any(text in str(entry.get("content")) for entry in watcher.get_queued_messages())

    wait_for(is_queued, timeout=_WAIT_SECONDS, error_message=f"{text!r} never parked in Claude's queue")


@pytest.mark.timeout(900, func_only=False)
def test_a_tap_that_cancels_the_turn_a_queued_message_just_joined_sends_the_recovery_nudge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The queued message joins the running turn after a tool call, then the tap's chord cancels that turn.

    Claude folds a message queued mid-turn into the turn at its next tool result, and the chord then cancels the
    request that carried it: nothing answers the message, and no new turn starts. The ``active`` marker the turn ran
    under is still on disk, because an interrupt fires no Stop hook, so it says nothing about whether a turn followed
    the chord. The tap has to see the cancelled follow-on and send its recovery nudge, which starts the turn that
    finally answers the message.
    """
    project_dir = tmp_path / "project"
    first_message = f"tap-probe-first-{uuid4().hex}"
    queued_message = f"tap-probe-queued-{uuid4().hex}"
    answer = f"tap-probe-answered-{uuid4().hex}"
    is_second_read_released = threading.Event()
    is_queued_message_in_flight = threading.Event()
    is_test_over = threading.Event()

    def script(body: dict[str, Any]) -> str | StandInToolUse | None:
        if RECOVERY_MESSAGE in _last_user_message_text(body):
            return answer
        if _answers_tool_call(body, "toolu_tap_probe_second"):
            # The request that carries the queued message: held until the chord cancels it.
            is_queued_message_in_flight.set()
            is_test_over.wait(_HOLD_SECONDS)
            return None
        if _answers_tool_call(body, "toolu_tap_probe_first"):
            is_second_read_released.wait(_HOLD_SECONDS)
            return StandInToolUse(
                tool_use_id="toolu_tap_probe_second",
                name="Read",
                arguments={"file_path": str(project_dir / "second.txt")},
            )
        if first_message in _last_user_message_text(body):
            return StandInToolUse(
                tool_use_id="toolu_tap_probe_first",
                name="Read",
                arguments={"file_path": str(project_dir / "first.txt")},
            )
        return None

    replies = _ScriptedReplies(script)
    try:
        with _real_claude_chat(tmp_path, project_dir, monkeypatch, replies) as (agent, manager, watcher):
            (project_dir / "first.txt").write_text("first\n")
            (project_dir / "second.txt").write_text("second\n")
            _send(manager, agent, first_message)
            _send(manager, agent, queued_message)
            _wait_until_queued(watcher, queued_message)

            def press_once_the_queued_message_joined_the_turn() -> bool:
                is_second_read_released.set()
                wait_for(
                    is_queued_message_in_flight.is_set,
                    timeout=_WAIT_SECONDS,
                    error_message="Claude never sent the turn request that answers the second tool call:\n"
                    + replies.describe(),
                )
                assert replies.was_sent(queued_message), (
                    "Claude did not fold the queued message into its turn:\n" + replies.describe()
                )
                return manager.press_key_chord_on_agent(AgentId(agent.id), TAP_CHORD)

            recoveries: list[str] = []

            def send_recovery(text: str) -> bool:
                recoveries.append(text)
                return manager.send_message_to_agent(AgentId(agent.id), text) is None

            outcome = ClaudeAtomicShoulderTap.build(agent).tap(
                watcher,
                press_chord=press_once_the_queued_message_joined_the_turn,
                send_recovery=send_recovery,
            )

            assert outcome.status == "tapped", outcome
            assert recoveries == [RECOVERY_MESSAGE]
            wait_for(
                lambda: replies.was_sent(RECOVERY_MESSAGE, queued_message),
                timeout=_WAIT_SECONDS,
                error_message="the recovery nudge never reached Claude with the queued message in view:\n"
                + replies.describe(),
            )
    finally:
        is_second_read_released.set()
        is_test_over.set()


@pytest.mark.timeout(900, func_only=False)
def test_a_tap_whose_flushed_turn_answers_slowly_sends_no_recovery_nudge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chord cancels the running turn and Claude starts the queued message's own turn, which is slow to answer.

    The flushed turn's first reply arrives only after the tap's watch has ended, so the tap must tell that a turn
    started from what the chord wrote, not from an answer, and must not nudge a turn that is already under way.
    """
    first_message = f"tap-probe-first-{uuid4().hex}"
    queued_message = f"tap-probe-queued-{uuid4().hex}"
    is_flushed_turn_in_flight = threading.Event()
    is_tap_over = threading.Event()
    is_test_over = threading.Event()

    def script(body: dict[str, Any]) -> str | StandInToolUse | None:
        last_user_message = _last_user_message_text(body)
        if queued_message in last_user_message:
            is_flushed_turn_in_flight.set()
            is_tap_over.wait(_HOLD_SECONDS)
            return f"tap-probe-answered-{uuid4().hex}"
        if first_message in last_user_message:
            # The turn the chord cancels: held open so the queued message parks behind it.
            is_test_over.wait(_HOLD_SECONDS)
            return None
        return None

    replies = _ScriptedReplies(script)
    try:
        with _real_claude_chat(tmp_path, tmp_path / "project", monkeypatch, replies) as (agent, manager, watcher):
            _send(manager, agent, first_message)
            _send(manager, agent, queued_message)
            _wait_until_queued(watcher, queued_message)

            recoveries: list[str] = []
            outcome = ClaudeAtomicShoulderTap.build(agent).tap(
                watcher,
                press_chord=lambda: manager.press_key_chord_on_agent(AgentId(agent.id), TAP_CHORD),
                send_recovery=lambda text: recoveries.append(text) or True,
            )
            is_tap_over.set()

            assert outcome.status == "tapped", outcome
            assert recoveries == []
            wait_for(
                is_flushed_turn_in_flight.is_set,
                timeout=_WAIT_SECONDS,
                error_message="the chord did not start the queued message's own turn",
            )
    finally:
        is_tap_over.set()
        is_test_over.set()
