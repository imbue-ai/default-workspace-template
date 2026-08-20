"""Acceptance tests for the transcript's scroll geometry.

These exist because this bug has been fixed five times and re-broken four. Each
previous attempt removed a real mechanism and introduced another, and the reason
it kept happening is that nothing measured the thing that actually matters:
whether the content in front of the reader stays put.

Two habits from that history are baked in here:

**Position is measured content-relatively, not as ``scrollTop``.** A jump can
move the reader while ``scrollTop`` is unchanged -- the content moves underneath
it instead -- and an earlier investigation found that four of six real jumps had
a ``scrollTop`` delta of exactly zero. So every assertion below anchors on "which
row is at the top of the viewport, and how far into it", which is what the reader
actually perceives.

**The transcript is tool-heavy.** Synthetic prose fixtures render at roughly the
per-event height the old code assumed, which masks the bug entirely: the whole
failure is that a turn with tool calls collapses into one row far shorter than
the space reserved for its events. A fixture without tool calls would pass
against the broken code.

Marked ``acceptance`` (not ``release``, as the neighbouring ``test_e2e`` module
is) so these run on every branch in CI: their entire purpose is to stop this
class of regression reaching main again.
"""

import json
import time
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Browser
from playwright.sync_api import Page

from imbue.system_interface.test_e2e import _frontend_built
from imbue.system_interface.test_e2e import _playwright_browsers_installed
from imbue.system_interface.test_e2e import _running_e2e_server
from imbue.system_interface.test_e2e import _visible_user_messages

# Mounting the workspace re-reads the machine, which shells out to tmux; the
# resource_guards plugin fails an unmarked test that reaches it.
pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.tmux,
    pytest.mark.skipif(not _playwright_browsers_installed(), reason="Playwright browsers not installed"),
    pytest.mark.skipif(
        not _frontend_built(),
        reason=(
            "System interface frontend not built "
            "(run `cd system/apps/system_interface/frontend && npm run build`); skipping e2e."
        ),
    ),
]

_PORT = 18795

# How long the watcher needs to notice a session-file append and stream it.
_STREAM_DELIVERY_MS = 3000
# Settling time after a scroll, for measurement and any triggered fetch to land.
_SETTLE_MS = 1200
# Longest wait for measured geometry to reach the workspace: rows settle half a
# second after they stop changing and the save is coalesced over two more.
_GEOMETRY_PERSIST_TIMEOUT_S = 20

# Reading the row at the top of the viewport and how far the viewport has cut
# into it. This is the reader's actual position; scrollTop is not.
_VIEWPORT_ANCHOR_JS = """
() => {
  const scroller = document.querySelector('.app-content');
  const list = scroller && scroller.querySelector('.message-list');
  if (!list) return null;
  const viewportTop = scroller.getBoundingClientRect().top;
  for (const child of list.children) {
    if (!child.id) continue;  // spacers carry no id
    const box = child.getBoundingClientRect();
    if (box.bottom > viewportTop) {
      return { id: child.id, offset: Math.round(box.top - viewportTop) };
    }
  }
  return null;
}
"""


def _viewport_anchor(page: Page) -> dict[str, Any] | None:
    """Which row is at the top of the viewport, and how far into it we are."""
    return page.evaluate(_VIEWPORT_ANCHOR_JS)


def _assert_anchor_held(before: dict[str, Any] | None, after: dict[str, Any] | None, what: str) -> None:
    """The reader must still be looking at the same content, within a pixel or two.

    A couple of pixels of tolerance covers sub-pixel layout rounding; anything
    larger is content moving under the reader, which is the bug.
    """
    assert before is not None, f"{what}: no anchor row before"
    assert after is not None, f"{what}: no anchor row after"
    assert after["id"] == before["id"], f"{what}: reader moved from row {before['id']} to {after['id']}"
    drift = abs(after["offset"] - before["offset"])
    assert drift <= 2, f"{what}: content shifted {drift}px under the reader (row {before['id']})"


