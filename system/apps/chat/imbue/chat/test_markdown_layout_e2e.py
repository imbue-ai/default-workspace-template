"""End-to-end tests that every markdown element a message can carry stays inside the bubble or column it is drawn in.

A message is rendered from markdown on several surfaces: the agent's prose bubbles, the narration of a step that is
running, a step's work once opened, and a person's bubble. Whatever a message holds -- a wide table, a long line of
code, a URL or key with nowhere to break, a large image, raw HTML of a fixed width -- each of its pieces has to stay
inside the box it is drawn in, at a phone's width as on a desktop, or the transcript itself scrolls sideways under the
reader's finger. Wide pieces scroll inside a box of their own instead (a table, a block of code).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from typing import Final

import pytest
from playwright.sync_api import Frame
from playwright.sync_api import FrameLocator
from playwright.sync_api import Page
from playwright.sync_api import ViewportSize
from playwright.sync_api import expect

from imbue.chat.auto_open import chat_root_path
from imbue.chat.primitives import ChatId
from imbue.chat.testing import FIXTURE_AGENT_ID
from imbue.chat.testing import assistant_record
from imbue.chat.testing import bash_call_record
from imbue.chat.testing import is_chat_frontend_built
from imbue.chat.testing import is_e2e_browser_installed
from imbue.chat.testing import running_workspace
from imbue.chat.testing import tool_call_record
from imbue.chat.testing import tool_result_record
from imbue.chat.testing import user_record
from imbue.system_interface.testing import find_free_port

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(not is_e2e_browser_installed(), reason="Playwright browsers not installed"),
    pytest.mark.skipif(
        not is_chat_frontend_built(),
        reason="The chat frontend is not built (run `npm run build` in system/); skipping e2e.",
    ),
]

_PHONE_VIEWPORT: Final[ViewportSize] = {"width": 393, "height": 852}
_DESKTOP_VIEWPORT: Final[ViewportSize] = {"width": 1400, "height": 1000}

_LONG_URL: Final[str] = "https://example.com/" + "/".join(f"segment-{i:02d}-of-a-deeply-nested-path" for i in range(8))
_LONG_TOKEN: Final[str] = "sk-ant-api03-" + "Qx7v" * 30
_LONG_CODE_LINE: Final[str] = "result = compute(" + ", ".join(f"argument_{i}=value_{i}" for i in range(20)) + ")"

_STEP_ID: Final[str] = "ws-step-md"
_STEP_TITLE: Final[str] = "Lay out the trip options"
_TRIPS_READ: Final[dict[str, Any]] = {"file_path": "/tmp/trips.md"}


def _kitchen_sink(image_path: Path) -> str:
    """One message carrying every markdown element, each in the form most likely to be wider than a phone."""
    return f"""# A heading with an unbroken word: {_LONG_TOKEN}

## Your trips

A paragraph with **bold**, *italic*, `inline code`, a [link whose text is long]({_LONG_URL}), a bare URL
{_LONG_URL} and a key {_LONG_TOKEN} in it.

Inline code with nowhere to break: `{_LONG_TOKEN}`

> A quote holding a long key {_LONG_TOKEN}
>
> ```
> {_LONG_CODE_LINE}
> ```

- one
  - two
    - three
      - four, holding {_LONG_URL}

1. first
2. second, holding `{_LONG_TOKEN}`

- [ ] a task
- [x] a finished task

```python
{_LONG_CODE_LINE}
```

| | What | When | Paid | Can you change or cancel? |
|---|---|---|---|---|
| Train | Example Rail Northtown -> Southport (ZZ1234) | Mon Mar 2, 1:05pm | $28 | No changes. 30% fee to cancel. |
| Flight 1 | Example Air AAA -> BBB -> CCC, Basic (ZZ5678) | Tue Mar 3, 5:55pm -> 10:15pm | $224.20 | Cancel up to 10 minutes before departure for a travel credit, good until about Sep 3, 2027. |
| Link | {_LONG_URL} | `{_LONG_TOKEN}` | $0 | See the link. |

