"""End-to-end tests for the compaction UX in the chat page: the Auto-compact toggle, the COMPACTING status, and the
"Context was compacted" pill.

The chat app is served over fakes (``running_workspace``) and its page is opened at the chat app's own URL, so the
chat root's rail and the chat's page are both in reach without the shell. The compaction signals are the files and
transcript records a real agent produces. The manager is never started in these tests, so the poller that watches the
``compacting`` marker is started here on its own.
"""

from __future__ import annotations

import json
import time
import urllib.request
from collections.abc import Generator
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import FrameLocator
from playwright.sync_api import Locator
from playwright.sync_api import Page
from playwright.sync_api import expect

from imbue.chat.activity_state import CompactionCause
from imbue.chat.auto_open import chat_root_path
from imbue.chat.chat_autocompact import AUTOCOMPACT_FILENAME
from imbue.chat.compaction_status import COMPACTION_REQUEST_FILENAME
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.primitives import ChatId
from imbue.chat.testing import FIXTURE_AGENT_ID
from imbue.chat.testing import FIXTURE_AGENT_NAME
from imbue.chat.testing import RunningWorkspace
from imbue.chat.testing import is_e2e_browser_installed
from imbue.chat.testing import running_workspace
from imbue.chat.testing import seed_agent_state
from imbue.mngr.utils.polling import wait_for
from imbue.mngr_claude.claude_config import COMPACTING_MARKER_FILENAME
from imbue.mngr_claude.claude_config import LAST_COMPACTION_FILENAME
from imbue.system_interface.testing import find_free_port


def _frontend_built() -> bool:
    return (Path(__file__).parent / "static" / "chat.html").is_file()


pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(not is_e2e_browser_installed(), reason="Playwright browsers not installed"),
    pytest.mark.skipif(
        not _frontend_built(),
        reason="The chat frontend is not built (run `npm run build` in system/); skipping e2e.",
    ),
]

_COMPACTING_LABEL = "Compacting…"
_COMPACTING_THEN_REPLYING_LABEL = "Compacting, then replying…"

# The "why?" popover's text for each cause, as ``compactionCauseText`` in ``user-message-display.ts`` words it.
_CAUSE_TEXT_BY_CAUSE: Mapping[str | None, str] = {
    "idle": "Compacted while idle to keep replies fast and cheap. Change this under Auto-compact in the model menu.",
    "manual": "Compacted because you asked (/compact).",
    "native": "Your agent triggered compaction. You can ask it about its current setting, or tell it to change it.",
    None: "Compacted to keep replies fast and cheap. Idle compaction is under Auto-compact in the model menu.",
}

_AUTOCOMPACT_NOTICE_TEXT = (
    "Idle chats now compact automatically to keep replies fast and cheap. Turn this off per chat, or for new chats, "
    "under Auto-compact in the model menu."
)

# A conversation whose last turn is still running with one message queued behind it, so the chat's snapshot carries
# a queued message.
_QUEUED_SESSION_EVENTS: list[dict[str, Any]] = [
    {
        "type": "user",
        "uuid": "uuid-q-1",
        "timestamp": "2026-01-01T00:00:00Z",
        "message": {"role": "user", "content": "Kick off the big refactor"},
    },
    {
        "type": "assistant",
        "uuid": "uuid-q-2",
        "timestamp": "2026-01-01T00:00:01Z",
        "message": {
            "role": "assistant",
            "model": "claude-opus-4-6",
            "content": [{"type": "text", "text": "On it -- starting now."}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 5, "output_tokens": 4},
        },
    },
    {
        "type": "user",
        "uuid": "uuid-q-3",
        "timestamp": "2026-01-01T00:00:03Z",
        "message": {"role": "user", "content": "Now run the tests"},
    },
    {
        "type": "queue-operation",
        "operation": "enqueue",
        "timestamp": "2026-01-01T00:00:05Z",
        "sessionId": "e2e-session-001",
        "content": "actually also update the changelog",
    },
]


def _utc_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


@contextmanager
def _compaction_workspace(
    tmp_path: Path, session_events: Sequence[Mapping[str, Any]] | None = None
) -> Iterator[RunningWorkspace]:
    with running_workspace(tmp_path, find_free_port(), find_free_port(), session_events=session_events) as server:
        poller = server.chat_state.agent_manager._compacting_marker_poller
        poller.start()
        try:
            yield server
        finally:
            poller.stop()


