"""A markdown file's view page, driven in a real browser over the Files server on a temporary folder (see conftest.py):
Suggest mode, comments, accepting suggestions, an agent's rewrite while the page is open, Read mode, and the bar in a
narrow window. Every change goes through the page's own controls and is read back off the disk."""

import time
from collections.abc import Callable
from pathlib import Path

import pytest
from playwright.sync_api import Page
from playwright.sync_api import expect

pytestmark = [pytest.mark.browser, pytest.mark.timeout(90)]

OpenFilesPage = Callable[..., Page]

PLAN = """# Plan

The agent wrote this paragraph and wrapped it
at a fixed width, like agents do.

An agent wrote {++this added clause, ++}and {--this removed clause --}and a {~~typo~>fix~~}.

- [ ] a task item
"""


def _wait_for_file(page: Page, path: Path, condition: Callable[[str], bool], timeout_seconds: float = 8.0) -> str:
    """The file's text once ``condition`` holds for it, failing the test when it never does."""
    deadline = time.monotonic() + timeout_seconds
    text = path.read_text()
    while not condition(text) and time.monotonic() < deadline:
        page.wait_for_timeout(200)
        text = path.read_text()
    assert condition(text), text
    return text


def test_a_plan_is_reviewed_with_suggestions_comments_and_accept_all(
    open_files_page: OpenFilesPage, files_root: Path
) -> None:
    plan = files_root / "plan.md"
    plan.write_text(PLAN)
    page = open_files_page("/plan.md?view")
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    expect(page.locator(".tiptap h1")).to_have_text("Plan")

    # Opening changes nothing, and Suggest is the default; the source is one click away.
    expect(page.locator("#mode-suggest")).to_have_attribute("aria-checked", "true")
    expect(page.locator("#review-counts")).to_have_text("3 suggestions")
    expect(page.locator('[data-action="edit"]')).to_have_attribute("href", "/plan.md?edit")
    page.wait_for_timeout(1500)
    assert plan.read_text() == PLAN

    # Typing at the end of a wrapped paragraph is saved as an insertion of just the new words.
    page.locator(".tiptap > p").first.click()
    page.evaluate(
        "() => { const p = document.querySelector('.tiptap > p'); const r = document.createRange();"
        " r.selectNodeContents(p); r.collapse(false); const s = getSelection(); s.removeAllRanges(); s.addRange(r); }"
    )
    page.keyboard.type(" Sam agrees.")
    text = _wait_for_file(page, plan, lambda text: "like agents do.{++ Sam agrees.++}" in text)
    assert "{--The agent" not in text
    expect(page.locator("#save-status")).to_have_text("Saved")

    # A signed comment on a selected word.
    # Hovered first, so the browser's pointer glide does not split the double-click into two clicks; and given a
    # moment, as a person's hands would, for the editor to take up the selection.
    page.locator(".tiptap h1").hover()
    page.locator(".tiptap h1").dblclick(position={"x": 20, "y": 20})
    page.wait_for_timeout(300)
    page.keyboard.press("Control+Alt+KeyM")
    page.fill("#popover-field", "Rename this")
    page.fill("#popover-signature", "Sam")
    page.click("#popover-save")
    _wait_for_file(page, plan, lambda text: "# {==Plan==}{>>Sam: Rename this<<}" in text)

    # Accept every suggestion.
    page.click("#accept-all")
    text = _wait_for_file(page, plan, lambda text: "{++" not in text and "{~~" not in text)
    assert "An agent wrote this added clause, and and a fix." in text
    assert "Sam agrees." in text

    # An agent rewrites the file while the page has nothing unsaved: the page shows the new version.
    plan.write_text(plan.read_text().replace("a task item", "a task the agent renamed"))
    expect(page.locator(".tiptap")).to_contain_text("a task the agent renamed", timeout=10_000)

    # Read mode cannot change the file.
    page.click("#mode-read")
    expect(page.locator(".tiptap")).to_have_attribute("contenteditable", "false")
    assert errors == []


