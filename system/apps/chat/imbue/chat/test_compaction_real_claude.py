"""A real Claude chat compacted from the composer: the status while it runs, then the chips and mngr's record.

Unlike the browser suite's synthetic markers (``test_compaction_e2e.py``), the agent here is real: ``mngr create``
starts the pinned ``claude`` in an isolated host dir on a private tmux server, mngr's ``PreCompact``/``PostCompact``
hooks write the marker and ``last_compaction.json``, and the chat app sends through mngr. Only the model is a
stand-in: Claude Code is pointed at a loopback ``StandInAnthropicApi`` with a dummy key, which answers every message
request with canned text, so no credential or network is needed.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from playwright.sync_api import Page
from playwright.sync_api import expect

from imbue.chat.agent_discovery import AgentInfo
from imbue.chat.agent_manager import AgentManager
from imbue.chat.auto_open import chat_root_path
from imbue.chat.primitives import ChatId
from imbue.chat.server import create_application
from imbue.chat.testing import ServedApp
from imbue.chat.testing import build_test_state
from imbue.chat.testing import is_chat_frontend_built
from imbue.chat.testing import is_e2e_browser_installed
from imbue.chat.testing import seed_agent_state
from imbue.chat.testing import serve_app
from imbue.chat.testing import stand_in_real_claude_agent
from imbue.chat.ws_broadcaster import WebSocketBroadcaster
from imbue.mngr.utils.polling import wait_for
from imbue.mngr_claude.claude_config import COMPACTING_MARKER_FILENAME
from imbue.mngr_claude.claude_config import LAST_COMPACTION_FILENAME

pytestmark = [
    pytest.mark.real_claude,
    pytest.mark.browser,
    pytest.mark.skipif(not is_e2e_browser_installed(), reason="Playwright browsers not installed"),
    pytest.mark.skipif(
        not is_chat_frontend_built(),
        reason="The chat frontend is not built (run `npm run build` in system/); skipping e2e.",
    ),
]

_STAND_IN_API_KEY = "sk-ant-api03-stand-in-compaction"
_REPLY_TEXT = "pong"
_SUMMARY_TEXT = "Stand-in summary: the user asked for a one-word reply and got it."
# The pinned Claude Code's compaction prompt, which asks for an analysis block and then a summary block.
_COMPACTION_PROMPT = "Your task is to create a detailed summary of the conversation so far"
_TURN_TIMEOUT_MS = 120_000


def _is_compaction_request(body: dict[str, Any]) -> bool:
    messages = body.get("messages") or []
    return bool(messages) and messages[-1].get("role") == "user" and _COMPACTION_PROMPT in json.dumps(messages[-1])


class _CompactionHoldingReplies:
    def __init__(self) -> None:
        self.release = threading.Event()
        self.compaction_requested = threading.Event()

    def __call__(self, body: dict[str, Any]) -> str:
        if not _is_compaction_request(body):
            return _REPLY_TEXT
        self.compaction_requested.set()
        self.release.wait(timeout=120.0)
        return f"<analysis>Stand-in analysis.</analysis>\n<summary>{_SUMMARY_TEXT}</summary>"


@contextmanager
def _chat_app_over(agent: AgentInfo, chats_root: Path) -> Generator[ServedApp, None, None]:
    """Watches the ``compacting`` marker as ``AgentManager.start`` would."""
    manager = AgentManager.build(WebSocketBroadcaster(), chat_files_root=chats_root)
    seed_agent_state(manager, agent.id, name=agent.name, labels=agent.labels, harness=agent.harness)
    manager._ensure_activity_tracking(agent.id)
    manager.note_agent_list_known()
    state = build_test_state(agent_manager=manager)
    manager._compacting_marker_poller.start()
    try:
        with serve_app(create_application(state)) as served:
            yield served
    finally:
        state.shutdown()


@pytest.mark.timeout(900, func_only=False)
def test_a_compact_typed_in_a_real_claude_chat_shows_compacting_then_lands_the_chips(
    tmp_path: Path, page: Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    replies = _CompactionHoldingReplies()
    with stand_in_real_claude_agent(
        tmp_path / "project",
        tmp_path / "mngr.log",
        f"compaction-probe-{uuid4().hex}",
        replies,
        _STAND_IN_API_KEY,
        monkeypatch,
    ) as (api, agent):
        try:
            with _chat_app_over(agent, tmp_path / "chats") as served:
                page.goto(f"{served.http_url}{chat_root_path(ChatId(agent.id))}")
                chat = page.frame_locator(f'iframe.chat-root-frame[data-chat-id="{agent.id}"]')
                textbox = chat.locator(".message-input-textbox")
                expect(textbox).to_be_visible(timeout=30_000)
                textbox.fill("Reply with exactly the word pong and nothing else.")
                textbox.press("Enter")
                expect(chat.locator(".message-assistant", has_text=_REPLY_TEXT).first).to_be_visible(
                    timeout=_TURN_TIMEOUT_MS
                )
                expect(chat.locator(".agent-activity-indicator")).to_have_count(0, timeout=_TURN_TIMEOUT_MS)

                textbox.fill("/compact")
                textbox.press("Enter")
                strip = chat.locator('.agent-activity-indicator[data-state="COMPACTING"]')
                expect(strip.locator(".agent-activity-indicator__label")).to_have_text(
                    "Compacting as requested…", timeout=30_000
                )
                # The typed /compact is the user's bubble, with the start chip under it while the compaction runs.
                bubble = chat.locator(".message-user", has_text="/compact")
                expect(bubble).to_have_count(1)
                started = chat.locator(".tool-chip.compaction-chip--started")
                expect(started.locator(".tool-chip-label")).to_have_text("Compacting as requested…")
                expect(
                    bubble.locator("xpath=following-sibling::*[1]").locator(".compaction-chip--started")
                ).to_have_count(1)
                marker = agent.agent_state_dir / COMPACTING_MARKER_FILENAME
                wait_for(
                    lambda: replies.compaction_requested.is_set() and marker.is_file(),
                    timeout=60.0,
                    error_message="Claude Code never asked the stand-in for a summary with mngr's compacting marker up",
                )
                expect(strip).to_be_visible()
                replies.release.set()

                finished = chat.locator(".tool-chip.compaction-chip--finished")
                expect(finished.locator(".tool-chip-label")).to_have_text("Compacted", timeout=_TURN_TIMEOUT_MS)
                expect(strip).to_have_count(0, timeout=60_000)
                expect(started).to_have_count(1)
                expect(bubble).to_have_count(1)
                finished.click()
                panel = chat.locator(".tool-chip-detail.compaction-detail")
                expect(panel.locator(".compaction-explanation")).to_have_text(
                    "Compacted because you asked (/compact)."
                )
                expect(panel.locator(".compaction-summary")).to_contain_text(_SUMMARY_TEXT)

                last_compaction = agent.agent_state_dir / LAST_COMPACTION_FILENAME
                wait_for(
                    lambda: last_compaction.is_file(),
                    timeout=30.0,
                    error_message=f"mngr's PostCompact hook never wrote {last_compaction}",
                )
                assert json.loads(last_compaction.read_text())["trigger"] == "manual"
                assert not marker.exists()
        finally:
            replies.release.set()
    message_requests = [request for request in api.captured if request["path"].startswith("/v1/messages")]
    assert message_requests
    assert {request["x-api-key"] for request in message_requests} == {_STAND_IN_API_KEY}