| Key | Value |
|---|---|
| a | b |

---

![a wide picture]({image_path})

<video width="1200" height="200"></video>

<audio controls></audio>

<svg width="1200" height="40"><rect width="1200" height="40"></rect></svg>
"""


def _reply_session(kitchen_sink: str) -> list[dict[str, Any]]:
    """The kitchen sink as a reply, then a person's message holding a URL and a key, last so both are on screen."""
    return [
        user_record("uuid-md-1", "2026-01-01T00:00:00Z", "What trips do I have booked?"),
        assistant_record("uuid-md-2", "2026-01-01T00:00:01Z", [{"type": "text", "text": kitchen_sink}], "end_turn"),
        user_record("uuid-md-3", "2026-01-01T00:00:02Z", f"Here is the link {_LONG_URL} and the key {_LONG_TOKEN}"),
    ]


def _running_step_session(kitchen_sink: str) -> list[dict[str, Any]]:
    """A turn still running inside a step, whose latest words are the kitchen sink and whose next call has not come
    back, so the step is the live one and the kitchen sink its narration."""
    return [
        user_record("uuid-md-1", "2026-01-01T00:00:00Z", "Go through my trips one at a time"),
        bash_call_record("uuid-md-2", "2026-01-01T00:00:01Z", "toolu_md1", f"tk start {_STEP_ID}"),
        tool_result_record(
            "uuid-md-3",
            "2026-01-01T00:00:02Z",
            "toolu_md1",
            f"Updated {_STEP_ID} -> in_progress\ntk-step {_STEP_ID} title: {_STEP_TITLE}",
        ),
        tool_call_record("uuid-md-4", "2026-01-01T00:00:03Z", "toolu_md2", "Read", _TRIPS_READ),
        tool_result_record("uuid-md-5", "2026-01-01T00:00:04Z", "toolu_md2", "trips"),
        assistant_record("uuid-md-6", "2026-01-01T00:00:05Z", [{"type": "text", "text": kitchen_sink}], "tool_use"),
        tool_call_record("uuid-md-7", "2026-01-01T00:00:06Z", "toolu_md3", "Read", _TRIPS_READ),
    ]


def _write_wide_picture(directory: Path) -> Path:
    path = directory / "wide-picture.svg"
    path.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="200">'
        '<rect width="1600" height="200" fill="#4a7"/></svg>'
    )
    return path


def _open_chat(page: Page, chat_url: str, viewport: ViewportSize) -> tuple[FrameLocator, Frame]:
    page.set_viewport_size(viewport)
    page.goto(f"{chat_url}{chat_root_path(ChatId(FIXTURE_AGENT_ID))}")
    chat = page.frame_locator(f'iframe.chat-root-frame[data-chat-id="{FIXTURE_AGENT_ID}"]')
    expect(chat.locator(".message-input-textbox")).to_be_visible(timeout=15000)
    frame = next(f for f in page.frames if f.url.rstrip("/").endswith(f"/{FIXTURE_AGENT_ID}"))
    return chat, frame