def test_an_edit_made_while_an_agent_rewrites_the_file_is_never_saved_over_it(
    open_files_page: OpenFilesPage, files_root: Path
) -> None:
    plan = files_root / "plan.md"
    plan.write_text("# Plan\n\nFirst draft.\n")
    page = open_files_page("/plan.md?view")
    expect(page.locator(".tiptap h1")).to_have_text("Plan")
    # Suggest mode is remembered per browser; Edit writes straight into the file, which keeps the check simple.
    page.click("#mode-edit")

    # The agent's rewrite lands between the person's keystroke and the save it schedules.
    page.locator(".tiptap > p").first.click()
    page.keyboard.press("End")
    page.keyboard.type(" Mine.")
    plan.write_text("# Plan\n\nThe agent's second draft.\n")

    expect(page.locator("#markdown-banner")).to_contain_text("changed outside the editor", timeout=10_000)
    expect(page.locator("#save-status")).to_have_text("Not saved")
    assert plan.read_text() == "# Plan\n\nThe agent's second draft.\n"

    page.click("#conflict-load")
    expect(page.locator(".tiptap")).to_contain_text("The agent's second draft.")
    assert plan.read_text() == "# Plan\n\nThe agent's second draft.\n"


def test_a_conflict_right_after_an_update_notice_stays_until_answered(
    open_files_page: OpenFilesPage, files_root: Path
) -> None:
    plan = files_root / "plan.md"
    plan.write_text("# Plan\n\nFirst draft.\n")
    page = open_files_page("/plan.md?view")
    expect(page.locator(".tiptap h1")).to_have_text("Plan")
    page.click("#mode-edit")

    # An agent rewrites the file while nothing is unsaved: the page shows it, with a notice that clears itself.
    plan.write_text("# Plan\n\nSecond draft.\n")
    expect(page.locator("#markdown-banner")).to_contain_text("just updated", timeout=10_000)

    # Before the notice clears, the person edits and the agent rewrites again: the conflict must outlive the notice.
    page.locator(".tiptap > p").first.click()
    page.keyboard.press("End")
    page.keyboard.type(" Mine.")
    plan.write_text("# Plan\n\nThird draft.\n")
    expect(page.locator("#conflict-load")).to_be_visible(timeout=10_000)
    page.wait_for_timeout(5000)
    expect(page.locator("#conflict-load")).to_be_visible()
    assert plan.read_text() == "# Plan\n\nThird draft.\n"


def test_the_bar_stays_under_the_header_in_a_narrow_window(open_files_page: OpenFilesPage, files_root: Path) -> None:
    # In a narrow window the bar wraps onto more rows; it must stay directly under the header while scrolling.
    (files_root / "long.md").write_text(
        "# Long\n\n" + "\n\n".join(f"Paragraph {number} " + "words " * 40 for number in range(40)) + "\n"
    )
    page = open_files_page("/long.md?view", width=640, height=500)
    expect(page.locator(".tiptap h1")).to_have_text("Long")
    page.mouse.move(320, 300)
    page.mouse.wheel(0, 3000)
    page.wait_for_timeout(500)
    header = page.locator("header.files-toolbar").bounding_box()
    bar = page.locator(".files-markdown-bar").bounding_box()
    assert header is not None and bar is not None
    assert abs(bar["y"] - (header["y"] + header["height"])) < 1
    # It really did wrap.
    assert bar["height"] > 40


def test_a_markdown_files_source_still_opens_in_the_text_editor(
    open_files_page: OpenFilesPage, files_root: Path
) -> None:
    (files_root / "notes.md").write_text("# Notes\n")
    page = open_files_page("/notes.md?edit")
    expect(page.locator("textarea.files-editor")).to_have_value("# Notes\n")
    expect(page.locator(".files-markdown")).to_have_count(0)
