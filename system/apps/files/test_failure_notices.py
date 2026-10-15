"""How the File Viewer tells a failed request (assets/index.js), driven in a real browser.

A refusal is answered by the `open_files_page` fixture's fetch stub (``window.fetchRefusals``) or, for an upload's
XMLHttpRequest, by routing the request; what the page shows is asserted, and that no browser alert was raised.
"""

from collections.abc import Callable
from typing import Any

import pytest
from playwright.sync_api import Dialog, Frame, Page, Route, expect

pytestmark = [pytest.mark.browser, pytest.mark.timeout(60)]

DESKTOP = (1280, 800)
WORKSPACE = "/home/user/workspace"

OpenFilesPage = Callable[[str, int, int], Page]
OpenFramedFilesPage = Callable[[str, bool], tuple[Page, Frame]]


def _record_browser_dialogs(page: Page, prompt_answer: str) -> list[Dialog]:
    """Answer the page's prompts with ``prompt_answer`` and confirms with OK, keeping every native dialog seen."""
    seen: list[Dialog] = []

    def answer(dialog: Dialog) -> None:
        seen.append(dialog)
        if dialog.type == "prompt":
            dialog.accept(prompt_answer)
        else:
            dialog.accept()

    page.on("dialog", answer)
    return seen


def _refuse(page: Page | Frame, method: str, refusal: dict[str, Any] | str) -> None:
    page.evaluate(
        "([method, refusal]) => { window.fetchRefusals = { ...(window.fetchRefusals || {}), [method]: refusal }; }",
        [method, refusal],
    )


def test_a_new_folder_that_is_already_there_is_told_in_the_pages_dialog(
    open_files_page: OpenFilesPage,
) -> None:
    page = open_files_page(f"{WORKSPACE}/", *DESKTOP)
    _refuse(page, "MKCOL", {"status": 405, "body": "Already exists"})
    dialogs = _record_browser_dialogs(page, "reports")

    page.locator(".new-folder").click()

    notice = page.locator(".notice")
    expect(notice).to_be_visible()
    expect(notice.locator(".notice-message")).to_have_text('Couldn\'t create "reports".')
    expect(notice.locator(".notice-cause")).to_have_text("Something with that name is already there.")
    notice.get_by_text("See details").click()
    expect(notice.locator(".notice-details-text")).to_have_text("HTTP 405\nAlready exists")
    # The prompt asked for the name; nothing else used a browser dialog.
    assert [dialog.type for dialog in dialogs] == ["prompt"]

    notice.get_by_role("button", name="Close").click()
    expect(notice).to_be_hidden()


def test_a_move_the_server_refuses_without_a_reason_names_the_file_and_folder_and_asks_for_a_bug_report(
    open_files_page: OpenFilesPage,
) -> None:
    page = open_files_page(f"{WORKSPACE}/", *DESKTOP)
    _refuse(page, "MOVE", {"status": 500, "body": ""})
    _record_browser_dialogs(page, f"{WORKSPACE}/data/notes.txt")

    page.locator("tr", has=page.get_by_role("link", name="notes.txt", exact=True)).get_by_title(
        "Move & Rename"
    ).click()

    notice = page.locator(".notice")
    expect(notice.locator(".notice-message")).to_have_text('Couldn\'t move "notes.txt" into "data".')
    expect(notice.locator(".notice-cause")).to_have_text("Try again. If this persists, report a bug.")
    expect(notice.locator(".notice-details-text")).to_have_text("HTTP 500")


def test_a_rename_in_place_that_fails_names_both_names(open_files_page: OpenFilesPage) -> None:
    page = open_files_page(f"{WORKSPACE}/", *DESKTOP)
    _refuse(page, "MOVE", "no answer")
    _record_browser_dialogs(page, f"{WORKSPACE}/todo.txt")

    page.locator("tr", has=page.get_by_role("link", name="notes.txt", exact=True)).get_by_title(
        "Move & Rename"
    ).click()

    notice = page.locator(".notice")
    expect(notice.locator(".notice-message")).to_have_text('Couldn\'t rename "notes.txt" to "todo.txt".')
    expect(notice.locator(".notice-details-text")).to_have_text("Failed to fetch")


