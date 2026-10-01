"""The file viewer's phone layout (assets/phone.js), driven in a real browser.

At a phone's width the vendored dufs frontend shows one row per entry instead of its table, and every
action on a row or in the header goes through dufs's own handlers: the requests asserted here
are the ones dufs itself makes, recorded by the stub the `open_files_page` fixture puts over
the page's fetch.
"""

from collections.abc import Callable
from typing import Any

import pytest
from playwright.sync_api import Dialog, Locator, Page, expect

pytestmark = [pytest.mark.browser, pytest.mark.timeout(60)]

PHONE = (393, 852)
DESKTOP = (1280, 800)
WORKSPACE = "/home/user/workspace"

OpenFilesPage = Callable[[str, int, int], Page]


def _answer_dialogs(page: Page, *answers: str | bool) -> list[Dialog]:
    """Answer the page's next dialogs in order: a string fills a prompt, True accepts, False dismisses."""
    seen: list[Dialog] = []
    pending = list(answers)

    def answer(dialog: Dialog) -> None:
        seen.append(dialog)
        reply = pending.pop(0)
        if reply is False:
            dialog.dismiss()
        elif reply is True:
            dialog.accept()
        else:
            dialog.accept(reply)

    page.on("dialog", answer)
    return seen


def _open_row_menu(page: Page, name: str) -> None:
    page.locator(
        ".phone-row", has=page.locator(".phone-row-name", has_text=name)
    ).locator(".phone-row-more").click()


def _menu_items(page: Page) -> Locator:
    return page.locator(".phone-menu .phone-menu-item")


def test_a_phone_width_lists_one_row_per_entry_in_place_of_the_table(
    open_files_page: OpenFilesPage, dufs_origin: str
) -> None:
    page = open_files_page(f"{WORKSPACE}/", *PHONE)

    expect(page.locator(".phone-row-name")).to_have_text(
        ["data", "README.md", "notes.txt"]
    )
    expect(page.locator(".phone-row-sub")).to_have_text(
        ["2026-09-27 09:06·3 items", "2026-10-03 04:00·1.3 KB", "2026-09-27 09:06·5 KB"]
    )
    expect(page.locator(".paths-table")).to_be_hidden()
    expect(page.locator(".head")).to_be_hidden()
    expect(page.locator(".phone-title")).to_have_text("workspace")
    expect(page.locator(".phone-crumb .breadcrumb")).to_have_text(
        "/home/user/workspace"
    )
    links = page.locator(".phone-row-link")
    expect(links.nth(0)).to_have_attribute("href", f"{dufs_origin}{WORKSPACE}/data/")
    expect(links.nth(1)).to_have_attribute(
        "href", f"{dufs_origin}{WORKSPACE}/README.md?edit"
    )


def test_the_header_kebab_holds_the_toolbox_and_shows_system_files_through_dufs(
    open_files_page: OpenFilesPage,
) -> None:
    page = open_files_page(f"{WORKSPACE}/", *PHONE)

    page.get_by_role("button", name="More", exact=True).click()
    expect(_menu_items(page)).to_have_text(
        [
            "Show system files",
            "Download folder as .zip",
            "Upload files or folders",
            "New folder",
            "New file",
        ]
    )
    _menu_items(page).filter(has_text="Show system files").click()

    expect(page.locator(".phone-menu")).to_have_count(0)
    expect(page.locator(".phone-row-name")).to_have_text(
        [".git", "data", ".env", "README.md", "notes.txt"]
    )
    page.get_by_role("button", name="More", exact=True).click()
    expect(_menu_items(page).first).to_have_text("Hide system files")

    # dufs keeps the choice, so the next page load shows them too.
    page.reload()
    expect(page.locator(".phone-row-name")).to_have_text(
        [".git", "data", ".env", "README.md", "notes.txt"]
    )


@pytest.mark.parametrize(
    ("item", "prompt", "method", "lands_on"),
    [
        ("New folder", "Enter folder name", "MKCOL", f"{WORKSPACE}/reports"),
        ("New file", "Enter file name", "PUT", f"{WORKSPACE}/reports?edit"),
    ],
)
def test_the_header_kebab_creates_through_dufs_prompt(
    open_files_page: OpenFilesPage,
    dufs_origin: str,
    fetch_calls: list[dict[str, Any]],
    item: str,
    prompt: str,
    method: str,
    lands_on: str,
) -> None:
    page = open_files_page(f"{WORKSPACE}/", *PHONE)
    dialogs = _answer_dialogs(page, "reports")

    page.get_by_role("button", name="More", exact=True).click()
    _menu_items(page).filter(has_text=item).click()

    page.wait_for_url(dufs_origin + lands_on)
    assert [dialog.message for dialog in dialogs] == [prompt]
    assert [(call["method"], call["url"]) for call in fetch_calls] == [
        (method, f"{dufs_origin}{WORKSPACE}/reports")
    ]