def _tool_heavy_events(pair_count: int) -> list[dict[str, Any]]:
    """A transcript whose turns carry tool calls, so they group into single rows.

    Each turn is one user message plus an assistant turn with two tool calls and
    their results. That is what collapses a many-event turn into one rendered
    row -- the mismatch the reserved-space arithmetic used to get wrong. A prose
    only fixture renders close to the old per-event assumption and would not
    exercise the bug at all.
    """
    events: list[dict[str, Any]] = []
    for i in range(pair_count):
        events.append(
            {
                "type": "user",
                "uuid": f"tool-u-{i}",
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {"role": "user", "content": f"msg-{i}"},
            }
        )
        events.append(
            {
                "type": "assistant",
                "uuid": f"tool-a-{i}",
                "timestamp": "2026-01-01T00:00:01Z",
                "message": {
                    "role": "assistant",
                    "model": "claude-opus-4-6",
                    "content": [
                        {"type": "text", "text": f"reply-{i}"},
                        {
                            "type": "tool_use",
                            "id": f"call-{i}-a",
                            "name": "Read",
                            "input": {"file_path": f"/tmp/file-{i}.py"},
                        },
                        {
                            "type": "tool_use",
                            "id": f"call-{i}-b",
                            "name": "Bash",
                            "input": {"command": f"echo {i}"},
                        },
                    ],
                    "stop_reason": "tool_use",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            }
        )
        for suffix in ("a", "b"):
            events.append(
                {
                    "type": "user",
                    "uuid": f"tool-r-{i}-{suffix}",
                    "timestamp": "2026-01-01T00:00:02Z",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": f"call-{i}-{suffix}",
                                "content": f"output {i} {suffix}",
                            }
                        ],
                    },
                }
            )
    return events


def _append_event(session_file: Path, uuid: str, text: str) -> None:
    """Append one user message to the live session file, as the agent would."""
    with open(session_file, "a") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "user",
                    "uuid": uuid,
                    "timestamp": "2026-01-01T00:05:00Z",
                    "message": {"role": "user", "content": text},
                }
            )
            + "\n"
        )


def _wait_for_transcript(page: Page) -> None:
    """Wait until the transcript is mounted and tall enough to scroll within."""
    page.wait_for_selector(".message-list", timeout=15000)
    page.wait_for_function(
        "() => { const el = document.querySelector('.app-content');"
        " return el && el.scrollHeight > el.clientHeight * 2; }",
        timeout=15000,
    )
    page.wait_for_timeout(_SETTLE_MS)


@pytest.mark.timeout(180, func_only=False)
def test_reading_history_does_not_move_the_viewport(tmp_path: Path, page: Page) -> None:
    """Paging older history in must not move the content the reader is looking at.

    This is the headline bug. Reserved space for unloaded history used to be
    sized per event while the renderer groups a turn into one row, so a landing
    page was thousands of pixels shorter than its reservation and the viewport
    teleported.

    The assertion is content-relative on purpose: the failure routinely leaves
    scrollTop untouched and moves everything else instead.
    """
    events = _tool_heavy_events(120)
    with _running_e2e_server(tmp_path, _PORT, session_events=events) as (base_url, _, _):
        page.goto(base_url)
        _wait_for_transcript(page)

        # Scroll up toward the top of the loaded window, which is what asks for
        # an older page. Repeated in steps so a page lands mid-read, exactly as
        # it does for a user scrolling back through a conversation.
        for _ in range(6):
            page.evaluate("() => { document.querySelector('.app-content').scrollBy(0, -600); }")
            page.wait_for_timeout(_SETTLE_MS)
            # The scroll itself moves the reader deliberately, so there is nothing
            # to hold across it; what must not happen is the anchor row vanishing
            # from the rendered set, which is what a window reset looks like.
            assert _viewport_anchor(page) is not None, "transcript stopped rendering rows while scrolling up"

        # Now hold still and let any in-flight page land. Nothing the user did
        # should move them; if a backfill lands, its geometry was already
        # reserved, so the anchor must be identical.
        settled_anchor = _viewport_anchor(page)
        page.wait_for_timeout(_STREAM_DELIVERY_MS)
        _assert_anchor_held(settled_anchor, _viewport_anchor(page), "a page landing while the reader was still")

        # And the reader is genuinely in history, not parked at either end.
        messages = _visible_user_messages(page)
        assert messages, "expected rendered user messages"
        assert "msg-0" not in messages, f"reader was dragged to the start of the conversation: {messages[:3]}"


