"""The notifications page in a real browser: what the owner sees of the devices, the send log, the full-text switch and
the reset, the explicit choice a computer's browser asks for, and that a visitor sees none of it."""

from collections.abc import Generator
from datetime import datetime
from datetime import timezone

import pytest
from flask import Flask
from playwright.sync_api import Browser
from playwright.sync_api import Page
from playwright.sync_api import expect
from workspace_layout.identity import RequestIdentity

from imbue.system_interface.app_context import state_of
from imbue.system_interface.shell.testing import identity_headers
from imbue.system_interface.shell.web_push import PushNotification
from imbue.system_interface.shell.web_push_dispatch import PushSendRequest
from imbue.system_interface.testing import ServedApp
from imbue.system_interface.testing import push_device_registration
from imbue.system_interface.testing import recording_push_transport
from imbue.system_interface.testing import serve_app

pytestmark = [pytest.mark.browser]

_PHONE = "https://web.push.apple.com/phone"
_LOST = "https://fcm.googleapis.com/fcm/send/lost"
_NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
_VISITOR = RequestIdentity(owner=False, user_id="user-visitor", email="visitor@example.com")


@pytest.fixture
def served_shell(app: Flask) -> Generator[ServedApp, None, None]:
    """The shell with two devices, one of which a push service has refused the key for, and one chat's send."""
    recording_push_transport(app).status_code_by_endpoint[_LOST] = 403
    shell = state_of(app).shell
    shell.web_push.register(push_device_registration(_PHONE, "iPhone, Safari (Home Screen app)"), _NOW)
    shell.web_push.register(push_device_registration(_LOST, "Android, Chrome"), _NOW)
    shell.web_push.send(PushNotification(title="Warm-up"), None, _NOW)
    shell.web_push_dispatch.submit(
        PushSendRequest(
            chat_id="agent-research",
            chat_title="Research",
            message="Report ready: 12 pages",
            app="chat",
            app_path="/?chat=agent-research",
        ),
        "Studio",
    )
    shell.web_push_dispatch.flush_due()
    with serve_app(app) as served:
        yield served


# Headless Chromium answers every notification permission "denied", whatever the context grants; a browser that has
# not yet been asked answers "default", which is how the owner first finds the page.
_NOT_YET_ASKED = "Object.defineProperty(Notification, 'permission', { get: () => 'default' });"


def _open_page(page: Page, served_shell: ServedApp) -> None:
    page.add_init_script(_NOT_YET_ASKED)
    page.goto(f"{served_shell.http_url}/notifications")


def test_the_owner_sees_each_device_the_send_log_and_a_computer_asks_before_turning_on(
    served_shell: ServedApp, page: Page
) -> None:
    _open_page(page, served_shell)

    devices = page.locator("#devices")
    expect(devices).to_contain_text("iPhone, Safari (Home Screen app)")
    expect(devices).to_contain_text("Not reaching it (its push service answered 403)")
    sends = page.locator("#sends")
    expect(sends).to_contain_text("Research")
    expect(sends).to_contain_text("Showed: Research: Has something for you in Studio.")
    expect(sends).to_contain_text("iPhone, Safari (Home Screen app): delivered")
    # Headless Chromium's pointer is a mouse: a computer, which turns on only by explicit choice.
    expect(page.locator("#desktop-note")).to_be_visible()
    expect(page.locator("#turn-on")).to_have_text("Turn on in this browser anyway")
    expect(page.locator("#full-text")).not_to_be_checked()


def test_the_full_text_switch_is_kept(served_shell: ServedApp, page: Page, app: Flask) -> None:
    _open_page(page, served_shell)
    full_text = page.locator("#full-text")
    expect(full_text).to_be_enabled()

    full_text.check()

    expect(full_text).to_be_enabled()
    assert state_of(app).shell.web_push.settings().is_full_text
    page.reload()
    expect(page.locator("#full-text")).to_be_checked()


def test_a_reset_asks_once_more_and_then_turns_every_device_off(
    served_shell: ServedApp, page: Page, app: Flask
) -> None:
    _open_page(page, served_shell)
    expect(page.locator("#devices li")).to_have_count(2)

    page.locator("#reset").click()
    page.locator("#reset-cancel").click()
    expect(page.locator("#devices li")).to_have_count(2)
    page.locator("#reset").click()
    page.locator("#reset-confirm").click()

    expect(page.locator("#devices li")).to_have_count(0)
    expect(page.locator("#status")).to_contain_text("Notifications are off on every device")
    assert state_of(app).shell.web_push.list_devices() == ()


def test_a_visitor_sees_no_devices_no_log_and_no_controls(served_shell: ServedApp, browser: Browser) -> None:
    context = browser.new_context(extra_http_headers=identity_headers(_VISITOR))
    try:
        page = context.new_page()
        _open_page(page, served_shell)

        expect(page.locator("#status")).to_contain_text("Only the workspace's owner can turn notifications on here.")
        expect(page.locator("#owner-controls")).to_be_hidden()
        expect(page.locator("#devices li")).to_have_count(0)
    finally:
        context.close()