def test_a_rows_kebab_opens_a_sheet_of_the_tables_actions(
    open_files_page: OpenFilesPage,
) -> None:
    page = open_files_page(f"{WORKSPACE}/", *PHONE)

    _open_row_menu(page, "README.md")
    expect(page.locator(".phone-menu-sheet .phone-menu-title")).to_have_text(
        "README.md"
    )
    expect(_menu_items(page)).to_have_text(
        ["Download file", "Edit file", "Move & Rename", "Delete"]
    )

    page.locator(".phone-scrim").click(position={"x": 20, "y": 20})
    expect(page.locator(".phone-menu")).to_have_count(0)

    _open_row_menu(page, "data")
    expect(_menu_items(page)).to_have_text(
        ["Download folder as .zip", "Move & Rename", "Delete"]
    )


def test_delete_from_the_sheet_asks_dufs_confirm_and_sends_dufs_request(
    open_files_page: OpenFilesPage, dufs_origin: str, fetch_calls: list[dict[str, Any]]
) -> None:
    page = open_files_page(f"{WORKSPACE}/", *PHONE)
    dialogs = _answer_dialogs(page, False, True)

    _open_row_menu(page, "notes.txt")
    _menu_items(page).filter(has_text="Delete").click()
    expect(page.locator(".phone-row-name")).to_have_text(
        ["data", "README.md", "notes.txt"]
    )
    assert fetch_calls == []

    _open_row_menu(page, "notes.txt")
    _menu_items(page).filter(has_text="Delete").click()
    expect(page.locator(".phone-row-name")).to_have_text(["data", "README.md"])
    assert [dialog.message for dialog in dialogs] == [
        "Delete `notes.txt`?",
        "Delete `notes.txt`?",
    ]
    assert [(call["method"], call["url"]) for call in fetch_calls] == [
        ("DELETE", f"{dufs_origin}{WORKSPACE}/notes.txt")
    ]


def test_move_from_the_sheet_asks_dufs_prompt_and_sends_dufs_requests(
    open_files_page: OpenFilesPage, dufs_origin: str, fetch_calls: list[dict[str, Any]]
) -> None:
    page = open_files_page(f"{WORKSPACE}/", *PHONE)
    dialogs = _answer_dialogs(page, f"{WORKSPACE}/data/notes.txt")

    _open_row_menu(page, "notes.txt")
    _menu_items(page).filter(has_text="Move & Rename").click()

    page.wait_for_url(f"{dufs_origin}{WORKSPACE}/data")
    assert [(dialog.message, dialog.default_value) for dialog in dialogs] == [
        ("Enter new path", f"{WORKSPACE}/notes.txt")
    ]
    destination = f"{dufs_origin}{WORKSPACE}/data/notes.txt"
    assert [
        (call["method"], call["url"], call["destination"]) for call in fetch_calls
    ] == [
        ("HEAD", destination, None),
        ("MOVE", f"{dufs_origin}{WORKSPACE}/notes.txt", destination),
    ]


def test_download_from_the_sheet_follows_the_tables_download_link(
    open_files_page: OpenFilesPage, dufs_origin: str
) -> None:
    page = open_files_page(f"{WORKSPACE}/", *PHONE)

    _open_row_menu(page, "README.md")
    with page.expect_download() as download:
        _menu_items(page).filter(has_text="Download file").click()

    assert download.value.url == f"{dufs_origin}{WORKSPACE}/README.md"


def test_tapping_a_folder_row_opens_the_folder(
    open_files_page: OpenFilesPage, dufs_origin: str
) -> None:
    page = open_files_page(f"{WORKSPACE}/", *PHONE)

    page.locator(".phone-row-name", has_text="data").click()

    page.wait_for_url(f"{dufs_origin}{WORKSPACE}/data/")
    expect(page.locator(".phone-title")).to_have_text("data")
    expect(page.locator(".empty-folder")).to_have_text("Empty folder")


def test_a_file_opens_the_editor_page_with_save_and_the_files_kebab(
    open_files_page: OpenFilesPage, dufs_origin: str, fetch_calls: list[dict[str, Any]]
) -> None:
    page = open_files_page(f"{WORKSPACE}/", *PHONE)

    page.locator(".phone-row-name", has_text="README.md").click()
    page.wait_for_url(f"{dufs_origin}{WORKSPACE}/README.md?edit")
    expect(page.locator(".phone-title")).to_have_text("README.md")
    expect(page.locator("#editor")).to_have_value("# workspace\n")
    save = page.get_by_role("button", name="Save")
    expect(save).to_be_disabled()

    page.get_by_role("button", name="More", exact=True).click()
    expect(page.locator(".phone-menu-sheet .phone-menu-title")).to_have_text(
        "README.md"
    )
    expect(_menu_items(page)).to_have_text(["Download file", "Move & Rename", "Delete"])
    page.keyboard.press("Escape")
    expect(page.locator(".phone-menu")).to_have_count(0)

    page.locator("#editor").fill("# workspace\n\nedited on a phone\n")
    expect(save).to_be_enabled()
    # dufs's saveChange reloads the page once the PUT has gone.
    with page.expect_navigation():
        save.click()

    assert [(call["method"], call["url"], call["body"]) for call in fetch_calls] == [
        (
            "PUT",
            f"{dufs_origin}{WORKSPACE}/README.md",
            "# workspace\n\nedited on a phone\n",
        )
    ]


