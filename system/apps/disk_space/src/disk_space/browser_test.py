"""The page in a real browser, over a planted measurement: a desktop window and a phone.

The measurement is planted and the disk's figures are fixed, so the page is judged on what it draws and does rather
than on what this machine happens to be using. The one folder the safe list offers is the test's own, so the delete
these tests run through the page deletes nothing else.
"""

import json
from pathlib import Path

import pytest
from playwright.sync_api import (
    Browser,
    BrowserContext,
    FloatRect,
    Locator,
    Page,
    expect,
)

from disk_space import scan, store
from disk_space.testing import (
    GIB,
    a_disk,
    a_node,
    a_saved_item,
    install_disk,
    latest_measurement,
    save_measurement,
)

pytestmark = [pytest.mark.browser, pytest.mark.timeout(120, func_only=False)]

PHONE_WIDTH = 393
PHONE_HEIGHT = 852
PHONE_SIDEWAYS_WIDTH = 852
PHONE_SIDEWAYS_HEIGHT = 393
DESKTOP_WIDTH = 1180
DESKTOP_HEIGHT = 820


def _open(
    browser: Browser,
    origin: str,
    *,
    width: int,
    height: int,
    touch: bool = False,
    mobile: bool = False,
) -> tuple[BrowserContext, Page]:
    """The page on a fresh context of ``width`` x ``height``, with a finger when ``touch`` says so."""
    context = browser.new_context(
        viewport={"width": width, "height": height}, has_touch=touch, is_mobile=mobile
    )
    page = context.new_page()
    page.goto(origin + "/")
    return context, page


def _box(locator: Locator) -> FloatRect:
    """Where a control is on screen; a control that is not drawn fails the test."""
    box = locator.bounding_box()
    assert box is not None, "the control is not on screen"
    return box


class Planted:
    """A measurement with something of each verdict in it, and the one safe item that is the test's own folder."""

    def __init__(self, item_path: str, item_folder: Path) -> None:
        self.item_path = item_path
        self.item_folder = item_folder


@pytest.fixture
def planted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Planted:
    item_path, item_folder = a_saved_item(
        tmp_path, name="leftover-build", size=20 * 1024 * 1024
    )
    save_measurement(
        [
            a_node("root", None, "Whole disk", size=93 * GIB),
            a_node("g1", "root", "Your workspace", size=60 * GIB),
            a_node(
                "n1",
                "g1",
                "Workspace code",
                size=30 * GIB,
                path="/home/user/workspace/system",
                verdict=scan.KEEP,
                why="The apps and services themselves.",
                item=True,
            ),
            a_node(
                "n2",
                "g1",
                "Your folder: reports",
                size=15 * GIB,
                path="/home/user/workspace/data/reports",
                verdict=scan.ASK,
                why="A folder of yours. Only you know if it still matters.",
                item=True,
                method="remove",
            ),
            a_node("g2", "root", "Download caches", size=33 * GIB),
            a_node(
                "n3",
                "g2",
                "Leftover build",
                size=20 * 1024 * 1024,
                path=item_path,
                verdict=scan.SAFE,
                why="Leftover scratch files nothing has touched in over 7 days.",
                item=True,
                method="remove",
            ),
            a_node(
                "n4",
                "g2",
                "AI model downloads",
                size=12 * GIB,
                path="/var/cache/user/huggingface",
                verdict=scan.SAFE,
                why="Downloaded again the next time something needs them.",
                item=True,
                method="remove",
            ),
        ]
    )
    install_disk(monkeypatch, a_disk(used=93 * GIB, free=7 * GIB, percent=93))
    return Planted(item_path, item_folder)


def _no_sideways_scroll(page: Page, width: int) -> None:
    scroll_width = page.evaluate("document.documentElement.scrollWidth")
    assert scroll_width <= width, (
        f"the page is {scroll_width}px wide in a {width}px viewport"
    )


def test_the_page_draws_the_gauge_the_alert_and_the_three_lists(
    module_browser: Browser, served_origin: str, planted: Planted
) -> None:
    context, page = _open(
        module_browser, served_origin, width=DESKTOP_WIDTH, height=DESKTOP_HEIGHT
    )
    try:
        expect(page.locator("#used-text")).to_contain_text("93% full")
        expect(page.locator("#used-text")).to_contain_text("of 100.0 GB")
        expect(page.locator("#free-text")).to_have_text("7.0 GB free")
        expect(page.locator("#alert-line-text")).to_have_text("Alert at 90%")
        expect(page.locator("#gauge")).to_have_class("bar guard-bar full")
        # Over the line, the page says so and offers the cleanup helper.
        expect(page.locator("#alert")).to_be_visible()
        expect(page.locator("#alert-helper")).to_have_text("Talk to the cleanup helper")

        # The treemap draws one tile per group, coloured by verdict.
        tiles = page.locator(".treemap .tile")
        expect(tiles).to_have_count(2)
        expect(page.locator(".legend")).to_contain_text("Safe to delete")
        expect(page.locator(".legend")).to_contain_text("Ask first")
        expect(page.locator(".legend")).to_contain_text("Keep")

        # The three lists hold what the measurement decided, with their sizes.
        expect(page.locator("#safe-list li")).to_have_count(2)
        expect(page.locator("#ask-list li")).to_have_count(1)
        expect(page.locator("#safe-total")).to_have_text("12.0 GB")
        expect(page.locator("#ask-total")).to_have_text("15.0 GB")
        expect(page.locator("#keep-total")).to_have_text("30.0 GB")
        expect(page.locator("#safe-list")).to_contain_text("Leftover build")
        expect(page.locator("#safe-list")).to_contain_text(
            "Downloaded again the next time something needs them."
        )
        expect(page.locator("#ask-list")).to_contain_text("Your folder: reports")
        expect(
            page.locator("#ask-list").get_by_role("button", name="Ask about this")
        ).to_be_visible()

        # The measurement links to the raw record it came from.
        expect(page.locator("#snapshot-age a")).to_have_attribute(
            "href", f"/raw/snapshot/{latest_measurement()['id']}"
        )
    finally:
        context.close()