@pytest.fixture
def compaction_server(tmp_path: Path) -> Generator[RunningWorkspace, None, None]:
    with _compaction_workspace(tmp_path) as server:
        yield server


def _get_json(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read())


def _settings(server: RunningWorkspace) -> dict[str, Any]:
    return dict(_get_json(f"{server.chat_url}/api/settings")["settings"])


def _update_settings(server: RunningWorkspace, **changes: Any) -> None:
    body = json.dumps({**_settings(server), **changes}).encode()
    request = urllib.request.Request(
        f"{server.chat_url}/api/settings", data=body, headers={"Content-Type": "application/json"}, method="PUT"
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.status == 200


def _set_fixture_harness(server: RunningWorkspace, harness: HarnessType) -> None:
    manager = server.chat_state.agent_manager
    seed_agent_state(
        manager, FIXTURE_AGENT_ID, name=FIXTURE_AGENT_NAME, labels={"account": server.account_ids[0]}, harness=harness
    )
    # Rebuilds the agent's activity tracker and session for the new harness.
    manager._ensure_activity_tracking(FIXTURE_AGENT_ID)


def _open_chat(page: Page, server: RunningWorkspace) -> FrameLocator:
    page.goto(f"{server.chat_url}{chat_root_path(ChatId(FIXTURE_AGENT_ID))}")
    expect(page.locator(".chat-root")).to_be_visible(timeout=15000)
    chat = page.frame_locator(f'iframe.chat-root-frame[data-chat-id="{FIXTURE_AGENT_ID}"]')
    expect(chat.locator(".message-input-textbox")).to_be_visible(timeout=15000)
    return chat


def _rail_row(page: Page) -> Locator:
    return page.locator(f'.chat-rail-row[data-chat-id="{FIXTURE_AGENT_ID}"]')


def _strip(chat: FrameLocator) -> Locator:
    return chat.locator('.agent-activity-indicator[data-state="COMPACTING"]')


def _placeholder(chat: FrameLocator) -> Locator:
    return chat.locator(".compaction-placeholder")


def _stop_button(chat: FrameLocator) -> Locator:
    return chat.locator(".message-input-stop-button")


def _write_compacting_marker(server: RunningWorkspace) -> Path:
    """Write the ``compacting`` marker mngr's ``PreCompact`` hook writes for a compaction the user started."""
    marker = server.agent_info.agent_state_dir / COMPACTING_MARKER_FILENAME
    marker.write_text(json.dumps({"trigger": "manual", "started_at": _utc_iso(datetime.now(timezone.utc))}))
    return marker


def _append_compact_summary(server: RunningWorkspace, uuid: str) -> None:
    """Append the record Claude Code writes when a compaction finishes, which the parser turns into the pill."""
    record = {
        "type": "user",
        "uuid": uuid,
        "timestamp": _utc_iso(datetime.now(timezone.utc)),
        "isCompactSummary": True,
        "message": {
            "role": "user",
            "content": "This session is being continued from a previous conversation. Summary: the user said hello.",
        },
    }
    with open(server.session_file, "a") as handle:
        handle.write(json.dumps(record) + "\n")


def _pill(chat: FrameLocator) -> Locator:
    return chat.locator(".message-system-status-container", has_text="Context was compacted")


def _open_autocompact_submenu(chat: FrameLocator) -> Locator:
    chat.locator(".model-selector-trigger").click()
    row = chat.locator('[data-menu-row="autocompact"]')
    expect(row).to_contain_text("Auto-compact")
    row.click()
    submenu = chat.locator('[data-menu-part="submenu"]')
    expect(submenu.locator(".autocompact-options")).to_be_visible()
    return submenu


@pytest.mark.timeout(60, func_only=False)
def test_turning_auto_compact_off_writes_the_chats_setting_and_the_switch_makes_off_the_default(
    compaction_server: RunningWorkspace, page: Page
) -> None:
    server = compaction_server
    autocompact_path = server.chat_state.agent_manager._chat_files_root / FIXTURE_AGENT_ID / AUTOCOMPACT_FILENAME
    assert not autocompact_path.exists()
    assert _settings(server)["autocompact_default"] is True
    chat = _open_chat(page, server)

    submenu = _open_autocompact_submenu(chat)
    expect(submenu.locator('[data-autocompact="on"]')).to_have_attribute("aria-checked", "true")
    submenu.locator('[data-autocompact="off"]').click()

    wait_for(
        lambda: autocompact_path.is_file() and json.loads(autocompact_path.read_text()) == {"is_enabled": False},
        timeout=10.0,
        error_message="the chat's autocompact.json never recorded Off",
    )
    expect(submenu.locator('[data-autocompact="off"]')).to_have_attribute("aria-checked", "true")
    default_switch = submenu.locator('[data-autocompact-default="off"]')
    expect(default_switch).to_have_attribute("aria-label", "Use Off for new chats")
    expect(default_switch).to_have_attribute("aria-checked", "false")
    assert _settings(server)["autocompact_default"] is True

    default_switch.click()
    wait_for(
        lambda: _settings(server)["autocompact_default"] is False,
        timeout=10.0,
        error_message="the workspace's autocompact_default never turned off",
    )
    expect(default_switch).to_have_attribute("aria-checked", "true")
    assert json.loads(autocompact_path.read_text()) == {"is_enabled": False}


@pytest.mark.timeout(60, func_only=False)
def test_a_harness_that_cannot_be_compacted_has_no_auto_compact_row(
    compaction_server: RunningWorkspace, page: Page
) -> None:
    _set_fixture_harness(compaction_server, HarnessType.ANTIGRAVITY)
    with page.expect_response(lambda response: response.url.endswith("/api/harnesses")) as catalogs:
        chat = _open_chat(page, compaction_server)
    assert catalogs.value.ok

    chat.locator(".model-selector-trigger").click()
    expect(chat.locator('[data-menu-row="providers"]')).to_be_visible()
    expect(chat.locator('[data-menu-row="autocompact"]')).to_have_count(0)


@pytest.mark.parametrize(
    ("presentation", "is_strip_shown", "is_placeholder_shown"),
    [("both", True, True), ("strip", True, False), ("placeholder", False, True)],
)
@pytest.mark.timeout(60, func_only=False)
def test_a_compacting_marker_shows_the_status_where_the_presentation_puts_it_until_it_goes(
    compaction_server: RunningWorkspace,
    page: Page,
    presentation: str,
    is_strip_shown: bool,
    is_placeholder_shown: bool,
) -> None:
    """The stop button stays because Claude's interrupt cancels a compaction."""
    server = compaction_server
    _update_settings(server, compaction_status_presentation=presentation)
    chat = _open_chat(page, server)
    expect(_rail_row(page)).not_to_have_attribute("data-status", "working")
    expect(_stop_button(chat)).to_have_count(0)

    marker = _write_compacting_marker(server)

    expect(_rail_row(page)).to_have_attribute("data-status", "working", timeout=15000)
    expect(_stop_button(chat)).to_be_visible()
    if is_strip_shown:
        expect(_strip(chat).locator(".agent-activity-indicator__label")).to_have_text(_COMPACTING_LABEL)
    else:
        expect(_strip(chat)).to_have_count(0)
    if is_placeholder_shown:
        expect(_placeholder(chat).locator(".compaction-placeholder__label")).to_have_text(_COMPACTING_LABEL)
    else:
        expect(_placeholder(chat)).to_have_count(0)

    marker.unlink()

    expect(_strip(chat)).to_have_count(0, timeout=15000)
    expect(_placeholder(chat)).to_have_count(0)
    expect(_stop_button(chat)).to_have_count(0)
    expect(_rail_row(page)).not_to_have_attribute("data-status", "working")


@pytest.mark.timeout(60, func_only=False)
def test_a_message_queued_behind_a_compaction_changes_the_label(tmp_path: Path, page: Page) -> None:
    with _compaction_workspace(tmp_path, session_events=_QUEUED_SESSION_EVENTS) as server:
        _write_compacting_marker(server)
        chat = _open_chat(page, server)

        expect(chat.locator(".queued-group")).to_be_visible(timeout=15000)
        expect(_strip(chat).locator(".agent-activity-indicator__label")).to_have_text(
            _COMPACTING_THEN_REPLYING_LABEL, timeout=15000
        )
        expect(_placeholder(chat).locator(".compaction-placeholder__label")).to_have_text(
            _COMPACTING_THEN_REPLYING_LABEL
        )


@pytest.mark.timeout(60, func_only=False)
def test_the_stop_button_is_hidden_while_a_harness_that_cannot_interrupt_a_compaction_compacts(
    compaction_server: RunningWorkspace, page: Page
) -> None:
    """Codex writes no ``compacting`` marker, so its compaction comes from the chat's own request."""
    server = compaction_server
    _set_fixture_harness(server, HarnessType.CODEX)
    chat = _open_chat(page, server)

    server.chat_state.agent_manager.note_compaction_requested(
        FIXTURE_AGENT_ID, CompactionCause.MANUAL, time.monotonic()
    )

    expect(_strip(chat).locator(".agent-activity-indicator__label")).to_have_text(_COMPACTING_LABEL, timeout=15000)
    expect(_rail_row(page)).to_have_attribute("data-status", "working")
    expect(_stop_button(chat)).to_have_count(0)


def _record_compaction_cause(server: RunningWorkspace, cause: str | None) -> None:
    """Leave the record each cause is read from: the chat's own request for an idle compaction, mngr's
    ``last_compaction.json`` (manual, or Claude Code's own ``auto``) for the others, and nothing for an unknown one."""
    state_dir = server.agent_info.agent_state_dir
    now = datetime.now(timezone.utc)
    if cause == "idle":
        (state_dir / COMPACTION_REQUEST_FILENAME).write_text(
            json.dumps({"cause": "idle", "requested_at": _utc_iso(now - timedelta(seconds=5))})
        )
    elif cause in ("manual", "native"):
        trigger = "manual" if cause == "manual" else "auto"
        (state_dir / LAST_COMPACTION_FILENAME).write_text(json.dumps({"trigger": trigger, "ended_at": _utc_iso(now)}))
    else:
        assert cause is None


@pytest.mark.parametrize("cause", ["idle", "manual", "native", None])
@pytest.mark.timeout(60, func_only=False)
def test_the_compacted_pill_says_why_the_context_was_compacted(
    compaction_server: RunningWorkspace, page: Page, cause: str | None
) -> None:
    server = compaction_server
    chat = _open_chat(page, server)
    expect(chat.locator(".message-user").first).to_contain_text("Hello agent!")

    _record_compaction_cause(server, cause)
    _append_compact_summary(server, "uuid-compact-1")

    pill = _pill(chat)
    expect(pill).to_be_visible(timeout=15000)
    expect(pill.locator(".compaction-why-popover")).to_have_count(0)
    pill.locator(".compaction-why-button").click()
    expect(pill.locator(".compaction-why-popover")).to_have_text(_CAUSE_TEXT_BY_CAUSE[cause])


@pytest.mark.timeout(60, func_only=False)
def test_the_auto_compact_notice_shows_under_the_pill_until_it_is_dismissed(
    compaction_server: RunningWorkspace, page: Page
) -> None:
    server = compaction_server
    assert _settings(server)["is_autocompact_notice_shown"] is False
    chat = _open_chat(page, server)

    _record_compaction_cause(server, "idle")
    _append_compact_summary(server, "uuid-compact-1")

    notice = _pill(chat).locator("xpath=..").locator(".autocompact-notice")
    expect(notice).to_have_text(_AUTOCOMPACT_NOTICE_TEXT, timeout=15000)
    notice.locator(".autocompact-notice-dismiss").click()
    expect(chat.locator(".autocompact-notice")).to_have_count(0)
    wait_for(
        lambda: _settings(server)["is_autocompact_notice_shown"] is True,
        timeout=10.0,
        error_message="the dismissal never reached the workspace's settings",
    )

    page.reload()
    chat = page.frame_locator(f'iframe.chat-root-frame[data-chat-id="{FIXTURE_AGENT_ID}"]')
    expect(_pill(chat)).to_be_visible(timeout=15000)
    expect(chat.locator(".autocompact-notice")).to_have_count(0)