# Every element and every line of text in the transcript, against the box it is drawn in: the nearest bubble, else the
# transcript's column. A piece inside a box that scrolls or clips sideways (a table's, a code block's) is that box's to
# contain, so it is passed over. Answers one line per piece that crosses its box's content edge, and one if the
# transcript itself is wider than its viewport.
_PIECES_OUTSIDE_THEIR_BOX_JS: Final[str] = """
() => {
  const TOLERANCE_PX = 1;
  const BOX_SELECTOR = "[data-machine-bubble], .message-user-bubble, .message-list";
  const clipsSideways = (el) => getComputedStyle(el).overflowX !== "visible";
  const contentEdges = (el) => {
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return {
      left: rect.left + parseFloat(style.borderLeftWidth) + parseFloat(style.paddingLeft),
      right: rect.right - parseFloat(style.borderRightWidth) - parseFloat(style.paddingRight),
    };
  };
  // The box a piece is drawn in, or null when a scroller between them contains it.
  const boxOf = (el) => {
    for (let node = el; node !== null; node = node.parentElement) {
      if (node.matches(BOX_SELECTOR)) return node;
      if (clipsSideways(node)) return null;
    }
    return null;
  };
  const describe = (el) => {
    const classes = typeof el.className === "string" && el.className ? "." + el.className.trim().split(/\\s+/).join(".") : "";
    return el.tagName.toLowerCase() + classes;
  };
  const problems = [];
  const transcript = document.querySelector(".app-content");
  if (transcript.scrollWidth > transcript.clientWidth + TOLERANCE_PX) {
    problems.push(`the transcript scrolls sideways: ${transcript.scrollWidth}px of content in ${transcript.clientWidth}px`);
  }
  const check = (label, owner, rects) => {
    const box = boxOf(owner);
    if (box === null) return;
    const edges = contentEdges(box);
    for (const rect of rects) {
      if (rect.width === 0 && rect.height === 0) continue;
      if (rect.left < edges.left - TOLERANCE_PX || rect.right > edges.right + TOLERANCE_PX) {
        problems.push(
          `${label} spans ${Math.round(rect.left)}..${Math.round(rect.right)}px, outside ${describe(box)}'s ` +
            `${Math.round(edges.left)}..${Math.round(edges.right)}px`,
        );
        return;
      }
    }
  };
  const column = document.querySelector(".message-list");
  for (const el of column.querySelectorAll("*")) {
    if (el.matches(BOX_SELECTOR)) continue;
    check(describe(el), el.parentElement, [el.getBoundingClientRect()]);
  }
  const walker = document.createTreeWalker(column, NodeFilter.SHOW_TEXT);
  for (let text = walker.nextNode(); text !== null; text = walker.nextNode()) {
    if (!text.textContent.trim()) continue;
    const range = document.createRange();
    range.selectNodeContents(text);
    check(`text "${text.textContent.trim().slice(0, 40)}" in ${describe(text.parentElement)}`, text.parentElement, Array.from(range.getClientRects()));
  }
  return problems;
}
"""

_IMAGES_SETTLED_JS: Final[str] = (
    "() => Array.from(document.querySelectorAll('.message-list img')).every((img) => img.complete)"
)


# The ordinary words of the transcript's table cells (outside a link or code, which may break anywhere) that are split
# across two lines: a column too narrow for its own words.
_WORDS_BROKEN_IN_TABLE_CELLS_JS: Final[str] = """
() => {
  const broken = [];
  for (const cell of document.querySelectorAll(".message-list td, .message-list th")) {
    const walker = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT);
    for (let text = walker.nextNode(); text !== null; text = walker.nextNode()) {
      if (text.parentElement.closest("a, code")) continue;
      for (const word of text.textContent.matchAll(/[A-Za-z]+/g)) {
        const range = document.createRange();
        range.setStart(text, word.index);
        range.setEnd(text, word.index + word[0].length);
        if (range.getClientRects().length > 1) broken.push(word[0]);
      }
    }
  }
  return broken;
}
"""

# The live narration's table cells whose text fill is transparent, which leaves their text to the narration's clipped
# gradient: it never reaches inside the table's scroller in WebKit, so the text there draws as nothing on an iPhone.
_UNPAINTED_NARRATION_TABLE_CELLS_JS: Final[str] = """
() => Array.from(document.querySelectorAll(".pv-tl-narration td, .pv-tl-narration th"))
  .filter((cell) => cell.textContent.trim() && getComputedStyle(cell).webkitTextFillColor === "rgba(0, 0, 0, 0)")
  .map((cell) => cell.textContent.trim().slice(0, 40))
"""

_VIEWPORTS: Final[list[Any]] = [
    pytest.param(_PHONE_VIEWPORT, id="phone"),
    pytest.param(_DESKTOP_VIEWPORT, id="desktop"),
]