@pytest.mark.timeout(180, func_only=False)
def test_tail_follow_attaches_and_detaches(tmp_path: Path, page: Page) -> None:
    """New messages follow at the tail; a scroll up detaches and stays detached.

    The specific regression that ended the previous attempt at this bug: that
    branch fixed the jump and broke following, and it was closed with the note
    that it "doesn't follow new messages". Both halves are asserted here.
    """
    events = _tool_heavy_events(60)
    with _running_e2e_server(tmp_path, _PORT + 1, session_events=events) as (base_url, _, session_file):
        page.goto(base_url)
        _wait_for_transcript(page)

        # At the tail, a new message must scroll itself into view.
        _append_event(session_file, "tail-follow-1", "followed-message")
        page.wait_for_timeout(_STREAM_DELIVERY_MS)
        assert "followed-message" in _visible_user_messages(page), "a new message did not follow at the tail"
        at_bottom = page.evaluate(
            "() => { const el = document.querySelector('.app-content');"
            " return el.scrollHeight - el.scrollTop - el.clientHeight < 40; }"
        )
        assert at_bottom, "following the tail did not leave the viewport at the bottom"

        # A deliberate scroll up must detach following. Scroll to a fixed
        # fraction of the way up rather than a fixed pixel count: following
        # deliberately re-arms whenever the viewport is near the bottom, and with
        # geometry sized from real measurements the transcript is short enough
        # that a fixed offset can land inside that band.
        page.evaluate(
            "() => { const el = document.querySelector('.app-content');"
            " el.scrollTop = Math.max(0, el.scrollHeight * 0.4); }"
        )
        page.wait_for_timeout(_SETTLE_MS)
        assert page.evaluate(
            "() => { const el = document.querySelector('.app-content');"
            " return el.scrollHeight - el.scrollTop - el.clientHeight >= 40; }"
        ), "setup failed: scrolling up did not leave the bottom band"
        detached_anchor = _viewport_anchor(page)

        # ...and stay detached while more messages stream in, without the new
        # content dragging the reader back down to the bottom.
        _append_event(session_file, "tail-follow-2", "streamed-while-reading")
        page.wait_for_timeout(_STREAM_DELIVERY_MS)
        _assert_anchor_held(detached_anchor, _viewport_anchor(page), "a message streaming in while scrolled up")
        still_detached = page.evaluate(
            "() => { const el = document.querySelector('.app-content');"
            " return el.scrollHeight - el.scrollTop - el.clientHeight >= 40; }"
        )
        assert still_detached, "streaming yanked a scrolled-up reader back to the tail"


