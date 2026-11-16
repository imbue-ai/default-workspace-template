"""The Markdown Editor, driven in a real browser over the real server on a throwaway home (see conftest.py).

Every change a test makes goes through the page's own controls and is read back off the disk.
"""

import time
from collections.abc import Callable
from pathlib import Path

import pytest
from playwright.sync_api import Page
from playwright.sync_api import expect

pytestmark = [pytest.mark.browser, pytest.mark.timeout(90)]

OpenEditorPage = Callable[..., Page]

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
    open_editor_page: OpenEditorPage, workspace: Path
) -> None:
    plan = workspace / "plan.md"
    plan.write_text(PLAN)
    page = open_editor_page(f"/?path={plan}")
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    expect(page.locator(".tiptap h1")).to_have_text("Plan")

    # Opening changes nothing, and Suggest is the default.
    expect(page.locator("#mode-suggest")).to_have_class("selected")
    expect(page.locator("#review-counts")).to_have_text("3 suggestions")
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

    # A signed comment on a selected word.
    page.locator(".tiptap h1").dblclick(position={"x": 20, "y": 20})
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


def test_the_start_page_lists_the_workspaces_markdown_and_opens_one(
    open_editor_page: OpenEditorPage, workspace: Path
) -> None:
    (workspace / "notes").mkdir()
    (workspace / "notes" / "ideas.md").write_text("# Ideas\n\nOne idea.\n")
    page = open_editor_page("/")
    found = page.locator("#found-list").get_by_role("link", name="ideas.md")
    expect(found).to_be_visible()
    found.click()
    expect(page.locator(".tiptap h1")).to_have_text("Ideas")
    assert "path=" in page.url


def test_the_toolbar_stays_under_the_top_bar_in_a_narrow_window(
    open_editor_page: OpenEditorPage, workspace: Path
) -> None:
    # In a narrow window the toolbar wraps onto two rows; it must stay directly under the top bar while scrolling.
    plan = workspace / "long.md"
    plan.write_text("# Long\n\n" + "\n\n".join(f"Paragraph {number} " + "words " * 40 for number in range(40)) + "\n")
    page = open_editor_page(f"/?path={plan}", width=640, height=500)
    expect(page.locator(".tiptap h1")).to_have_text("Long")
    page.mouse.wheel(0, 3000)
    page.wait_for_timeout(500)
    top_bar = page.locator("#topbar").bounding_box()
    toolbar = page.locator("#toolbar").bounding_box()
    assert top_bar is not None and toolbar is not None
    assert abs(top_bar["y"]) < 1
    assert abs(toolbar["y"] - (top_bar["y"] + top_bar["height"])) < 1
    # It really did wrap onto two rows.
    assert toolbar["height"] > 40
