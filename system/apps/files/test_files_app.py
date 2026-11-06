"""Files, driven in a real browser over the Files server on a temporary folder (see conftest.py).

Every change a test makes goes through the app's own controls and is read back off the disk.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Locator
from playwright.sync_api import Page
from playwright.sync_api import Request
from playwright.sync_api import expect

pytestmark = [pytest.mark.browser, pytest.mark.timeout(60)]

PHONE = (393, 852)

OpenFilesPage = Callable[..., Page]


def _sync(**fields: object) -> dict[str, object]:
    """A sync as Imbue Studio reports it, up to date unless ``fields`` say otherwise."""
    return {
        "state": "SYNCED",
        "message": None,
        "bytes_done": None,
        "bytes_total": None,
        **fields,
    }


def _entry(page: Page, path: str) -> Locator:
    return page.locator(f'[data-entry-path="{path}"]')


def _shown_paths(page: Page) -> list[str]:
    return page.locator("[data-entry-path]").evaluate_all("els => els.map(el => el.dataset.entryPath)")


def test_a_folder_shows_its_entries_as_icons_folders_first_with_system_files_hidden(
    open_files_page: OpenFilesPage,
) -> None:
    page = open_files_page("/")
    expect(page.locator(".files-tile")).to_have_count(4)
    assert _shown_paths(page) == ["/photos/", "/projects/", "/notes.txt", "/README.md"]

    page.locator('[data-action="options"]').click()
    page.locator('[data-menu-row="show-hidden"]').click()
    expect(page.locator(".files-tile")).to_have_count(5)
    assert "/.env" in _shown_paths(page)


def test_the_list_sorts_by_its_column_headers(open_files_page: OpenFilesPage) -> None:
    page = open_files_page("/")
    page.locator('[data-view-mode="list"]').click()
    expect(page.locator(".files-list-header")).to_be_visible()
    assert _shown_paths(page) == ["/photos/", "/projects/", "/notes.txt", "/README.md"]

    page.locator('[data-sort="name"]').click()
    expect(page.locator('[data-sort="name"]')).to_have_attribute("aria-sort", "descending")
    assert _shown_paths(page) == ["/projects/", "/photos/", "/README.md", "/notes.txt"]

    page.locator('[data-sort="size"]').click()
    assert _shown_paths(page)[2:] == ["/README.md", "/notes.txt"]


def test_columns_open_to_the_right_and_end_in_a_preview_of_the_chosen_file(
    open_files_page: OpenFilesPage,
) -> None:
    page = open_files_page("/")
    page.locator('[data-view-mode="columns"]').click()
    page.locator('.files-column [data-entry-path="/projects/"]').click()
    page.locator('.files-column [data-entry-path="/projects/app/"]').click()
    page.locator('.files-column [data-entry-path="/projects/app/main.py"]').click()

    expect(page.locator(".files-column")).to_have_count(4)
    expect(page.locator(".files-preview-text")).to_have_text('print("hi")\n')
    expect(page).to_have_url(page.url.split("/projects")[0] + "/projects/app/")

    heights = page.locator(".files-column .files-row").evaluate_all(
        "rows => [...new Set(rows.map(row => Math.round(row.getBoundingClientRect().height)))]"
    )
    assert heights == [22]

    page.keyboard.press("ArrowLeft")
    expect(_entry(page, "/projects/app/")).to_have_attribute("aria-selected", "true")
    expect(page.locator(".files-preview")).to_have_count(0)


def test_the_preview_keeps_one_height_whatever_it_shows(open_files_page: OpenFilesPage, files_root: Path) -> None:
    (files_root / "archive.zip").write_bytes(b"PK")
    page = open_files_page("/")
    page.locator('[data-view-mode="columns"]').click()
    picture = page.locator(".files-preview-picture")

    def height_of(path: str, shown: str) -> float:
        _entry(page, path).click()
        expect(page.locator(f".files-preview {shown}")).to_be_visible()
        box = picture.bounding_box()
        assert box is not None
        return box["height"]

    assert height_of("/archive.zip", ".file-icon") == 240
    assert height_of("/README.md", ".files-preview-text") == 240
    _entry(page, "/photos/").click()
    assert height_of("/photos/sunset.png", ".files-preview-image") == 240


def test_double_clicking_a_folder_opens_it_and_the_history_keys_return(
    open_files_page: OpenFilesPage,
) -> None:
    page = open_files_page("/")
    _entry(page, "/projects/").dblclick()
    expect(_entry(page, "/projects/app/")).to_be_visible()
    assert page.url.endswith("/projects/")

    page.keyboard.press("ControlOrMeta+BracketLeft")
    expect(_entry(page, "/projects/")).to_be_visible()
    assert page.url.endswith("/")
    page.keyboard.press("ControlOrMeta+BracketRight")
    expect(_entry(page, "/projects/app/")).to_be_visible()


def test_going_back_to_a_folder_opened_without_its_slash_lists_it_by_its_own_paths(
    open_files_page: OpenFilesPage,
) -> None:
    # A chat link to a folder opens it at `/projects?view`.
    page = open_files_page("/projects?view")
    expect(_entry(page, "/projects/app/")).to_be_visible()
    assert page.url.endswith("/projects/")
    _entry(page, "/projects/app/").dblclick()
    expect(_entry(page, "/projects/app/main.py")).to_be_visible()

    page.keyboard.press("ControlOrMeta+BracketLeft")
    expect(_entry(page, "/projects/app/")).to_be_visible()
    assert page.url.endswith("/projects/")


def test_the_sidebar_lists_the_shortcuts_and_every_share_with_how_it_reaches_the_workspace(
    open_files_page: OpenFilesPage, files_root: Path, connections: Any
) -> None:
    workspace = files_root / "home" / "user" / "workspace"
    (workspace / "data" / "uploads").mkdir(parents=True)
    notes_copy = files_root / "home/user/synced_folders/host-mac/Users/kim/notes"
    notes_copy.mkdir(parents=True)
    (notes_copy / "todo.md").write_text("- call\n")
    (files_root / "home/user/synced_folders/host-mac/Users/kim/Brand Fonts").mkdir(parents=True)
    (files_root / "home/user/synced_folders/host-mac/Users/kim/Atkinson").mkdir(parents=True)
    (files_root / "home/user/synced_folders/host-mac/Users/kim/Taxes").mkdir(parents=True)
    connections.listed = {
        "devices": [{"id": "host-mac", "name": "Kims-Mac.local", "has_device_paths": True}],
        "shares": [
            {
                "device": "host-mac",
                "path": "/Users/kim/notes",
                "access": "write",
                "kind": "folder",
                "is_reachable": True,
                "synced_copy": "/home/user/synced_folders/host-mac/Users/kim/notes/",
                "sync": _sync(),
            },
            {
                "device": "host-mac",
                "path": "/Users/kim/Brand Fonts",
                "access": "read",
                "kind": "folder",
                "is_reachable": True,
                "synced_copy": "/home/user/synced_folders/host-mac/Users/kim/Brand Fonts/",
                "sync": _sync(state="FAILED", message="Unison exited with code 3."),
            },
            {
                "device": "host-mac",
                "path": "/Users/kim/Atkinson",
                "access": "write",
                "kind": "folder",
                "is_reachable": True,
                "synced_copy": "/home/user/synced_folders/host-mac/Users/kim/Atkinson/",
                "sync": _sync(state="SYNCING", bytes_done=2_000_000, bytes_total=8_000_000),
            },
            {
                "device": "host-mac",
                "path": "/Users/kim/Taxes",
                "access": "write",
                "kind": "folder",
                "is_reachable": True,
                "synced_copy": "/home/user/synced_folders/host-mac/Users/kim/Taxes/",
                # Its computer did not say how the sync stands.
                "sync": None,
            },
            {
                "device": "host-mac",
                "path": "/Users/kim/photo.png",
                "access": "read",
                "kind": "file",
                "is_reachable": True,
                "synced_copy": None,
                "sync": None,
            },
        ],
    }
    page = open_files_page("/home/user/workspace/")

    expect(page.locator("[data-sidebar-row] .files-sidebar-label")).to_have_text(
        ["Workspace", "Uploads", "notes", "Brand Fonts", "Atkinson", "Taxes", "photo.png"]
    )
    expect(page.locator('[data-sidebar-row="shortcut:workspace"]')).to_have_attribute("aria-current", "true")

    def marks(path: str) -> tuple[str | None, list[str]]:
        row = page.locator(f'[data-sidebar-row="share:host-mac:{path}"]')
        badge = row.locator("[data-share-badge]").get_attribute("data-share-badge")
        return badge, row.locator("[data-mark]").evaluate_all("els => els.map(el => el.dataset.mark)")

    assert marks("/Users/kim/notes") == ("synced", [])
    assert marks("/Users/kim/Brand Fonts") == ("synced", ["read-only", "failed"])
    # A sync on its way turns its badge rather than adding a mark.
    assert marks("/Users/kim/Atkinson") == ("syncing", [])
    assert marks("/Users/kim/photo.png") == ("linked", ["read-only"])
    # A computer not answering greys its share out, icon and all, rather than adding a mark.
    assert marks("/Users/kim/Taxes") == ("synced", [])

    def icon_opacity(path: str) -> str:
        icon = page.locator(f'[data-sidebar-row="share:host-mac:{path}"] [data-share-badge]')
        return icon.evaluate("el => getComputedStyle(el).opacity")

    assert icon_opacity("/Users/kim/Taxes") == "0.5"
    assert icon_opacity("/Users/kim/notes") == "1"
    expect(
        page.locator('[data-sidebar-row="share:host-mac:/Users/kim/photo.png"] [data-mark="read-only"]')
    ).to_have_text("Read-only")
    # A shared folder's row takes drops like any folder; a shared file's does not.
    expect(page.locator('[data-sidebar-row="share:host-mac:/Users/kim/notes"]')).to_have_attribute(
        "data-drop-folder", "/home/user/synced_folders/host-mac/Users/kim/notes/"
    )
    assert (
        page.locator('[data-sidebar-row="share:host-mac:/Users/kim/photo.png"]').get_attribute("data-drop-folder")
        is None
    )

    page.locator('[data-sidebar-row="share:host-mac:/Users/kim/Brand Fonts"]').hover()
    tooltip = page.get_by_role("tooltip")
    expect(tooltip.locator(":scope > span").first).to_have_text("Sync with Kims-Mac stopped")
    expect(tooltip.locator(".hover-tooltip-detail")).to_have_text("Unison exited with code 3")

    page.locator('[data-sidebar-row="share:host-mac:/Users/kim/notes"]').click()
    expect(_entry(page, "/home/user/synced_folders/host-mac/Users/kim/notes/todo.md")).to_be_visible()

    expect(page.locator(".files-crumb")).to_have_text(["Kims-Mac", "notes"])

    page.locator('[data-sidebar-row="share:host-mac:/Users/kim/Brand Fonts"]').click()
    expect(page.locator(".files-sync-notice")).to_contain_text("Sync with Kims-Mac stopped")
    expect(page.locator(".files-sync-notice")).to_contain_text("Unison exited with code 3.")
    expect(page.locator(".files-sync-notice")).not_to_contain_text(".local")
    page.locator('[data-sidebar-row="share:host-mac:/Users/kim/Atkinson"]').click()
    expect(page.locator(".files-sync-notice")).to_contain_text("Syncing from Kims-Mac")
    expect(page.locator(".files-sync-notice")).to_contain_text("25% done")
    page.locator('[data-view-mode="columns"]').click()
    expect(page.locator(".files-column .files-sync-notice")).to_contain_text("Syncing from Kims-Mac")

    # Listed in the folder that holds it, a synced copy wears the same badge as its sidebar row.
    page.goto(page.url.split("/home/")[0] + "/home/user/synced_folders/host-mac/Users/kim/")
    expect(
        _entry(page, "/home/user/synced_folders/host-mac/Users/kim/notes/").locator("[data-icon-badge]")
    ).to_have_attribute("data-icon-badge", "synced")


def test_a_long_reason_for_a_stopped_sync_is_cut_to_two_lines_in_the_tooltip_and_read_in_full_in_the_folder(
    open_files_page: OpenFilesPage, files_root: Path, connections: Any
) -> None:
    (files_root / "home/user/workspace").mkdir(parents=True)
    (files_root / "home/user/synced_folders/host-mac/Users/kim/notes").mkdir(parents=True)
    reason = (
        "Unison could not finish: the archive for /Users/kim/notes is locked by another copy of Unison, "
        "which may still be running or may have stopped without letting go of it. Close it and try again."
    )
    connections.listed = {
        "devices": [{"id": "host-mac", "name": "Kims-Mac.local", "has_device_paths": True}],
        "shares": [
            {
                "device": "host-mac",
                "path": "/Users/kim/notes",
                "access": "write",
                "kind": "folder",
                "is_reachable": True,
                "synced_copy": "/home/user/synced_folders/host-mac/Users/kim/notes/",
                "sync": _sync(state="FAILED", message=reason),
            }
        ],
    }
    page = open_files_page("/home/user/workspace/")

    row = page.locator('[data-sidebar-row="share:host-mac:/Users/kim/notes"]')
    row.hover()
    detail = page.get_by_role("tooltip").locator(".hover-tooltip-detail")
    expect(detail).to_be_visible()
    shown_lines, holds_more = detail.evaluate(
        "el => [Math.round(el.clientHeight / parseFloat(getComputedStyle(el).lineHeight)),"
        " el.scrollHeight > el.clientHeight]"
    )
    assert (shown_lines, holds_more) == (2, True)

    row.click()
    expect(page.locator(".files-sync-notice")).to_contain_text(reason)


_FROM_STUDIO = """(message) => window.postMessage(message, "*")"""


def test_manage_connected_files_shows_only_inside_imbue_studio_and_asks_it_for_its_local_files_settings(
    open_files_page: OpenFilesPage, connections: Any
) -> None:
    connections.listed = {
        "devices": [{"id": "host-mac", "name": "Kims-Mac.local", "has_device_paths": True}],
        "shares": [],
    }
    page = open_files_page("/")
    expect(page.locator(".files-connected-empty")).to_contain_text("Share a file or folder from Imbue Studio")
    expect(page.locator('[data-action="manage-connected"]')).to_have_count(0)

    # A page with no frame around it is its own embedder: what it sends lands on itself, and so does Studio's word.
    page.evaluate("window.sent = []; window.addEventListener('message', (event) => window.sent.push(event.data))")
    page.evaluate(_FROM_STUDIO, {"type": "minds:embedder-capabilities", "canPopOut": False, "opensLocalFiles": True})
    manage = page.locator('[data-action="manage-connected"]')
    expect(manage).to_have_text("Manage connected files")
    expect(page.locator(".files-connected-empty")).to_have_count(0)
    manage.click()
    page.wait_for_function("window.sent.some((message) => message.type === 'minds:open-local-files')")

    # The user shares something in Studio's settings, and coming back to the window lists it.
    connections.listed = {
        "devices": [{"id": "host-mac", "name": "Kims-Mac.local", "has_device_paths": True}],
        "shares": [
            {
                "device": "host-mac",
                "path": "/Users/kim/notes",
                "access": "read",
                "kind": "folder",
                "is_reachable": True,
                "synced_copy": None,
                "sync": None,
            }
        ],
    }
    page.evaluate("window.dispatchEvent(new Event('focus'))")
    expect(page.locator('[data-sidebar-row="share:host-mac:/Users/kim/notes"]')).to_be_visible(timeout=2_000)


def test_the_sidebar_follows_a_share_changed_in_studio_without_the_window_coming_back_to_the_front(
    open_files_page: OpenFilesPage, connections: Any
) -> None:
    notes = {
        "device": "host-mac",
        "path": "/Users/kim/notes",
        "access": "write",
        "kind": "folder",
        "is_reachable": True,
        "synced_copy": "/home/user/synced_folders/host-mac/Users/kim/notes/",
        "sync": _sync(state="SYNCING", bytes_done=1, bytes_total=4),
    }
    fonts = {**notes, "path": "/Users/kim/Brand Fonts", "access": "read", "synced_copy": None, "sync": None}
    device = {"id": "host-mac", "name": "Kims-Mac.local", "has_device_paths": True}
    connections.listed = {"devices": [device], "shares": [notes]}
    page = open_files_page("/")
    row = page.locator('[data-sidebar-row="share:host-mac:/Users/kim/notes"]')
    expect(row.locator('[data-share-badge="syncing"] .files-badge-turning')).to_be_visible()

    connections.listed = {
        "devices": [device],
        "shares": [{**notes, "sync": _sync(state="FAILED", message="Disk full.")}, fonts],
    }
    # Read again within a few seconds while a sync is moving, with nothing from the user.
    expect(row.locator('[data-mark="failed"]')).to_be_visible(timeout=10_000)
    expect(row.locator(".files-badge-turning")).to_have_count(0)
    expect(page.locator('[data-sidebar-row="share:host-mac:/Users/kim/Brand Fonts"]')).to_be_visible()


def test_a_file_a_sync_brings_into_the_open_folder_shows_with_nothing_from_the_user(
    open_files_page: OpenFilesPage, files_root: Path, connections: Any
) -> None:
    copy = files_root / "home/user/synced_folders/host-mac/Users/kim/Brand Fonts"
    copy.mkdir(parents=True)
    (copy / "Inter.ttf").write_text("font\n")
    connections.listed = {
        "devices": [{"id": "host-mac", "name": "Kims-Mac.local", "has_device_paths": True}],
        "shares": [
            {
                "device": "host-mac",
                "path": "/Users/kim/Brand Fonts",
                "access": "write",
                "kind": "folder",
                "is_reachable": True,
                "synced_copy": "/home/user/synced_folders/host-mac/Users/kim/Brand Fonts/",
                # Settled, so the shares are read again only every 15 s: the folder's own check finds the file.
                "sync": _sync(),
            }
        ],
    }
    folder = "/home/user/synced_folders/host-mac/Users/kim/Brand Fonts/"
    page = open_files_page(folder)
    expect(_entry(page, folder + "Inter.ttf")).to_be_visible()

    (copy / "Atkinson.otf").write_text("font\n")

    expect(_entry(page, folder + "Atkinson.otf")).to_be_visible(timeout=5_000)


def test_enter_on_a_focused_button_presses_it_rather_than_opening_the_selection(
    open_files_page: OpenFilesPage, files_root: Path
) -> None:
    (files_root / "home/user/workspace/data/uploads").mkdir(parents=True)
    page = open_files_page("/")
    _entry(page, "/projects/").click()
    page.locator('[data-sidebar-row="shortcut:uploads"]').focus()
    page.keyboard.press("Enter")
    expect(page.locator('[data-sidebar-row="shortcut:uploads"]')).to_have_attribute("aria-current", "true")
    assert not page.url.endswith("/projects/")


def test_enter_opens_an_entry_chosen_by_the_arrows_after_a_button_was_pressed(open_files_page: OpenFilesPage) -> None:
    page = open_files_page("/")
    expect(_entry(page, "/photos/")).to_be_visible()
    page.locator('[data-action="add"]').focus()
    page.keyboard.press("ArrowDown")
    expect(_entry(page, "/photos/")).to_have_attribute("aria-selected", "true")
    page.keyboard.press("Enter")
    expect(_entry(page, "/photos/sunset.png")).to_be_visible()


_DROP_FROM_COMPUTER = """([selector, name]) => {
  const transfer = new DataTransfer();
  transfer.items.add(new File(["bytes"], name, { type: "application/pdf" }));
  const target = document.querySelector(selector);
  for (const type of ["dragenter", "dragover", "drop"]) {
    target.dispatchEvent(new DragEvent(type, { bubbles: true, cancelable: true, dataTransfer: transfer }));
  }
}"""


def test_a_file_dropped_on_a_shared_folder_uploads_into_it(
    open_files_page: OpenFilesPage, files_root: Path, connections: Any
) -> None:
    copy = files_root / "home/user/synced_folders/host-mac/Users/kim/notes"
    copy.mkdir(parents=True)
    connections.listed = {
        "devices": [{"id": "host-mac", "name": "Kims-Mac.local", "has_device_paths": True}],
        "shares": [
            {
                "device": "host-mac",
                "path": "/Users/kim/notes",
                "access": "write",
                "kind": "folder",
                "is_reachable": True,
                "synced_copy": "/home/user/synced_folders/host-mac/Users/kim/notes/",
                "sync": _sync(),
            }
        ],
    }
    page = open_files_page("/")
    expect(page.locator('[data-sidebar-row="share:host-mac:/Users/kim/notes"]')).to_be_visible()
    page.evaluate(_DROP_FROM_COMPUTER, ['[data-sidebar-row="share:host-mac:/Users/kim/notes"]', "brief.pdf"])

    expect(page.locator(".files-uploads-summary")).to_have_text("Uploaded 1 item")
    assert (copy / "brief.pdf").read_bytes() == b"bytes"


def test_the_sidebar_says_when_no_folder_is_connected_and_resizes_by_its_edge(
    open_files_page: OpenFilesPage,
) -> None:
    page = open_files_page("/")
    expect(page.locator(".files-sidebar [data-sidebar-row^='share:']")).to_have_count(0)
    expect(page.locator(".files-connected-empty")).to_have_text(
        "Nothing yet. Share a file or folder from Imbue Studio on your computer."
    )
    sidebar = page.locator(".files-sidebar")
    start = sidebar.bounding_box()
    assert start is not None and round(start["width"]) == 208

    handle = page.locator(".files-sidebar-resize").bounding_box()
    assert handle is not None
    page.mouse.move(handle["x"] + handle["width"] / 2, handle["y"] + 200)
    page.mouse.down()
    page.mouse.move(handle["x"] + handle["width"] / 2 + 60, handle["y"] + 200, steps=4)
    page.mouse.up()
    resized = sidebar.bounding_box()
    assert resized is not None and round(resized["width"]) == 268
    assert page.evaluate("localStorage.getItem('files-sidebar-width')") == "268"


def test_f2_renames_an_entry_in_place_and_refuses_a_slash(open_files_page: OpenFilesPage, files_root: Path) -> None:
    page = open_files_page("/")
    _entry(page, "/notes.txt").click()
    page.keyboard.press("F2")
    field = page.locator(".files-rename-field")
    expect(field).to_be_focused()
    page.keyboard.type("a/b")
    page.keyboard.press("Enter")
    expect(page.locator(".files-rename-error")).to_have_text('A name can\'t contain "/".')

    field.fill("journal.txt")
    page.keyboard.press("Enter")
    expect(_entry(page, "/journal.txt")).to_be_visible()
    assert (files_root / "journal.txt").read_text() == "first line\nsecond line\n"
    assert not (files_root / "notes.txt").exists()


def test_delete_from_the_entry_menu_asks_first_and_removes_the_file(
    open_files_page: OpenFilesPage, files_root: Path
) -> None:
    page = open_files_page("/")
    _entry(page, "/README.md").click(button="right")
    page.locator('[data-menu-row="delete"]').click()
    expect(page.locator('[data-dialog="delete"]')).to_contain_text('Delete "README.md"?')
    page.locator('[data-dialog-action="confirm"]').click()

    expect(_entry(page, "/README.md")).to_have_count(0)
    assert not (files_root / "README.md").exists()


def test_download_loads_in_a_frame_and_saves_a_folder_as_a_zip_and_a_file_under_its_name(
    open_files_page: OpenFilesPage,
) -> None:
    """A download is a frame's load, whose request carries the workspace's session when Imbue Studio frames Files from
    another site (a ``download`` link's request goes without it), and the server names the file."""
    page = open_files_page("/")
    download_requests: list[Request] = []

    def record_download_request(request: Request) -> None:
        if "?zip" in request.url or "?download" in request.url:
            download_requests.append(request)

    page.on("request", record_download_request)
    _entry(page, "/projects/").click(button="right")
    with page.expect_download() as folder:
        page.locator('[data-menu-row="download"]').click()
    assert folder.value.suggested_filename == "projects.zip"

    _entry(page, "/notes.txt").click(button="right")
    with page.expect_download() as file:
        page.locator('[data-menu-row="download"]').click()
    assert file.value.suggested_filename == "notes.txt"
    assert file.value.path().read_text() == "first line\nsecond line\n"

    file_page = open_files_page("/README.md?view")
    file_page.on("request", record_download_request)
    with file_page.expect_download() as from_its_page:
        file_page.locator('[data-action="download"]').click()
    assert from_its_page.value.suggested_filename == "README.md"
    assert [request.all_headers()["sec-fetch-dest"] for request in download_requests] == ["iframe", "iframe", "iframe"]


def test_a_download_the_server_refuses_says_why_in_a_toast(open_files_page: OpenFilesPage, files_root: Path) -> None:
    page = open_files_page("/")
    expect(_entry(page, "/notes.txt")).to_be_visible()
    (files_root / "notes.txt").unlink()
    _entry(page, "/notes.txt").click(button="right")
    page.locator('[data-menu-row="download"]').click()
    expect(page.locator("[data-toast]")).to_have_text("Couldn't download notes.txt: There's nothing at /notes.txt.")


def test_add_makes_a_new_folder_and_names_it(open_files_page: OpenFilesPage, files_root: Path) -> None:
    page = open_files_page("/")
    page.locator('[data-action="add"]').click()
    page.locator('[data-menu-row="new-folder"]').click()
    expect(page.locator(".files-rename-field")).to_be_focused()
    page.keyboard.type("Drafts")
    page.keyboard.press("Enter")

    expect(_entry(page, "/Drafts/")).to_be_visible()
    assert (files_root / "Drafts").is_dir()
    assert not (files_root / "untitled folder").exists()


def test_uploads_land_in_the_folder_and_a_taken_name_can_keep_both(
    open_files_page: OpenFilesPage, files_root: Path
) -> None:
    page = open_files_page("/photos/")
    with page.expect_file_chooser() as chooser:
        page.locator('[data-action="add"]').click()
        page.locator('[data-menu-row="upload-files"]').click()
    chooser.value.set_files([{"name": "notes.txt", "mimeType": "text/plain", "buffer": b"uploaded"}])
    expect(_entry(page, "/photos/notes.txt")).to_be_visible()
    expect(page.locator(".files-uploads-summary")).to_have_text("Uploaded 1 item")
    assert (files_root / "photos" / "notes.txt").read_text() == "uploaded"

    with page.expect_file_chooser() as chooser:
        page.locator('[data-action="add"]').click()
        page.locator('[data-menu-row="upload-files"]').click()
    chooser.value.set_files([{"name": "notes.txt", "mimeType": "text/plain", "buffer": b"again"}])
    page.locator('[data-dialog-action="keep-both"]').click()
    expect(_entry(page, "/photos/notes 2.txt")).to_be_visible()
    assert (files_root / "photos" / "notes.txt").read_text() == "uploaded"
    assert (files_root / "photos" / "notes 2.txt").read_text() == "again"


def test_files_dropped_on_a_folder_upload_into_it(open_files_page: OpenFilesPage, files_root: Path) -> None:
    page = open_files_page("/")
    page.evaluate(
        """() => {
          const transfer = new DataTransfer();
          transfer.items.add(new File(["dropped"], "dropped.txt", { type: "text/plain" }));
          const folder = document.querySelector('[data-entry-path="/projects/"]');
          for (const type of ["dragenter", "dragover", "drop"]) {
            folder.dispatchEvent(new DragEvent(type, { bubbles: true, cancelable: true, dataTransfer: transfer }));
          }
        }"""
    )
    expect(page.locator(".files-uploads-summary")).to_have_text("Uploaded 1 item")
    assert (files_root / "projects" / "dropped.txt").read_text() == "dropped"
    expect(page.locator(".files-uploads")).to_have_count(0)


@pytest.mark.flaky
def test_an_entry_dragged_onto_a_folder_moves_into_it(open_files_page: OpenFilesPage, files_root: Path) -> None:
    page = open_files_page("/")
    _entry(page, "/README.md").drag_to(_entry(page, "/projects/"))
    expect(_entry(page, "/README.md")).to_have_count(0)
    assert (files_root / "projects" / "README.md").read_text() == "# hello\n"


def test_a_path_param_opens_that_folder(open_files_page: OpenFilesPage) -> None:
    page = open_files_page("/?path=/projects/app/")
    expect(_entry(page, "/projects/app/main.py")).to_be_visible()
    assert page.url.endswith("/projects/app/")
    assert page.title() == "app"


def test_a_path_param_naming_a_file_opens_its_page(open_files_page: OpenFilesPage) -> None:
    page = open_files_page("/?path=/projects/app/main.py")
    expect(page.locator(".files-file-name")).to_have_text("main.py")
    assert page.url.endswith("/projects/app/main.py?view")


def test_a_phone_lists_entries_and_opens_one_with_a_tap(
    open_files_page: OpenFilesPage,
) -> None:
    page = open_files_page("/", *PHONE)
    expect(page.locator(".files-sidebar")).to_have_count(0)
    page.locator('[data-action="sidebar"]').click()
    expect(page.locator(".files-sidebar")).to_be_visible()
    page.locator(".files-sidebar-scrim").click(position={"x": 380, "y": 400})
    expect(page.locator(".files-sidebar")).to_have_count(0)
    expect(page.locator('[data-view-mode="columns"]')).to_have_count(0)
    page.locator('[data-view-mode="list"]').click()
    expect(page.locator(".files-entry-menu-button")).to_have_count(4)

    _entry(page, "/projects/").click()
    expect(_entry(page, "/projects/app/")).to_be_visible()
    _entry(page, "/projects/app/").click()
    _entry(page, "/projects/app/main.py").click()
    expect(page).to_have_url(page.url.split("/projects")[0] + "/projects/app/main.py?view")


def test_a_narrow_desktop_window_keeps_its_sidebar_beside_the_folder_without_the_column_view(
    open_files_page: OpenFilesPage,
) -> None:
    page = open_files_page("/", 600, 600)
    expect(page.locator(".files-sidebar")).to_be_visible()
    expect(page.locator('[data-action="sidebar"]')).to_have_count(0)
    page.locator('[data-view-mode="list"]').click()
    # A desktop window: no "..." button on every entry, as on a phone.
    expect(page.locator(".files-entry-menu-button")).to_have_count(0)
    expect(page.locator('[data-view-mode="columns"]')).to_have_count(0)


def test_a_text_file_is_edited_and_saved_from_its_page(open_files_page: OpenFilesPage, files_root: Path) -> None:
    page = open_files_page("/notes.txt?view")
    editor = page.locator(".files-editor")
    expect(editor).to_have_value("first line\nsecond line\n")
    expect(editor).to_have_attribute("readonly", "")
    expect(page.locator(".files-file-folder")).to_have_count(0)

    page.locator(".edit-file").click()
    expect(page.locator('[data-action="save"]')).to_be_disabled()
    page.locator(".files-editor").fill("rewritten\n")
    expect(page.locator(".files-file-dirty")).to_have_text("Edited")
    page.locator('[data-action="save"]').click()
    expect(page.locator(".files-file-dirty")).to_have_count(0)
    assert (files_root / "notes.txt").read_text() == "rewritten\n"


def test_an_image_page_draws_the_image(open_files_page: OpenFilesPage) -> None:
    page = open_files_page("/photos/sunset.png?view")
    image = page.locator(".files-file-image")
    expect(image).to_be_visible()
    assert image.evaluate("img => img.naturalWidth") == 1
    expect(page.locator(".edit-file")).to_have_count(0)


def test_deleting_a_file_from_its_page_leaves_the_window_saying_so(
    open_files_page: OpenFilesPage, files_root: Path
) -> None:
    page = open_files_page("/README.md?view")
    page.locator('[data-action="more"]').click()
    page.locator('[data-menu-row="delete"]').click()
    page.locator('[data-dialog-action="confirm"]').click()
    expect(page.locator(".files-file-deleted")).to_contain_text('"README.md" was deleted')
    assert page.url.endswith("/README.md?view")
    assert not (files_root / "README.md").exists()