@pytest.mark.timeout(180, func_only=False)
def test_measured_geometry_is_kept_by_the_workspace(tmp_path: Path, page: Page, browser: Browser) -> None:
    """What one browser measures, the next one reserves its space from.

    The client's half of this is deliberately silent -- every failure path
    degrades to "nothing measured yet" -- so a mismatched request shape or a
    rejected width bucket would look exactly like a cold cache. Only driving
    both ends together says whether the measurements make the trip.

    The second browser context is the point: a fresh profile has an empty
    IndexedDB, so it cannot have measured this conversation itself, and the only
    way its geometry request comes back with rows is if the first browser's
    measurements reached the workspace.
    """
    # Unlike the tests above, this one needs a primary agent: without one there
    # is no workspace layout dir, and geometry has nowhere to be filed.
    events = _tool_heavy_events(60)
    with _running_e2e_server(tmp_path, _PORT + 3, session_events=events, primary_agent_id="primary-agent") as (
        base_url,
        _,
        _,
    ):
        page.goto(base_url)
        _wait_for_transcript(page)

        # Scroll so rows above the first screen mount and measure too.
        for _ in range(4):
            page.evaluate("() => { document.querySelector('.app-content').scrollBy(0, -700); }")
            page.wait_for_timeout(_SETTLE_MS)

        geometry_file = tmp_path / "agents" / "primary-agent" / "workspace_layout" / "transcript_geometry.json"
        deadline = time.monotonic() + _GEOMETRY_PERSIST_TIMEOUT_S
        while not geometry_file.exists() and time.monotonic() < deadline:
            page.wait_for_timeout(500)
        assert geometry_file.exists(), "the transcript's measured rows never reached the workspace"
        stored = json.loads(geometry_file.read_text())["geometry_by_agent_id"]
        assert stored, f"the geometry file names no transcript: {stored}"

        served_row_counts: list[int] = []
        cold_context = browser.new_context()
        try:
            cold_page = cold_context.new_page()
            cold_page.on(
                "response",
                lambda response: served_row_counts.append(len(response.json().get("rows", [])))
                if "/geometry?width=" in response.url
                else None,
            )
            cold_page.goto(base_url)
            cold_page.wait_for_selector(".message-list", timeout=15000)
            cold_page.wait_for_timeout(_SETTLE_MS)
        finally:
            cold_context.close()
        assert any(count > 0 for count in served_row_counts), (
            f"a browser that never rendered this conversation got no stored geometry: {served_row_counts}"
        )


@pytest.mark.timeout(180, func_only=False)
def test_selection_survives_streaming_and_scrolling(tmp_path: Path, page: Page) -> None:
    """A text selection must survive events streaming in and the viewport moving.

    Rows holding a selection are pinned into the rendered set even once the
    viewport has moved far from them, because unmounting a selection endpoint
    collapses the selection. Streaming is the hard case: every event redraws.
    """
    events = _tool_heavy_events(60)
    with _running_e2e_server(tmp_path, _PORT + 2, session_events=events) as (base_url, _, session_file):
        page.goto(base_url)
        _wait_for_transcript(page)

        # Select the text of a user bubble partway up the transcript.
        page.evaluate("() => { document.querySelector('.app-content').scrollBy(0, -900); }")
        page.wait_for_timeout(_SETTLE_MS)
        selected = page.evaluate(
            """
            () => {
              const rows = Array.from(document.querySelectorAll('.message-user'));
              const target = rows[Math.floor(rows.length / 2)];
              if (!target) return null;
              const range = document.createRange();
              range.selectNodeContents(target);
              const selection = window.getSelection();
              selection.removeAllRanges();
              selection.addRange(range);
              return selection.toString().trim();
            }
            """
        )
        assert selected, "failed to make a selection to test with"

        # Stream several events, each of which redraws the whole transcript.
        for index in range(3):
            _append_event(session_file, f"selection-stream-{index}", f"streamed-{index}")
        page.wait_for_timeout(_STREAM_DELIVERY_MS)
        after_streaming = page.evaluate("() => (window.getSelection().toString() || '').trim()")
        assert after_streaming == selected, f"streaming collapsed the selection ({after_streaming!r})"

        # Scroll well away from the selected rows; they must stay mounted.
        page.evaluate("() => { document.querySelector('.app-content').scrollBy(0, 2400); }")
        page.wait_for_timeout(_SETTLE_MS)
        after_scrolling = page.evaluate("() => (window.getSelection().toString() || '').trim()")
        assert after_scrolling == selected, f"scrolling away collapsed the selection ({after_scrolling!r})"