def test_clicking_a_block_looks_inside_it_and_the_crumbs_lead_back(
    module_browser: Browser, served_origin: str, planted: Planted
) -> None:
    context, page = _open(
        module_browser, served_origin, width=DESKTOP_WIDTH, height=DESKTOP_HEIGHT
    )
    try:
        expect(page.locator(".treemap .tile")).to_have_count(2)
        page.locator(".treemap .tile", has_text="Your workspace").click()
        expect(page.locator("#crumbs")).to_contain_text("Whole disk")
        expect(page.locator("#crumbs")).to_contain_text("Your workspace")
        expect(page.locator(".treemap .tile")).to_have_count(2)
        expect(page.locator(".treemap")).to_contain_text("Workspace code")
        page.locator("#crumbs a").click()
        expect(page.locator(".treemap")).to_contain_text("Download caches")
        expect(page.locator(".treemap")).to_contain_text("Your workspace")
    finally:
        context.close()


def test_deleting_a_safe_item_through_the_page_asks_first_and_then_deletes_it(
    module_browser: Browser, served_origin: str, planted: Planted
) -> None:
    context, page = _open(
        module_browser, served_origin, width=DESKTOP_WIDTH, height=DESKTOP_HEIGHT
    )
    try:
        checkbox = page.locator(
            f'#safe-list li[data-path="{planted.item_path}"] input[type="checkbox"]'
        )
        expect(checkbox).to_be_enabled()
        expect(page.locator("#delete-selected")).to_be_disabled()
        checkbox.check()
        expect(page.locator("#delete-selected")).to_be_enabled()
        expect(page.locator("#delete-selected")).to_contain_text(
            "Delete selected (20 MB)"
        )

        page.locator("#delete-selected").click()
        expect(page.locator("#sheet")).to_be_visible()
        expect(page.locator("#sheet-list")).to_contain_text("Leftover build")
        expect(page.locator("#sheet-note")).to_contain_text("About 20 MB")
        page.locator("#sheet-cancel").click()
        expect(page.locator("#sheet")).to_be_hidden()
        assert planted.item_folder.exists()

        page.locator("#delete-selected").click()
        page.locator("#sheet-confirm").click()
        expect(page.locator("#toast")).to_be_visible()
        expect(page.locator("#toast")).to_contain_text("Freed")
        assert not planted.item_folder.exists()

        # The page catches up on its own, and the deletion is on record with a way back to the raw entry.
        expect(page.locator("#safe-list")).not_to_contain_text("Leftover build")
        expect(page.locator("#safe-total")).to_have_text("12.0 GB")
        history_row = page.locator("#history li").first
        expect(history_row).to_contain_text("You deleted 1 item (Leftover build).")
        entry_id = store.read_history()[0]["id"]
        expect(history_row.get_by_role("link", name="Details")).to_have_attribute(
            "href", f"/raw/history/{entry_id}"
        )
    finally:
        context.close()