def test_a_file_whose_contents_are_gone_says_so_in_the_page_and_try_again_loads_them(
    open_files_page: OpenFilesPage, dufs_origin: str
) -> None:
    page = open_files_page(f"{WORKSPACE}/", *DESKTOP)
    page.add_init_script('window.fetchRefusals = { GET: { status: 404, body: "Not Found" } };')
    dialogs = _record_browser_dialogs(page, "")
    page.goto(f"{dufs_origin}{WORKSPACE}/notes.txt?edit")

    failure = page.locator(".load-failure")
    expect(failure.locator(".load-failure-message")).to_have_text(
        "This file is no longer there. It may have been moved or deleted."
    )
    expect(page.locator(".editor")).to_be_hidden()
    failure.get_by_text("See details").click()
    expect(failure.locator(".load-failure-details-text")).to_have_text("HTTP 404\nNot Found")

    page.evaluate("delete window.fetchRefusals")
    failure.get_by_role("button", name="Try again").click()

    expect(page.locator(".editor")).to_have_value("notes\n")
    expect(failure).to_be_hidden()
    assert dialogs == []


def test_a_file_that_cannot_be_loaded_for_another_reason_asks_for_a_bug_report(
    open_files_page: OpenFilesPage, dufs_origin: str
) -> None:
    page = open_files_page(f"{WORKSPACE}/", *DESKTOP)
    page.add_init_script('window.fetchRefusals = { GET: "no answer" };')
    page.goto(f"{dufs_origin}{WORKSPACE}/notes.txt?edit")

    expect(page.locator(".load-failure-message")).to_have_text(
        "This file couldn't be opened. Try again. If this persists, report a bug."
    )


def test_a_failed_upload_says_why_in_its_row_and_a_failed_retry_says_so_again(
    open_files_page: OpenFilesPage,
) -> None:
    page = open_files_page(f"{WORKSPACE}/", *DESKTOP)
    answers: list[str] = ["refuse", "drop"]

    def answer(route: Route) -> None:
        if route.request.method != "PUT":
            route.fallback()
        elif answers.pop(0) == "refuse":
            route.fulfill(status=403, body="Forbidden")
        else:
            route.abort()

    page.route("**/upload-me.txt", answer)
    page.locator("#file").set_input_files(
        files=[{"name": "upload-me.txt", "mimeType": "text/plain", "buffer": b"upload me\n"}]
    )

    row = page.locator(".uploaders-table .uploader")
    expect(row.locator(".upload-failure")).to_have_text("Upload failed — this folder is read-only")
    expect(row.locator(".upload-failure")).to_have_attribute("title", "HTTP 403 Forbidden")

    row.get_by_title("Retry").click()
    expect(row.locator(".upload-failure")).to_have_text("Upload interrupted")
    assert answers == []

    # A retry whose own first request is not answered says so too, with the retry control still there.
    _refuse(page, "HEAD", "no answer")
    page.evaluate('document.querySelector(".upload-failure").textContent = ""')
    row.get_by_title("Retry").click()
    expect(row.locator(".upload-failure")).to_have_text("Upload interrupted")
    expect(row.get_by_title("Retry")).to_be_visible()


def test_a_file_too_deep_for_a_window_of_its_own_is_told_in_the_page_instead_of_asked_of_the_shell(
    open_files_page: OpenFilesPage, long_folder: str
) -> None:
    page = open_files_page(f"{long_folder}/", *DESKTOP)
    page.evaluate(
        """() => {
          window.opened = [];
          window.mindsShell = {
            hasShell: () => true, canReportBug: () => false, openPath: (path) => opened.push(path), location() {},
          };
        }"""
    )

    page.get_by_role("link", name="far.txt", exact=True).click()

    notice = page.locator(".notice")
    expect(notice.locator(".notice-message")).to_have_text(
        "This file's location is too long to open in a window of its own. Download it from the listing instead."
    )
    expect(notice.locator(".notice-cause")).to_be_hidden()
    expect(notice.locator(".notice-details")).to_be_hidden()
    assert page.evaluate("opened") == []