def _assert_every_piece_is_inside_its_box(frame: Frame, where: str) -> None:
    frame.wait_for_function(_IMAGES_SETTLED_JS, timeout=15000)
    problems = frame.evaluate(_PIECES_OUTSIDE_THEIR_BOX_JS)
    assert problems == [], f"{where}:\n" + "\n".join(problems)
    broken_words = frame.evaluate(_WORDS_BROKEN_IN_TABLE_CELLS_JS)
    assert broken_words == [], f"{where}: words broken across lines in a table cell: {broken_words}"


@pytest.mark.timeout(60, func_only=False)
@pytest.mark.parametrize("viewport", _VIEWPORTS)
def test_a_reply_and_a_persons_message_keep_every_markdown_element_inside_their_bubbles(
    tmp_path: Path, page: Page, viewport: ViewportSize
) -> None:
    """The agent's reply holds the kitchen sink, and a person's bubble a URL and a key, without any piece of either
    crossing its bubble and without the transcript scrolling sideways; on a phone the wide table scrolls in its own
    box instead."""
    session = _reply_session(_kitchen_sink(_write_wide_picture(tmp_path)))
    with running_workspace(tmp_path, find_free_port(), find_free_port(), session_events=session) as server:
        chat, frame = _open_chat(page, server.chat_url, viewport)
        expect(chat.locator(".message-user-bubble").last).to_contain_text(_LONG_TOKEN, timeout=15000)
        page.screenshot(path=str(tmp_path / "markdown-reply-end.png"))
        _assert_every_piece_is_inside_its_box(frame, "the reply's end and the person's message")

        tables = chat.locator("[data-machine-bubble] table")
        expect(tables).to_have_count(2)
        tables.first.evaluate("table => table.scrollIntoView({ block: 'start' })")
        page.screenshot(path=str(tmp_path / "markdown-reply-table.png"))
        _assert_every_piece_is_inside_its_box(frame, "the reply's table")
        expect(chat.locator("[data-machine-bubble] .markdown-table-scroll table")).to_have_count(2)

        wide_table_box = chat.locator(".markdown-table-scroll").first
        is_scrolling = wide_table_box.evaluate("box => box.scrollWidth > box.clientWidth")
        assert is_scrolling == (viewport == _PHONE_VIEWPORT), "the wide table scrolls on a phone and fits a desktop"
        narrow_table_box = chat.locator(".markdown-table-scroll").last
        assert narrow_table_box.evaluate("box => box.querySelector('table').offsetWidth === box.clientWidth"), (
            "a table narrower than its bubble does not fill it"
        )


@pytest.mark.timeout(60, func_only=False)
@pytest.mark.parametrize("viewport", _VIEWPORTS)
def test_a_running_steps_narration_and_opened_work_keep_every_markdown_element_inside_the_column(
    tmp_path: Path, page: Page, viewport: ViewportSize
) -> None:
    """The live step's narration holds the kitchen sink, and so does its work once opened, without any piece of it
    crossing the transcript's column and without the transcript scrolling sideways. The narration's tables paint
    their own text, since its shimmer's clipped gradient does not reach into them."""
    session = _running_step_session(_kitchen_sink(_write_wide_picture(tmp_path)))
    with running_workspace(tmp_path, find_free_port(), find_free_port(), session_events=session) as server:
        chat, frame = _open_chat(page, server.chat_url, viewport)
        expect(chat.locator(".pv-tl-narration .markdown-table-scroll table")).to_have_count(2, timeout=15000)
        page.screenshot(path=str(tmp_path / "markdown-narration.png"))
        _assert_every_piece_is_inside_its_box(frame, "the step's narration")
        unpainted_cells = frame.evaluate(_UNPAINTED_NARRATION_TABLE_CELLS_JS)
        assert unpainted_cells == [], f"the narration's table cells with no paint of their own: {unpainted_cells}"

        chat.get_by_text(_STEP_TITLE, exact=True).click()
        expect(chat.locator(".pv-expanded .markdown-table-scroll table")).to_have_count(2, timeout=15000)
        page.screenshot(path=str(tmp_path / "markdown-step-open.png"))
        _assert_every_piece_is_inside_its_box(frame, "the step's opened work")