def test_the_page_before_any_measurement_says_what_to_do(
    module_browser: Browser, served_origin: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_disk(monkeypatch, a_disk(used=50 * GIB, free=50 * GIB, percent=50))
    context, page = _open(
        module_browser, served_origin, width=DESKTOP_WIDTH, height=DESKTOP_HEIGHT
    )
    try:
        expect(page.locator("#used-text")).to_contain_text("50% full")
        expect(page.locator("#alert")).to_be_hidden()
        expect(page.locator("#snapshot-age")).to_have_text("Not measured yet")
        expect(page.locator(".treemap-empty")).to_contain_text("Measure again")
        # A total of nothing reads as nothing, not as a suspicious 1 MB.
        expect(page.locator("#safe-total")).to_have_text("0 MB")
        expect(page.locator("#ask-total")).to_have_text("0 MB")
        expect(page.locator("#safe-list")).to_contain_text("Nothing here right now.")
        expect(page.locator("#ask-list")).to_contain_text("Nothing here right now.")
        expect(page.locator("#history")).to_contain_text("Nothing yet.")
    finally:
        context.close()


def test_a_phone_shows_the_page_without_scrolling_sideways_and_can_delete_by_tapping(
    module_browser: Browser, served_origin: str, planted: Planted
) -> None:
    context, page = _open(
        module_browser,
        served_origin,
        width=PHONE_WIDTH,
        height=PHONE_HEIGHT,
        touch=True,
        mobile=True,
    )
    try:
        expect(page.locator("#used-text")).to_contain_text("93% full")
        _no_sideways_scroll(page, PHONE_WIDTH)

        # Everything the page needs is big enough for a finger.
        delete_button = page.locator("#delete-selected")
        assert _box(delete_button)["height"] >= 44
        assert (
            page.locator("#threshold").evaluate(
                "el => parseFloat(getComputedStyle(el).fontSize)"
            )
            >= 16
        )
        assert (
            _box(
                page.locator(
                    f'#safe-list li[data-path="{planted.item_path}"] input[type="checkbox"]'
                )
            )["height"]
            >= 20
        )

        # The whole action, by tapping: select all, delete, confirm, and the item is gone.
        page.locator("#select-all").tap()
        expect(delete_button).to_be_enabled()
        delete_button.tap()
        sheet = page.locator(".sheet")
        expect(sheet).to_be_visible()
        box = _box(sheet)
        assert box["x"] >= 0 and box["x"] + box["width"] <= PHONE_WIDTH
        assert box["y"] >= 0 and box["y"] + box["height"] <= PHONE_HEIGHT
        page.locator("#sheet-confirm").tap()
        expect(page.locator("#toast")).to_contain_text("Freed")
        assert not planted.item_folder.exists()
        expect(page.locator("#safe-list")).not_to_contain_text("Leftover build")
        _no_sideways_scroll(page, PHONE_WIDTH)
    finally:
        context.close()


def test_a_phone_held_sideways_fits_too(
    module_browser: Browser, served_origin: str, planted: Planted
) -> None:
    context, page = _open(
        module_browser,
        served_origin,
        width=PHONE_SIDEWAYS_WIDTH,
        height=PHONE_SIDEWAYS_HEIGHT,
        touch=True,
        mobile=True,
    )
    try:
        expect(page.locator("#used-text")).to_contain_text("93% full")
        _no_sideways_scroll(page, PHONE_SIDEWAYS_WIDTH)
        # A finger is a finger whichever way the phone is held: the targets stay big.
        assert _box(page.locator("#select-all"))["height"] >= 44
    finally:
        context.close()


def test_a_visitor_sees_the_breakdown_but_not_the_controls_that_change_anything(
    module_browser: Browser,
    served_origin: str,
    planted: Planted,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_disk(monkeypatch, a_disk(used=93 * GIB, free=7 * GIB, percent=93))
    context = module_browser.new_context(
        viewport={"width": DESKTOP_WIDTH, "height": DESKTOP_HEIGHT}
    )
    context.set_extra_http_headers(
        {
            "X-Imbue-Identity": json.dumps(
                {"owner": False, "user_id": "user-1", "email": "someone@example.com"}
            )
        }
    )
    page = context.new_page()
    try:
        page.goto(served_origin + "/")
        expect(page.locator("#used-text")).to_contain_text("93% full")
        expect(page.locator("#safe-list")).to_contain_text("Leftover build")
        expect(page.locator("#owner-note")).to_be_visible()
        expect(page.locator("#owner-note")).to_contain_text(
            "only its owner can free space from here"
        )
        expect(page.locator("#select-all")).to_be_disabled()
        expect(page.locator("#alert-helper")).to_be_disabled()
        expect(page.locator("#helper-inline")).to_be_disabled()
        expect(page.locator("#threshold")).to_be_disabled()
        expect(
            page.locator("#ask-list").get_by_role("button", name="Ask about this")
        ).to_be_disabled()
        expect(page.locator("#delete-selected")).to_be_disabled()
        assert planted.item_folder.exists()
    finally:
        context.close()


def test_the_viewport_meta_lets_a_phone_draw_the_page_at_its_own_width(
    module_browser: Browser, served_origin: str
) -> None:
    context, page = _open(
        module_browser,
        served_origin,
        width=PHONE_WIDTH,
        height=PHONE_HEIGHT,
        touch=True,
        mobile=True,
    )
    try:
        assert (
            page.locator('meta[name="viewport"]').get_attribute("content")
            == "width=device-width, initial-scale=1"
        )
        assert page.evaluate("window.innerWidth") == PHONE_WIDTH
    finally:
        context.close()


def test_the_raw_records_the_page_links_open_as_json(
    module_browser: Browser, served_origin: str, planted: Planted
) -> None:
    context, page = _open(
        module_browser, served_origin, width=DESKTOP_WIDTH, height=DESKTOP_HEIGHT
    )
    try:
        snapshot_id = latest_measurement()["id"]
        raw = page.request.get(f"{served_origin}/raw/snapshot/{snapshot_id}")
        assert raw.ok
        assert json.loads(raw.body())["id"] == snapshot_id
    finally:
        context.close()