def test_a_files_view_page_offers_edit_from_its_kebab(
    open_files_page: OpenFilesPage, dufs_origin: str
) -> None:
    page = open_files_page(f"{WORKSPACE}/README.md?view", *PHONE)

    expect(page.locator(".phone-title")).to_have_text("README.md")
    expect(page.get_by_role("button", name="Save")).to_have_count(0)
    page.get_by_role("button", name="More", exact=True).click()
    expect(_menu_items(page)).to_have_text(["Download file", "Edit file"])

    _menu_items(page).filter(has_text="Edit file").click()
    page.wait_for_url(f"{dufs_origin}{WORKSPACE}/README.md?edit")
    expect(page.get_by_role("button", name="Save")).to_be_disabled()


def test_the_up_button_leaves_the_editor_for_its_folder(
    open_files_page: OpenFilesPage, dufs_origin: str
) -> None:
    page = open_files_page(f"{WORKSPACE}/README.md?edit", *PHONE)

    page.get_by_role("button", name="Up one folder").click()

    page.wait_for_url(f"{dufs_origin}{WORKSPACE}/")
    expect(page.locator(".phone-row-name")).to_have_text(
        ["data", "README.md", "notes.txt"]
    )


def test_the_sort_keys_are_dufs_header_links(
    open_files_page: OpenFilesPage, dufs_origin: str
) -> None:
    page = open_files_page(f"{WORKSPACE}/", *PHONE)

    keys = page.locator(".phone-sort-key")
    expect(keys).to_have_text(["Name", "Last Modified", "Size"])
    expect(page.locator(".phone-sort-key.on")).to_have_count(0)

    keys.filter(has_text="Last Modified").click()
    page.wait_for_url(f"{dufs_origin}{WORKSPACE}/?order=desc&sort=mtime")
    expect(page.locator(".phone-sort-key.on")).to_have_text("Last Modified")

    keys.filter(has_text="Last Modified").click()
    page.wait_for_url(f"{dufs_origin}{WORKSPACE}/?order=asc&sort=mtime")


def test_search_replaces_the_breadcrumb_and_lists_results_by_relative_path(
    open_files_page: OpenFilesPage, dufs_origin: str
) -> None:
    page = open_files_page(f"{WORKSPACE}/", *PHONE)

    page.get_by_role("button", name="Search", exact=True).click()
    expect(page.locator(".phone-crumb")).to_be_hidden()
    search = page.locator(".phone-search #search")
    expect(search).to_be_focused()
    expect(search).to_have_attribute("placeholder", "Search in workspace")
    search.fill("note")
    search.press("Enter")

    page.wait_for_url(f"{dufs_origin}{WORKSPACE}/?q=note")
    expect(page.locator(".phone-search #search")).to_have_value("note")
    expect(page.locator(".phone-row-name")).to_have_text(["ideas.md", "notes.txt"])
    expect(page.locator(".phone-row-sub")).to_have_text(
        ["data/notes", "2026-09-27 09:06·5 KB"]
    )
    expect(page.locator(".phone-row-link").first).to_have_attribute(
        "href", f"{dufs_origin}{WORKSPACE}/data/notes/ideas.md?edit"
    )

    page.get_by_role("button", name="Close search").click()
    page.wait_for_url(f"{dufs_origin}{WORKSPACE}/")
    expect(page.locator(".phone-crumb")).to_be_visible()


def test_a_desktop_width_keeps_the_table(open_files_page: OpenFilesPage) -> None:
    page = open_files_page(f"{WORKSPACE}/", *DESKTOP)

    expect(page.locator(".paths-table tbody tr")).to_have_count(3)
    expect(page.locator(".paths-table")).to_be_visible()
    expect(page.locator(".head .breadcrumb")).to_be_visible()
    expect(page.locator(".phone-top")).to_have_count(0)
    expect(page.locator(".phone-row")).to_have_count(0)


def test_crossing_the_width_boundary_swaps_the_layouts_live(
    open_files_page: OpenFilesPage,
) -> None:
    page = open_files_page(f"{WORKSPACE}/", *DESKTOP)
    expect(page.locator(".paths-table")).to_be_visible()

    page.set_viewport_size({"width": PHONE[0], "height": PHONE[1]})
    expect(page.locator(".phone-row")).to_have_count(3)
    expect(page.locator(".paths-table")).to_be_hidden()
    expect(page.locator(".phone-crumb .breadcrumb")).to_have_count(1)

    page.set_viewport_size({"width": DESKTOP[0], "height": DESKTOP[1]})
    expect(page.locator(".phone-top")).to_have_count(0)
    expect(page.locator(".phone-row")).to_have_count(0)
    expect(page.locator(".paths-table")).to_be_visible()
    # dufs's breadcrumb and search bar are back where dufs put them.
    expect(page.locator(".head > .breadcrumb + .toolbox")).to_have_count(1)
    expect(
        page.locator(".head > .toolbox + .searchbar + .toolbox-right")
    ).to_have_count(1)