def test_a_file_in_an_ordinary_folder_is_still_asked_of_the_shell(open_files_page: OpenFilesPage) -> None:
    page = open_files_page(f"{WORKSPACE}/", *DESKTOP)
    page.evaluate(
        """() => {
          window.opened = [];
          window.mindsShell = {
            hasShell: () => true, canReportBug: () => false, openPath: (path) => opened.push(path), location() {},
          };
        }"""
    )

    page.get_by_role("link", name="notes.txt", exact=True).click()

    page.wait_for_function("opened.length === 1")
    assert page.evaluate("opened") == [f"{WORKSPACE}/notes.txt?view"]
    expect(page.locator(".notice")).to_be_hidden()


def _shell_messages(page: Page, message_type: str) -> list[dict[str, Any]]:
    return [message for message in page.evaluate("window.shellMessages") if message["type"] == message_type]


@pytest.mark.parametrize("can_report_bug", [True, False], ids=["embedder-opens-reports", "no-bug-report"])
def test_a_refusal_that_asks_for_a_bug_report_offers_one_only_where_the_shell_can_open_it(
    open_framed_files_page: OpenFramedFilesPage, can_report_bug: bool
) -> None:
    page, viewer = open_framed_files_page(f"{WORKSPACE}/", can_report_bug)
    _refuse(viewer, "MOVE", {"status": 500, "body": ""})
    _record_browser_dialogs(page, f"{WORKSPACE}/data/notes.txt")

    viewer.locator("tr", has=viewer.get_by_role("link", name="notes.txt", exact=True)).get_by_title(
        "Move & Rename"
    ).click()

    notice = viewer.locator(".notice")
    expect(notice.locator(".notice-cause")).to_have_text("Try again. If this persists, report a bug.")
    report = notice.get_by_role("button", name="Report a bug")
    if not can_report_bug:
        expect(report).to_be_hidden()
        return
    report.click()
    page.wait_for_function("window.shellMessages.some((message) => message.type === 'shell:report-bug')")
    assert len(_shell_messages(page, "shell:report-bug")) == 1


def test_a_refusal_with_a_known_cause_offers_no_bug_report(open_framed_files_page: OpenFramedFilesPage) -> None:
    page, viewer = open_framed_files_page(f"{WORKSPACE}/", True)
    _refuse(viewer, "MKCOL", {"status": 405, "body": "Already exists"})
    _record_browser_dialogs(page, "reports")

    viewer.locator(".new-folder").click()

    notice = viewer.locator(".notice")
    expect(notice.locator(".notice-cause")).to_have_text("Something with that name is already there.")
    expect(notice.get_by_role("button", name="Report a bug")).to_be_hidden()


def test_a_file_that_cannot_be_loaded_offers_report_a_bug_and_a_missing_one_does_not(
    open_framed_files_page: OpenFramedFilesPage, dufs_origin: str
) -> None:
    page, viewer = open_framed_files_page(f"{WORKSPACE}/", True)
    page.add_init_script('window.fetchRefusals = { GET: "no answer" };')
    viewer.goto(f"{dufs_origin}{WORKSPACE}/notes.txt?edit")
    viewer.wait_for_function("window.shellHandshakes > 0")

    failure = viewer.locator(".load-failure")
    expect(failure.locator(".load-failure-message")).to_have_text(
        "This file couldn't be opened. Try again. If this persists, report a bug."
    )
    failure.get_by_role("button", name="Report a bug").click()
    page.wait_for_function("window.shellMessages.some((message) => message.type === 'shell:report-bug')")

    _refuse(viewer, "GET", {"status": 404, "body": "Not Found"})
    failure.get_by_role("button", name="Try again").click()

    expect(failure.locator(".load-failure-message")).to_have_text(
        "This file is no longer there. It may have been moved or deleted."
    )
    expect(failure.get_by_role("button", name="Report a bug")).to_be_hidden()
