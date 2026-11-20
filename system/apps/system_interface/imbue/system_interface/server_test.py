"""Tests for the Flask server."""

import errno
import html
import json
import os
import re
import subprocess
from collections.abc import Callable
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from app_manifest.content_security_policy import inline_source
from app_manifest.testing import assert_document_policy_admits_page
from app_schema.testing import CROSS_SITE_FETCH_HEADERS
from app_schema.testing import assert_api_schema_describes_app
from app_schema.testing import assert_request_guard_refuses_unknown_provenance
from flask import Flask
from flask.testing import FlaskClient
from workspace_layout.identity import ANONYMOUS_OWNER
from workspace_layout.identity import RequestIdentity
from workspace_layout.primitives import ClientId

from imbue.imbue_common.model_update import to_update
from imbue.mngr.utils.polling import wait_for
from imbue.system_interface.app_context import state_of
from imbue.system_interface.avatar.primitives import DesignId
from imbue.system_interface.avatar.testing import png_size
from imbue.system_interface.config import Config
from imbue.system_interface.documents import BASE_PATH_META_NAME
from imbue.system_interface.documents import FRONTEND_BUILT_HEADER
from imbue.system_interface.documents import THEME_GALLERY_ROUTE
from imbue.system_interface.presence import PRESENCE_CONNECTED_WINDOW
from imbue.system_interface.presence import utc_now
from imbue.system_interface.profiles import ProfileResolver
from imbue.system_interface.recovery_page import DESKTOPS_RECOVERY_PATH
from imbue.system_interface.recovery_page import INTERFACE_NOT_BUILT_PAGE
from imbue.system_interface.recovery_page import INTERFACE_NOT_BUILT_REPAIR_ARGV
from imbue.system_interface.recovery_page import INTERFACE_NOT_BUILT_REPAIR_MESSAGE
from imbue.system_interface.recovery_page import INTERFACE_NOT_BUILT_REPAIR_MNGR_COMMAND
from imbue.system_interface.recovery_page import RECOVERY_LAUNCH_PATH
from imbue.system_interface.recovery_page import render_recovery_page
from imbue.system_interface.server import THEME_GALLERY_FILENAME
from imbue.system_interface.server import _handle_client_state_message
from imbue.system_interface.server import create_application
from imbue.system_interface.shell.clients import CLIENT_RETENTION
from imbue.system_interface.shell.desktops import DESKTOPS_FILENAME
from imbue.system_interface.shell.launches import LaunchPostOutcome
from imbue.system_interface.shell.testing import DesktopStoreWhoseReadFails
from imbue.system_interface.shell.testing import TEST_PAGE_ID
from imbue.system_interface.shell.testing import client_report
from imbue.system_interface.shell.testing import drain_messages
from imbue.system_interface.shell.testing import identity_headers
from imbue.system_interface.shell.testing import recovery_shell
from imbue.system_interface.shell.tour import ONBOARDING_MEDIA_ORIGIN
from imbue.system_interface.testing import build_test_state
from imbue.system_interface.testing import close_ws
from imbue.system_interface.testing import open_ws
from imbue.system_interface.testing import serve_app
from imbue.system_interface.testing import words_a_shell_splits
from imbue.system_interface.ws_broadcaster import ConnectionRegistration
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster

# Generous: the first receive occasionally exceeded the previous 5.0s cap on a
# loaded machine (~1-in-8 locally, failing as ``json.loads(None)``) even though
# passing runs complete in well under a second -- the wait is pure scheduling
# delay, so a bigger cap costs nothing when healthy.
_WS_RECEIVE_TIMEOUT = 15.0


@pytest.fixture
def config() -> Config:
    return Config()


@pytest.fixture
def app(config: Config) -> Flask:
    return create_application(build_test_state(config=config))


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    return app.test_client()


def _moving_report(client_id: str, desktop_id: str) -> dict[str, Any]:
    """A window's ``client_state`` message moving its client, as the page sends it."""
    return {
        "type": "client_state",
        "client_id": client_id,
        "active_desktop": desktop_id,
        "page_id": TEST_PAGE_ID,
        "revision": 0,
    }


def test_index_returns_html_when_static_exists(client: FlaskClient, tmp_path: Path) -> None:
    """When the static dir has index.html, the server serves it."""
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html><body>test</body></html>")

    state = build_test_state()
    state.static_directory = static_dir
    test_client = create_application(state).test_client()
    response = test_client.get("/")
    assert response.status_code == 200
    assert "test" in response.text
    # Both the app and the placeholder are HTTP 200 HTML, so the header is
    # the only thing that distinguishes them to a health check.
    assert response.headers[FRONTEND_BUILT_HEADER] == "true"


_AGENT_ID = "agent-0123456789abcdef0123456789abcdef"


def _named_workspace_app(tmp_path: Path, index_html: str, display_name: str) -> Flask:
    """A shell over a built page and a mngr host directory whose services agent is labelled ``display_name``."""
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text(index_html)
    agent_record = tmp_path / "host" / "agents" / _AGENT_ID / "data.json"
    agent_record.parent.mkdir(parents=True)
    agent_record.write_text(json.dumps({"labels": {"workspace_display_name": display_name}}))
    state = build_test_state(
        static_directory=static_dir,
        workspace_environ={"MNGR_HOST_DIR": str(tmp_path / "host"), "MNGR_AGENT_ID": _AGENT_ID},
    )
    return create_application(state)


# The shape the frontend build's page has, with the tags the build itself carries.
_BUILT_INDEX = (
    "<!doctype html><html><head>"
    '<meta charset="UTF-8" />'
    '<meta name="viewport" content="width=device-width, initial-scale=1.0" />'
    "<title>System Interface</title>"
    "</head><body></body></html>"
)


def test_the_page_carries_the_workspaces_name_and_what_a_phone_saves_to_its_home_screen(tmp_path: Path) -> None:
    app = _named_workspace_app(tmp_path, _BUILT_INDEX, 'Tom & Jerry\'s <"Lab">')
    client = app.test_client()

    page = client.get("/").text
    prefixed = client.get("/", base_url="http://localhost/shell").text

    escaped = html.escape('Tom & Jerry\'s <"Lab">', quote=True)
    assert '<title>Tom &amp; Jerry\'s &lt;"Lab"&gt;</title>' in page
    assert page.count("<title>") == 1
    assert f'<meta name="apple-mobile-web-app-title" content="{escaped}">' in page
    assert '<meta name="theme-color" content="#fafaf8">' in page
    assert (
        '<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover, maximum-scale=1">'
        in page
    )
    assert page.count('name="viewport"') == 1
    assert '<link rel="apple-touch-icon" href="/apple-touch-icon.png">' in page
    assert '<link rel="manifest" href="/manifest.webmanifest" crossorigin="use-credentials">' in page
    assert '<link rel="apple-touch-icon" href="/shell/apple-touch-icon.png">' in prefixed
    assert '<link rel="manifest" href="/shell/manifest.webmanifest" crossorigin="use-credentials">' in prefixed
    assert client.get("/api/inventory").get_json()["workspace_name"] == 'Tom & Jerry\'s <"Lab">'


def test_tags_the_build_already_carries_are_replaced_rather_than_repeated(tmp_path: Path) -> None:
    built = (
        "<html><head>"
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover, maximum-scale=5">'
        '<meta name="theme-color" content="#000000">'
        '<link rel="manifest" href="/stale.webmanifest">'
        "</head><body></body></html>"
    )
    page = _named_workspace_app(tmp_path, built, "Lab").test_client().get("/").text

    assert page.count('name="viewport"') == 1
    assert 'content="width=device-width, initial-scale=1, viewport-fit=cover, maximum-scale=1"' in page
    assert page.count('name="theme-color"') == 1 and "#000000" not in page
    assert page.count('rel="manifest"') == 1 and "stale" not in page
    assert page.count("<title>") == 1 and "<title>Lab</title>" in page


def test_the_touch_icon_and_the_manifest_follow_the_selected_avatar_and_the_workspaces_name(tmp_path: Path) -> None:
    app = _named_workspace_app(tmp_path, _BUILT_INDEX, "Research Lab")
    client = app.test_client()
    shell = state_of(app).shell

    default_icon = client.get("/apple-touch-icon.png")
    assert default_icon.status_code == 200 and default_icon.mimetype == "image/png"
    assert png_size(default_icon.data) == (180, 180)
    assert default_icon.data == client.get("/api/avatars/imbue-character/icon.png?size=180").data
    shell.avatar_selection.write(DesignId("jelly-cat"))
    assert client.get("/apple-touch-icon.png").data == client.get("/api/avatars/jelly-cat/icon.png").data
    # A selection naming a design the catalog no longer holds shows the default, as the avatar does.
    shell.avatar_selection.write(DesignId("gone"))
    assert client.get("/apple-touch-icon.png").data == default_icon.data

    shell.avatar_selection.write(DesignId("jelly-cat"))
    response = client.get("/manifest.webmanifest", base_url="http://localhost/shell")
    assert response.status_code == 200 and response.mimetype == "application/manifest+json"
    assert json.loads(response.data) == {
        "name": "Research Lab",
        "short_name": "Research Lab",
        "start_url": "/shell/",
        "scope": "/shell/",
        "display": "standalone",
        "background_color": "#fafaf8",
        "theme_color": "#fafaf8",
        "icons": [
            {
                "src": f"/shell/api/avatars/jelly-cat/icon.png?size={size}",
                "sizes": f"{size}x{size}",
                "type": "image/png",
            }
            for size in (180, 192, 512)
        ],
    }
    assert json.loads(client.get("/manifest.webmanifest").data)["start_url"] == "/"


def test_the_shell_refuses_a_page_of_another_origin_and_a_header_less_request_off_loopback(
    client: FlaskClient,
) -> None:
    """The workspace's own browser is on loopback too, so a header-less request is the owner's only from loopback
    under a loopback Host, and a request the browser marks as another origin's never reaches a route; the shell's
    frame of its own page is a navigation and passes, as does a request a proxy stamped."""
    assert client.get("/api/health").status_code == 200
    assert_request_guard_refuses_unknown_provenance(client, "GET", "/api/health")
    framed = client.get("/api/health", headers={"Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "navigate"})
    assert framed.status_code == 200
    unreadable = client.post("/api/presence/heartbeat", json={}, headers={"X-Imbue-Identity": "{not json"})
    assert unreadable.status_code == 400
    assert "X-Imbue-Identity" in unreadable.get_json()["detail"]


@pytest.mark.parametrize("basename", ["app_contract.js", "context_menu.js"])
def test_the_shell_serves_its_browser_side_modules_for_the_stub_pages(tmp_path: Path, basename: str) -> None:
    static_dir = tmp_path / "static"
    (static_dir / "_static").mkdir(parents=True)
    state = build_test_state()
    state.static_directory = static_dir
    test_client = create_application(state).test_client()
    assert test_client.get(f"/_static/{basename}").status_code == 404
    (static_dir / "_static" / basename).write_text("export const built = true;\n")
    response = test_client.get(f"/_static/{basename}")
    assert response.status_code == 200
    assert response.mimetype == "text/javascript"
    assert response.headers["Access-Control-Allow-Origin"] == "*"
    assert b"built" in response.data
    # Meant for a stub page of another origin: the one kind of cross-origin request the shell admits.
    imported = test_client.get(f"/_static/{basename}", headers=CROSS_SITE_FETCH_HEADERS)
    assert imported.status_code == 200


def test_a_preview_shells_page_says_so_and_carries_no_staleness_banner(tmp_path: Path) -> None:
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html><head></head><body>test</body></html>")

    state = build_test_state(is_preview=True)
    state.static_directory = static_dir
    response = create_application(state).test_client().get("/")

    assert response.status_code == 200
    assert 'name="system-interface-preview"' in response.text
    assert 'content="true"' in response.text
    assert "system-interface-update-staleness" not in response.text


def test_index_is_served_uncacheable(client: FlaskClient, tmp_path: Path) -> None:
    """The shell must never be cached, or a reload cannot pick up a new build.

    The built assets are content-hashed, so the shell is the only document whose
    freshness decides which bundle a reloaded page runs. A page cannot drop its
    own HTTP cache (``location.reload(true)`` is Firefox-only), so a cacheable
    shell would let a reveal's reload land right back on the old interface --
    including through a shared Cloudflare tunnel, where an intermediary may
    cache anything not marked otherwise.
    """
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html><body>test</body></html>")

    state = build_test_state()
    state.static_directory = static_dir
    test_client = create_application(state).test_client()
    response = test_client.get("/")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"


def test_an_unknown_path_falls_through_to_the_shell_document(tmp_path: Path) -> None:
    """A path the shell does not serve is a client-side route: it answers the shell document, not a 404."""
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html><body>the shell</body></html>")

    state = build_test_state()
    state.static_directory = static_dir
    test_client = create_application(state).test_client()
    response = test_client.get("/some/client/route")

    assert response.status_code == 200
    assert "text/html" in response.content_type
    assert "the shell" in response.text


def test_the_built_page_and_the_placeholder_are_served_under_the_document_policy(tmp_path: Path) -> None:
    """The desktop (a built document with no inline script) and the placeholder (one inline script) both carry a
    policy a browser would run them under; the shell's frames of other apps and its socket stay admitted."""
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    state = build_test_state()
    state.static_directory = static_dir
    test_client = create_application(state).test_client()

    placeholder = test_client.get("/")
    (static_dir / "index.html").write_text(
        '<html><head><script src="/assets/index.js"></script></head><body></body></html>'
    )
    desktop = test_client.get("/")

    assert placeholder.headers[FRONTEND_BUILT_HEADER] == "false"
    assert desktop.headers[FRONTEND_BUILT_HEADER] == "true"
    for page in (placeholder, desktop):
        assert_document_policy_admits_page(page)
        policy = page.headers["Content-Security-Policy"]
        assert "frame-src http: https:" in policy
        # The tour's pictures are hosted, so the page admits images from their origin.
        assert "img-src 'self' data: blob: https://onboarding-media.imbueminds.com" in policy
        assert "connect-src" not in policy
        assert "frame-ancestors" not in policy
    assert "Content-Security-Policy" not in test_client.get("/api/health").headers


def test_index_marks_the_not_built_placeholder_as_not_the_app(tmp_path: Path) -> None:
    """The placeholder and the real app are both HTTP 200 HTML.

    Only the header tells them apart, and the reveal flow's frontend probe
    decides whether to roll back on it -- a placeholder that claimed to be the
    app would let a reveal sign off on a UI the user cannot see.
    """
    empty_dir = tmp_path / "static"
    empty_dir.mkdir()

    state = build_test_state()
    state.static_directory = empty_dir
    test_client = create_application(state).test_client()
    response = test_client.get("/")

    assert response.status_code == 200
    assert response.headers[FRONTEND_BUILT_HEADER] == "false"
    # The page keeps asking whether the bundle is back, which is the only thing
    # that returns an open tab to the interface once something else restores it
    # -- nothing on the page can produce one, and nothing notifies it.
    assert FRONTEND_BUILT_HEADER in response.text


def test_the_theme_gallery_serves_its_own_page_and_the_placeholder_until_it_is_built(tmp_path: Path) -> None:
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text(_BUILT_INDEX)
    client = create_application(build_test_state(static_directory=static_dir)).test_client()

    unbuilt = client.get(THEME_GALLERY_ROUTE)
    (static_dir / THEME_GALLERY_FILENAME).write_text("<html><head><title>Theme gallery</title></head></html>")
    built = client.get(THEME_GALLERY_ROUTE, base_url="http://localhost/shell")

    assert unbuilt.headers[FRONTEND_BUILT_HEADER] == "false"
    assert built.headers[FRONTEND_BUILT_HEADER] == "true"
    assert "<title>Theme gallery</title>" in built.text
    assert f'<meta name="{BASE_PATH_META_NAME}" content="/shell">' in built.text


def test_not_built_placeholder_polls_rather_than_refreshing_the_whole_page(tmp_path: Path) -> None:
    """The reader's terminal must survive the wait for a bundle.

    Returning to the interface unattended and hosting a live shell pull against
    each other: a whole-page refresh on a timer would tear down the terminal
    session every few seconds, right while it is being typed into. So the
    scripted page asks for the app-shell marker and reloads only once it says
    the bundle is back. A page-level ``http-equiv="refresh"`` may therefore
    appear only inside ``<noscript>``, where there is no terminal to protect.
    """
    empty_dir = tmp_path / "static"
    empty_dir.mkdir()

    state = build_test_state()
    state.static_directory = empty_dir
    test_client = create_application(state).test_client()
    response = test_client.get("/")

    scriptless_only = re.sub(r"<noscript>.*?</noscript>", "", response.text, flags=re.DOTALL)
    assert 'http-equiv="refresh"' not in scriptless_only
    assert 'http-equiv="refresh"' in response.text
    # HEAD, because the marker is a header: the poll must not pull the page's
    # own body down every tick for the lifetime of the outage.
    assert '"HEAD"' in response.text


def test_not_built_placeholder_offers_the_registered_terminal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The way out of a missing interface is a shell, and the page has to name it.

    The terminal's origin label is minted per workspace, so the page cannot
    carry it -- it is read from the app registry at render time and handed to
    the script, which derives the origin from the browser's own location. If
    the label never reaches the page there is no frame to open, and the reader
    is back to prose about a repair they cannot perform here.
    """
    apps_file = tmp_path / "apps.toml"
    apps_file.write_text('[[apps]]\nname = "terminal"\nurl = "http://localhost:7681"\nlabel = "terminal-x7k9q2w1"\n')
    monkeypatch.setenv("MINDS_APPS_FILE", str(apps_file))
    empty_dir = tmp_path / "static"
    empty_dir.mkdir()

    state = build_test_state()
    state.static_directory = empty_dir
    # What ``ShellState.start`` does for the served app: read the registry once.
    state.shell.inventory.reload_registry()
    test_client = create_application(state).test_client()
    response = test_client.get("/")

    assert '"terminal-x7k9q2w1"' in response.text
    assert 'id="terminal"' in response.text


def test_not_built_placeholder_renders_without_a_terminal_to_offer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A workspace with no registered terminal still gets a usable page.

    The terminal app registers itself alongside the other apps rather than before
    them, so the placeholder can be served in the window where there is nothing to
    offer -- and this page exists precisely for states where things are missing.
    It must degrade to the prose rather than fail to render or show an empty
    frame pointed at nowhere.
    """
    apps_file = tmp_path / "apps.toml"
    apps_file.write_text('[[apps]]\nname = "browser"\nurl = "http://localhost:8081"\nlabel = "browser-aaaa1111"\n')
    monkeypatch.setenv("MINDS_APPS_FILE", str(apps_file))
    empty_dir = tmp_path / "static"
    empty_dir.mkdir()

    state = build_test_state()
    state.static_directory = empty_dir
    test_client = create_application(state).test_client()
    response = test_client.get("/")

    assert response.status_code == 200
    assert response.headers[FRONTEND_BUILT_HEADER] == "false"
    # The empty label is what the script reads as "no terminal", so the frame
    # stays hidden instead of loading a made-up origin.
    assert 'var terminalLabel = "";' in response.text
    assert "needs to be rebuilt" in response.text

    # The shell prefix is not part of the argv the CLI validates, but it is what
    # makes the connect half work from the workspace's own tmux-backed terminals.
    assert INTERFACE_NOT_BUILT_PAGE.repair_command == "env -u TMUX " + INTERFACE_NOT_BUILT_REPAIR_MNGR_COMMAND


def test_not_built_repair_message_names_the_heading_the_reader_is_looking_at_behind_the_assist_command() -> None:
    """What the message names has to be what the page says, or it names nothing.

    The message's whole claim on the agent's attention is that it repeats the line the reader is looking at, so the
    two are one statement written twice. Nothing else notices when they part: reword the heading and the message
    still parses and still validates against the CLI, naming a sentence that now appears nowhere. It is worded as
    every ask of the desktop's is, Imbue Studio telling the agent what the user was shown, and it starts with the
    assist command, which a chat runs only at the start of its first message.
    """
    message = INTERFACE_NOT_BUILT_REPAIR_ARGV[INTERFACE_NOT_BUILT_REPAIR_ARGV.index("--message") + 1]
    heading = re.search(r"<h1>(.*?)</h1>", render_recovery_page(INTERFACE_NOT_BUILT_PAGE, None, True, ""), re.DOTALL)
    assert heading is not None, "the page no longer carries a heading"

    assert message.startswith("/assist ")
    assert f"Imbue Studio showed the user this failure: {heading.group(1).strip()}." in message
    assert message == INTERFACE_NOT_BUILT_REPAIR_MESSAGE


def _repair_line_shown_on(page: str) -> str:
    """The repair line as the page's own markup hands it to the reader.

    Undoing the escaping is what the browser does to fill ``textContent``, which
    is both what a reader sees in the block and what the copy button puts on the
    clipboard, so this is the line the page actually offers.
    """
    shown = re.search(r'<pre id="repair-command">(.*?)</pre>', page, re.DOTALL)
    assert shown is not None, "the page no longer carries a repair-command block"
    return html.unescape(shown.group(1))


def test_not_built_repair_command_reaches_the_page_as_text_not_markup() -> None:
    """The suggested line is prose, so the page has to render it as written.

    It carries a ``--message`` a maintainer will reword, and a browser reads an
    ``&`` in it as the start of an entity reference and a ``<`` as the start of
    a tag. Either would show a line other than the one the tests validated, and
    the copy button reads ``textContent``, so it would put that other line on
    the reader's clipboard.
    """
    content = INTERFACE_NOT_BUILT_PAGE.model_copy_update(
        to_update(INTERFACE_NOT_BUILT_PAGE.field_ref().repair_command, 'mngr create --message "a & b <c>"')
    )
    page = render_recovery_page(content, None, True, "")

    assert 'mngr create --message "a &amp; b &lt;c&gt;"' in page
    assert "<c>" not in page

    # And the escaping has to be transparent to the line that ships: undoing it
    # is what the browser does to fill ``textContent``, so this is the line the
    # reader reads and copies, and it has to be the one the CLI check and the
    # shell split validated. The assertions above only show that escaping
    # happens; this is what says the real command survives it.
    shown = _repair_line_shown_on(render_recovery_page(INTERFACE_NOT_BUILT_PAGE, None, True, ""))
    assert shown == INTERFACE_NOT_BUILT_PAGE.repair_command


def test_not_built_repair_line_splits_the_way_a_shell_splits_it() -> None:
    """The argv the CLI validates has to be the argv the reader's shell builds.

    The readable line is the source of truth and the argv is parsed back out of
    it, which is only sound while the parse agrees with a shell's. ``shlex.split``
    quotes and splits but expands nothing, so a ``$`` or a backtick worded into
    the message -- prose, and prose gets reworded -- would reach the argv as
    itself, leaving the sentence assertion and the live-CLI check above both
    green while the line a reader copies tells the agent something else.

    So the split is checked against a real shell rather than assumed to match
    one. ``set --`` keeps the flags from being read as options to ``set`` and
    keeps the line's first word from being run as a command -- but the words are
    still expanded on the way in, which is how a ``$`` is caught here. Command
    substitution is an expansion too, and that one would be *run* rather than
    reported, so it is refused before a shell ever sees the line.
    """
    for substitution in ("`", "$("):
        assert substitution not in INTERFACE_NOT_BUILT_REPAIR_MNGR_COMMAND, (
            f"the suggested line contains a command substitution ({substitution}), which the shell below "
            "would execute rather than report: word it out of the message"
        )

    printed_words = subprocess.run(
        ["sh", "-c", f'set -- {INTERFACE_NOT_BUILT_REPAIR_MNGR_COMMAND}\nprintf "%s\\n" "$@"'],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert printed_words.stdout.splitlines() == list(INTERFACE_NOT_BUILT_REPAIR_ARGV)


def test_the_not_built_page_asks_an_agent_through_the_primary_text_action_and_opens_a_terminal_session(
    tmp_path: Path,
) -> None:
    """The placeholder's "Ask an agent to fix this" sends the repair message through the launcher's primary text
    action, the main chat rather than a new one, and answers where that chat's page is; its "See details" terminal is
    a session of its own."""
    app, posts = recovery_shell(
        tmp_path,
        [
            LaunchPostOutcome(status_code=200, body={"path": "/?chat=agent-1"}),
            LaunchPostOutcome(status_code=500, body={"detail": "boom"}),
        ],
    )
    client = app.test_client()
    page = client.get("/")
    assert page.headers[FRONTEND_BUILT_HEADER] == "false"
    assert "var isAgentOffered = true;" in page.text

    asked = client.post(RECOVERY_LAUNCH_PATH, json={"reason": "interface_not_built", "purpose": "repair_agent"})
    assert asked.status_code == 200, asked.get_json()
    assert asked.get_json() == {"label": "helper-y2", "path": "/?chat=agent-1", "display_name": "Helper"}
    (post,) = posts
    assert post.url == "http://localhost:7002/api/intake"
    assert post.body == {"target": "main_chat", "message": INTERFACE_NOT_BUILT_REPAIR_MESSAGE}

    # A launch that fails answers as the launch route does: the app's own words, and no sentence of the shell's, for
    # the page to word.
    failed = client.post(RECOVERY_LAUNCH_PATH, json={"reason": "interface_not_built", "purpose": "repair_agent"})
    assert failed.status_code == 502
    assert failed.get_json() == {"detail": "helper answered the launch with 500: boom"}

    terminal = client.post(RECOVERY_LAUNCH_PATH, json={"reason": "interface_not_built", "purpose": "terminal"})
    assert terminal.status_code == 200, terminal.get_json()
    assert terminal.get_json() == {"label": "terminal-x1", "path": "/new", "display_name": "Terminal"}


def test_the_not_built_page_offers_no_agent_without_an_app_to_ask_and_a_preview_asks_nothing(tmp_path: Path) -> None:
    app, posts = recovery_shell(tmp_path / "bare", [], is_chat_registered=False)
    client = app.test_client()
    assert "var isAgentOffered = false;" in client.get("/").text
    assert (
        client.post(
            RECOVERY_LAUNCH_PATH, json={"reason": "interface_not_built", "purpose": "repair_agent"}
        ).status_code
        == 404
    )

    preview, preview_posts = recovery_shell(tmp_path / "preview", [], is_preview=True)
    refused = preview.test_client().post(
        RECOVERY_LAUNCH_PATH, json={"reason": "interface_not_built", "purpose": "repair_agent"}
    )
    assert refused.status_code == 403
    assert refused.get_json()["user_facing_message"] == refused.get_json()["detail"]
    assert posts == [] and preview_posts == []


def _without_temp_names(text: str) -> str:
    """``text`` with the random names of the shell's temporary state files masked, which each failed write mints."""
    return re.sub(r"\.tmp-[0-9a-f]{32}", ".tmp-*", text)


def _launch_message_on(page: str, working_directory: Path) -> str:
    """The ``--message`` of the repair line a page offers, as a shell hands it to mngr."""
    words = words_a_shell_splits(_repair_line_shown_on(page), working_directory)
    return words[words.index("--message") + 1]


def test_the_desktops_recovery_page_says_what_the_read_of_the_desktops_raised_and_asks_an_agent_with_it(
    tmp_path: Path,
) -> None:
    """The page reads the desktops itself, so what it shows is the shell's own failure, the one the inventory route
    answered the frontend with; its agent and its command are told that failure behind ``/assist``."""
    app, posts = recovery_shell(tmp_path, [LaunchPostOutcome(status_code=200, body={"path": "/?chat=agent-2"})])
    # A directory where the desktops file should be: the read cannot seed the first desktop.
    (tmp_path / "state" / DESKTOPS_FILENAME).mkdir(parents=True)
    client = app.test_client()
    inventory = client.get("/api/inventory")
    assert inventory.status_code == 500
    detail = inventory.get_json()["detail"]

    page = client.get(DESKTOPS_RECOVERY_PATH)

    assert page.status_code == 200
    assert page.headers[FRONTEND_BUILT_HEADER] == "false"
    assert "<h1>This workspace's desktops couldn't be loaded.</h1>" in page.text
    shown_details = re.search(r'<pre id="failure-details">(.*?)</pre>', page.text, re.DOTALL)
    assert shown_details is not None
    assert _without_temp_names(html.unescape(shown_details.group(1))) == _without_temp_names(detail)
    assert ">Ask an agent to fix this</button>" in page.text
    assert 'id="reload"' in page.text
    assert "var isAgentOffered = true;" in page.text
    assert 'var reason = "desktops_not_loaded";' in page.text
    # The page polls the read the frontend boots from.
    assert 'fetch("/api/inventory"' in page.text

    asked = client.post(RECOVERY_LAUNCH_PATH, json={"reason": "desktops_not_loaded", "purpose": "repair_agent"})
    assert asked.status_code == 200, asked.get_json()
    assert asked.get_json() == {"label": "helper-y2", "path": "/?chat=agent-2", "display_name": "Helper"}
    (post,) = posts
    assert post.body["target"] == "main_chat"
    assert _without_temp_names(post.body["message"]) == _without_temp_names(
        "/assist Imbue Studio showed the user this failure: This workspace's desktops couldn't be loaded.\n\n"
        f"Details:\n```\n{detail}\n```\n\n"
        "Find out why it failed and fix it, then tell the user what was wrong and what you changed."
    )
    assert _without_temp_names(_launch_message_on(page.text, tmp_path)) == _without_temp_names(post.body["message"])
    terminal = client.post(RECOVERY_LAUNCH_PATH, json={"reason": "desktops_not_loaded", "purpose": "terminal"})
    assert terminal.get_json() == {"label": "terminal-x1", "path": "/new", "display_name": "Terminal"}


def test_the_desktops_recovery_page_goes_back_to_the_desktop_once_they_load_and_asks_no_agent_about_them(
    tmp_path: Path,
) -> None:
    app, posts = recovery_shell(tmp_path, [])
    unreadable = tmp_path / "state" / DESKTOPS_FILENAME
    unreadable.mkdir(parents=True)
    client = app.test_client()
    assert client.get(DESKTOPS_RECOVERY_PATH).status_code == 200

    unreadable.rmdir()

    # Back to the desktop, keeping the query a pulled-out window's own page is opened with, under the shell's prefix.
    loaded = client.get(f"{DESKTOPS_RECOVERY_PATH}?solo=win-1", base_url="http://localhost/shell")
    assert loaded.status_code == 302
    assert loaded.headers["Location"] == "/shell/?solo=win-1"
    assert client.get(DESKTOPS_RECOVERY_PATH).headers["Location"] == "/"
    # A page still open asks about a failure that has gone: there is nothing to ask, and the page is told so.
    refused = client.post(RECOVERY_LAUNCH_PATH, json={"reason": "desktops_not_loaded", "purpose": "repair_agent"})
    assert refused.status_code == 409
    assert refused.get_json()["user_facing_message"] == "This workspace's desktops load now."
    assert posts == []


def test_the_desktops_recovery_page_on_a_full_disk_says_so_and_asks_an_agent_to_free_up_space(tmp_path: Path) -> None:
    app, posts = recovery_shell(
        tmp_path,
        [LaunchPostOutcome(status_code=200, body={"path": "/?chat=agent-3"})],
        desktops=DesktopStoreWhoseReadFails(state_directory=tmp_path / "state", failing_errno=errno.ENOSPC),
    )
    client = app.test_client()
    assert client.get("/api/inventory").get_json()["kind"] == "out_of_space"

    page = client.get(DESKTOPS_RECOVERY_PATH)

    assert "<h1>This workspace is out of space, so its desktops couldn't be loaded.</h1>" in page.text
    assert ">Ask an agent to free up space</button>" in page.text
    assert (
        client.post(
            RECOVERY_LAUNCH_PATH, json={"reason": "desktops_not_loaded", "purpose": "repair_agent"}
        ).status_code
        == 200
    )
    (post,) = posts
    assert post.body["message"].startswith(
        "/assist Imbue Studio showed the user this failure: This workspace is out of space, so its desktops couldn't "
        "be loaded.\n\nDetails:\n```\ncannot write shell state file "
    )
    assert _launch_message_on(page.text, tmp_path) == post.body["message"]


def test_the_desktops_recovery_page_shows_an_unexpected_error_with_its_traceback(tmp_path: Path) -> None:
    """An error that is no refusal of the shell's is the inventory route's bare 500, and the page shows where it was
    raised, which is what an agent needs to fix it."""
    app, _posts = recovery_shell(
        tmp_path,
        [],
        desktops=DesktopStoreWhoseReadFails(state_directory=tmp_path / "state", failing_bug="the record lost its id"),
    )
    client = app.test_client()
    assert client.get("/api/inventory").get_json()["detail"] == "Internal server error: the record lost its id"

    page = client.get(DESKTOPS_RECOVERY_PATH)

    assert "<h1>This workspace's desktops couldn't be loaded.</h1>" in page.text
    shown_details = re.search(r'<pre id="failure-details">(.*?)</pre>', page.text, re.DOTALL)
    assert shown_details is not None
    details = html.unescape(shown_details.group(1))
    assert details.startswith("Traceback (most recent call last):\n")
    assert "in ensure_default" in details
    assert details.endswith("DesktopsReadBugError: the record lost its id")


def test_the_recovery_launch_asks_nothing_from_a_preview_and_no_agent_without_an_app_to_ask(tmp_path: Path) -> None:
    preview, preview_posts = recovery_shell(tmp_path / "preview", [], is_preview=True)
    (tmp_path / "preview" / "state" / DESKTOPS_FILENAME).mkdir(parents=True)
    refused = preview.test_client().post(
        RECOVERY_LAUNCH_PATH, json={"reason": "desktops_not_loaded", "purpose": "repair_agent"}
    )
    assert refused.status_code == 403

    bare, bare_posts = recovery_shell(tmp_path / "bare", [], is_chat_registered=False)
    (tmp_path / "bare" / "state" / DESKTOPS_FILENAME).mkdir(parents=True)
    client = bare.test_client()
    assert "var isAgentOffered = false;" in client.get(DESKTOPS_RECOVERY_PATH).text
    unoffered = client.post(RECOVERY_LAUNCH_PATH, json={"reason": "desktops_not_loaded", "purpose": "repair_agent"})
    assert unoffered.status_code == 404
    assert preview_posts == [] and bare_posts == []


def test_assets_404_rather_than_falling_through_to_the_spa_shell(tmp_path: Path) -> None:
    """A missing asset must 404, never come back as the SPA shell.

    The catch-all would answer with index.html as text/html, which the browser
    refuses as a module script -- a blank screen with no hint of the cause.
    """
    empty_dir = tmp_path / "static"
    empty_dir.mkdir()

    state = build_test_state()
    state.static_directory = empty_dir
    test_client = create_application(state).test_client()
    response = test_client.get("/assets/index-abc123.js")

    assert response.status_code == 404
    # The app-shell marker is absent, proving the request did not reach the
    # catch-all and come back as index.html with a 200.
    assert FRONTEND_BUILT_HEADER not in response.headers


def test_assets_do_not_reveal_whether_files_outside_the_directory_exist(tmp_path: Path) -> None:
    """A ``..`` path must get the same plain 404 whether or not its target exists.

    Flask's ``<path:>`` converter passes ``..`` segments through unnormalized, so
    any pre-check that joins the raw filename onto the assets directory stats
    paths outside it -- and a response that differs between an existing and a
    missing target is an existence oracle for the whole filesystem.
    """
    static_dir = tmp_path / "static"
    (static_dir / "assets").mkdir(parents=True)
    (static_dir / "index.html").write_text("<html>app</html>")

    state = build_test_state()
    state.static_directory = static_dir
    test_client = create_application(state).test_client()
    # index.html exists one level above assets/; a file two levels up does not.
    exists_outside = test_client.get("/assets/../index.html")
    missing_outside = test_client.get("/assets/../../no-such-file")

    for response in (exists_outside, missing_outside):
        assert response.status_code == 404
        assert response.data == b""


def test_assets_serve_a_bundle_that_appeared_after_startup(tmp_path: Path) -> None:
    """The route must survive being constructed before the bundle exists.

    Deciding at construction time whether to register it turned a recoverable
    state into a stuck one: rebuilding no longer helped until a restart.
    """
    static_dir = tmp_path / "static"
    static_dir.mkdir()

    state = build_test_state()
    state.static_directory = static_dir
    # App built while there is no bundle at all, as it is on a cold start
    # into a wiped tree.
    test_client = create_application(state).test_client()
    (static_dir / "assets").mkdir()
    (static_dir / "assets" / "index-abc123.js").write_text("console.log('app');")
    response = test_client.get("/assets/index-abc123.js")

    assert response.status_code == 200
    assert "javascript" in response.headers["Content-Type"]


def test_http_errors_keep_their_status_codes(client: FlaskClient) -> None:
    """Routing-level HTTP errors pass through the unhandled-exception handler intact.

    Regression: the handler re-raised HTTPExceptions, which re-entered Flask's
    handle_exception and surfaced every 404/405 as a 500 (observed live on a
    method-not-allowed destroy call).
    """
    assert client.post("/api/definitely-not-a-route").status_code == 405
    assert client.put("/api/layout/broadcast").status_code == 405


def test_a_page_that_fails_to_load_shows_a_sentence_and_a_try_again_and_a_call_keeps_its_json(
    tmp_path: Path,
) -> None:
    """The workspace frame gets a page of ours, the exception behind "See details", rather than raw JSON; a caller
    that reads JSON still gets the developer's reason."""
    static_dir = tmp_path / "static"
    # A shell document that exists but cannot be read.
    (static_dir / "index.html").mkdir(parents=True)
    state = build_test_state()
    state.static_directory = static_dir
    test_client = create_application(state).test_client()

    page = test_client.get("/", headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})

    assert page.status_code == 500 and page.mimetype == "text/html"
    assert "This workspace couldn't load. Try again. If this persists, report a bug." in page.text
    assert 'id="try-again">Try again</button>' in page.text
    # Try again is bound from the page's own script, which its policy admits by hash; no inline handler would run.
    script = page.text.split("<script>")[1].split("</script>")[0]
    assert "location.reload()" in script
    assert inline_source(script) in page.headers["Content-Security-Policy"]
    details = page.text.split("<summary>See details</summary><pre>")[1].split("</pre>")[0]
    assert details.startswith("Internal server error: [Errno ") and "index.html" in details
    called = test_client.get("/", headers={"Accept": "*/*"})
    assert called.status_code == 500 and called.get_json()["detail"].startswith("Internal server error: [Errno ")


def test_an_unknown_api_path_is_a_json_404_not_the_app_shell(client: FlaskClient) -> None:
    """The SPA catch-all serves the app shell for any unknown GET, which is right for a
    client-side route and wrong for a caller of the API: a 200 page where JSON was expected
    reads as success to a script and as a parse error to a browser."""
    response = client.get("/api/definitely-not-a-route")
    assert response.status_code == 404
    assert response.get_json()["detail"] == "No such API route: /api/definitely-not-a-route"
    assert client.get("/api").status_code == 404
    # A client-side route still renders the shell.
    assert client.get("/some/client/route").status_code == 200


@pytest.mark.flaky
@pytest.mark.timeout(15)
def test_websocket_endpoint_sends_initial_snapshot(app: Flask) -> None:
    """On connect the socket sends the shell's inventory, desktops, the avatar's status, and the update notice."""
    with serve_app(app) as served:
        ws = open_ws(served, "/api/ws")
        try:
            messages = [json.loads(ws.receive(timeout=_WS_RECEIVE_TIMEOUT)) for _ in range(4)]
        finally:
            close_ws(ws)

    assert [message["type"] for message in messages] == [
        "apps_updated",
        "desktops_updated",
        "avatar_status",
        "update_notice_changed",
    ]
    assert messages[0]["apps"] == []
    assert messages[1]["desktops"] == []
    assert messages[2] == {"type": "avatar_status", "mood": "idle", "is_stale": True}
    assert messages[3]["notice"] is None


@pytest.mark.flaky
@pytest.mark.timeout(15)
def test_websocket_connect_sends_the_chats_activity_once_the_chat_app_has_reported_it(app: Flask) -> None:
    """A window that connects after the chat app's report paints the agent's card from it, after the roster."""
    report = {"chats": [{"chat_id": "agent-1", "title": "Main chat", "status": "idle", "kind": "agent"}]}
    assert app.test_client().post("/api/chats-activity", json=report).status_code == 204
    with serve_app(app) as served:
        ws = open_ws(served, "/api/ws")
        try:
            messages = [json.loads(ws.receive(timeout=_WS_RECEIVE_TIMEOUT)) for _ in range(7)]
        finally:
            close_ws(ws)

    assert [message["type"] for message in messages[4:]] == ["presence_updated", "roster_updated", "chats_activity"]
    assert messages[6] == {"type": "chats_activity", **report}


def test_websocket_connect_ends_with_the_resource_gauge(app: Flask) -> None:
    with serve_app(app) as served:
        ws = open_ws(served, "/api/ws")
        try:
            messages = [json.loads(ws.receive(timeout=_WS_RECEIVE_TIMEOUT)) for _ in range(7)]
        finally:
            close_ws(ws)

    assert messages[6]["type"] == "resource_gauge_changed"
    assert messages[6]["view"]["settings"]["disk_warning_free_gb"] == 5.0
    assert messages[6]["view"]["alerts"] == []


_VISITOR_IDENTITY = RequestIdentity(owner=False, user_id="user-bob-4471", email="bob@example.com")
_OWNER_IDENTITY = RequestIdentity(owner=True, user_id="user-owner-9c21", email="owner@example.com")


def _heartbeat(client: FlaskClient, identity: RequestIdentity | None, body: dict[str, Any] | None = None) -> Any:
    headers = {} if identity is None else identity_headers(identity)
    return client.post("/api/presence/heartbeat", json=body if body is not None else {}, headers=headers)


def _presence_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The ``presence_updated`` messages among ``messages``: the only other thing pushed beside them is the roster
    they changed (a connected set that changed and left the roster as it was pushes no roster)."""
    assert {message["type"] for message in messages} <= {"presence_updated", "roster_updated"}
    return [message for message in messages if message["type"] == "presence_updated"]


def _profile_aware_app(
    tmp_path: Path, broadcaster: WebSocketBroadcaster, connector: Callable[[httpx.Request], httpx.Response]
) -> Flask:
    """A shell whose profile resolver reaches ``connector`` through the broker a share.env names."""
    share_env_path = tmp_path / "share.env"
    share_env_path.write_text('export SHARE_BROKER_URL="https://broker.example.test"\n')
    profiles = ProfileResolver(
        cache_directory=tmp_path / "profiles", share_env_path=share_env_path, transport=httpx.MockTransport(connector)
    )
    return create_application(
        build_test_state(
            broadcaster=broadcaster,
            shell_state_directory=tmp_path / "state",
            repo_root=tmp_path / "repo",
            static_directory=tmp_path / "static",
            presence_directory=tmp_path / "presence",
            profiles=profiles,
        )
    )


def test_a_persons_picture_is_served_from_the_shells_origin_and_cached_by_version(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    """``GET /api/roster/<key>/picture`` (the multiplayer spec, section 4.2) relays the bytes the profile's URL
    serves, immutably cacheable under its current version alone (a stale page asking under an older version, or
    none, gets the same bytes uncached); a key that is not a listed person's (an account the roster does
    not name, a key with no id, the agent's), or a person with no picture, is a 404, and nothing is fetched for it."""
    pictures_fetched: list[str] = []

    def connector(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/users/user-owner-9c21/profile":
            return httpx.Response(
                200, json={"user_id": "user-owner-9c21", "display_name": "Josh", "profile_picture_url": "https://a/b"}
            )
        if request.url.path == "/users/user-bob-4471/profile":
            return httpx.Response(200, json={"user_id": "user-bob-4471", "display_name": "Bob"})
        if request.url.path == "/users/user-carol-1138/profile":
            return httpx.Response(
                200, json={"user_id": "user-carol-1138", "display_name": "Carol", "profile_picture_url": "https://a/c"}
            )
        if request.url.host == "a":
            pictures_fetched.append(str(request.url))
            return httpx.Response(200, content=b"\x89PNG...", headers={"Content-Type": "image/png"})
        return httpx.Response(404, json={"detail": "no such user"})

    client = _profile_aware_app(tmp_path, broadcaster, connector).test_client()
    _heartbeat(client, _OWNER_IDENTITY)

    def owner_entry() -> dict[str, Any]:
        participants = client.get("/api/roster").get_json()["participants"]
        return next(entry for entry in participants if entry["key"] == "user:user-owner-9c21")

    # The roster answers without waiting on the connector; the owner's picture appears once the fetch behind it lands.
    wait_for(
        lambda: owner_entry()["profile_picture_url"] is not None,
        timeout=10.0,
        poll_interval=0.01,
        error_message="the owner's profile never reached the roster",
    )
    owner = owner_entry()
    assert owner["profile_picture_url"].startswith("/api/roster/user:user-owner-9c21/picture?v=")

    served = client.get(owner["profile_picture_url"])
    assert (served.status_code, served.mimetype, served.data) == (200, "image/png", b"\x89PNG...")
    assert served.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    # Outside bytes under the shell's origin are sent as its own images are.
    assert served.headers["X-Content-Type-Options"] == "nosniff"
    assert served.headers["Content-Security-Policy"] == "default-src 'none'; style-src 'unsafe-inline'; sandbox"
    unversioned = client.get("/api/roster/user:user-owner-9c21/picture")
    assert (unversioned.status_code, unversioned.headers["Cache-Control"]) == (200, "no-cache")
    stale = client.get("/api/roster/user:user-owner-9c21/picture?v=0123456789abcdef")
    assert (stale.status_code, stale.data, stale.headers["Cache-Control"]) == (200, b"\x89PNG...", "no-cache")
    assert client.get("/api/roster/user:user-bob-4471/picture").status_code == 404
    assert client.get("/api/roster/user:user-carol-1138/picture").status_code == 404
    assert client.get("/api/roster/user:/picture").status_code == 404
    assert client.get("/api/roster/workspace-agent/picture").status_code == 404
    assert pictures_fetched == ["https://a/b"]
    assert not (tmp_path / "profiles" / "user-carol-1138.picture.json").exists()


def test_presence_heartbeat_records_the_requester_and_answers_their_identity(client: FlaskClient) -> None:
    response = _heartbeat(client, _VISITOR_IDENTITY)

    assert response.status_code == 200
    assert response.get_json() == {
        "identity": {
            "owner": False,
            "user_id": "user-bob-4471",
            "email": "bob@example.com",
            "agent_id": None,
            "agent_name": None,
            "granter": None,
            "is_header_present": True,
        },
        "participant": "user:user-bob-4471",
    }
    listed = client.get("/api/presence").get_json()
    (user,) = listed["users"]
    assert user["user_id"] == "user-bob-4471"
    assert user["owner"] is False
    # No connector to ask, so no profile; the record itself is complete without one.
    assert user["display_name"] is None and user["profile_picture_url"] is None
    assert set(user) == {
        "user_id",
        "email",
        "display_name",
        "profile_picture_url",
        "owner",
        "first_seen",
        "last_seen",
        "last_input_at",
    }


def test_presence_heartbeat_ignores_whatever_body_an_older_page_still_sends(client: FlaskClient) -> None:
    response = client.post(
        "/api/presence/heartbeat", json={"session_id": "tab-0001-aaaa"}, headers=identity_headers(_VISITOR_IDENTITY)
    )
    assert response.status_code == 200
    # A page from before the body was sent posts none at all.
    bodiless = client.post("/api/presence/heartbeat", headers=identity_headers(_VISITOR_IDENTITY))
    assert bodiless.status_code == 200
    assert bodiless.get_json()["participant"] == "user:user-bob-4471"


def test_presence_heartbeat_records_the_owner_of_an_unshared_workspace_under_the_owner_key(
    client: FlaskClient,
) -> None:
    assert _heartbeat(client, None).get_json()["participant"] == "owner"
    assert _heartbeat(client, RequestIdentity(owner=True)).get_json()["participant"] == "owner"
    # The owner has no account to list among the connected accounts; the roster carries them instead.
    assert client.get("/api/presence").get_json() == {"users": []}
    roster = client.get("/api/roster").get_json()["participants"]
    assert [(entry["key"], entry["is_connected"], entry["is_owner"]) for entry in roster] == [
        ("owner", True, True),
        ("workspace-agent", True, False),
    ]


def test_a_present_identity_header_that_does_not_parse_is_refused_by_every_route(client: FlaskClient) -> None:
    """A header no proxy could have stamped is nobody's: the request is refused rather than read as the owner's
    (the multiplayer spec, D10), on the shell's own routes and the layout routes alike."""
    unreadable = {"X-Imbue-Identity": '{"owner": "false"}'}
    heartbeat = client.post("/api/presence/heartbeat", json={}, headers=unreadable)
    assert heartbeat.status_code == 400
    assert "X-Imbue-Identity" in heartbeat.get_json()["detail"]
    assert client.get("/api/presence").get_json() == {"users": []}
    assert client.post("/api/desktops", json={"name": "Mine"}, headers=unreadable).status_code == 400
    # A blank header is a missing one: the anonymous owner.
    assert _heartbeat(client, None).get_json()["participant"] == "owner"


def test_presence_heartbeat_records_nothing_for_a_visiting_agent(client: FlaskClient, app: Flask) -> None:
    """Neither presence nor, whatever windows the body reports, a ``window_input`` event."""
    agent = RequestIdentity(owner=False, agent_id="va-0123", agent_name="Visitor", granter="user:8c0f")
    body = {"client_id": "c1", "input_windows": [{"window_id": "win-0123456789abcdef", "app": "files"}]}
    assert _heartbeat(client, agent, body).status_code == 204
    assert client.get("/api/presence").get_json() == {"users": []}
    assert state_of(app).shell.activity.read_events() == []


def test_presence_heartbeat_keeps_the_pages_newest_input_time(client: FlaskClient) -> None:
    _heartbeat(client, _VISITOR_IDENTITY, {"last_input_at": "2026-09-19T10:00:20Z"})
    _heartbeat(client, _VISITOR_IDENTITY, {"last_input_at": "2026-09-19T10:00:05+00:00"})

    (user,) = client.get("/api/presence").get_json()["users"]
    assert user["last_input_at"] == "2026-09-19T10:00:20.000000000Z"
    assert _heartbeat(client, _VISITOR_IDENTITY, {"last_input_at": "yesterday"}).status_code == 400
    assert _heartbeat(client, _VISITOR_IDENTITY, {"input_age_seconds": -1}).status_code == 400
    # An age the shell could not stamp (the request's JSON parser admits Infinity) is refused, never a 500.
    for unstampable in (1e300, float("inf")):
        refused = _heartbeat(client, _VISITOR_IDENTITY, {"input_age_seconds": unstampable})
        assert refused.status_code == 400
        assert "input_age_seconds" in refused.get_json()["detail"]


def test_presence_heartbeat_stamps_an_input_age_on_the_shells_clock(client: FlaskClient) -> None:
    before = utc_now()
    _heartbeat(client, _VISITOR_IDENTITY, {"input_age_seconds": 90})
    after = utc_now()

    (user,) = client.get("/api/presence").get_json()["users"]
    stamped = datetime.fromisoformat(user["last_input_at"][:26] + "+00:00")
    assert before - timedelta(seconds=90) <= stamped <= after - timedelta(seconds=90)


def test_a_heartbeat_that_moves_the_input_time_announces_the_roster_and_one_that_does_not_is_silent(
    app: Flask,
) -> None:
    client = app.test_client()
    broadcaster = state_of(app).shell.broadcaster
    client_queue = broadcaster.register()
    try:
        _heartbeat(client, _OWNER_IDENTITY, {"input_age_seconds": 60})
        drain_messages(client_queue)

        _heartbeat(client, _OWNER_IDENTITY, {"input_age_seconds": 5})
        (announced,) = drain_messages(client_queue)
        assert announced["type"] == "roster_updated"
        (owner,) = [entry for entry in announced["participants"] if entry["key"] == "user:user-owner-9c21"]
        (listed,) = client.get("/api/presence").get_json()["users"]
        assert owner["last_input_at"] == listed["last_input_at"]

        # An older age, and no input at all: the stored time stands, and nothing is pushed.
        _heartbeat(client, _OWNER_IDENTITY, {"input_age_seconds": 120})
        _heartbeat(client, _OWNER_IDENTITY)
        assert drain_messages(client_queue) == []
    finally:
        broadcaster.unregister(client_queue)


def test_the_leave_route_is_gone(client: FlaskClient) -> None:
    # Only the SPA catch-all (a GET) is left under the old path, so an older page's beacon is refused.
    assert client.post("/api/presence/leave", headers=identity_headers(_VISITOR_IDENTITY)).status_code == 405


def test_presence_carries_each_users_profile_from_the_connector(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    def connector(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/users/user-bob-4471/profile":
            return httpx.Response(
                200, json={"user_id": "user-bob-4471", "display_name": "Bob", "profile_picture_url": "https://a/b"}
            )
        return httpx.Response(404, json={"detail": "no such user"})

    app = _profile_aware_app(tmp_path, broadcaster, connector)
    client = app.test_client()
    client_queue = broadcaster.register()
    try:
        _heartbeat(client, _VISITOR_IDENTITY)
        _heartbeat(client, _OWNER_IDENTITY)
        # The heartbeats answer without waiting on the connector; the fetch behind them announces the connected
        # set again once Bob's profile lands.
        presence_messages: list[dict[str, Any]] = []

        def is_bob_named() -> bool:
            presence_messages.extend(_presence_messages(drain_messages(client_queue)))
            return any(user["display_name"] == "Bob" for message in presence_messages for user in message["users"])

        wait_for(is_bob_named, timeout=10.0, poll_interval=0.01, error_message="Bob's profile was never announced")
        assert [
            (user["user_id"], user["display_name"], user["profile_picture_url"])
            for user in presence_messages[-1]["users"]
        ] == [
            ("user-bob-4471", "Bob", "https://a/b"),
            ("user-owner-9c21", None, None),
        ]
        listed = client.get("/api/presence").get_json()["users"]
        assert [(user["user_id"], user["display_name"]) for user in listed] == [
            ("user-bob-4471", "Bob"),
            ("user-owner-9c21", None),
        ]
    finally:
        broadcaster.unregister(client_queue)


def test_the_sweep_broadcasts_a_silent_departure_and_a_heartbeat_that_changes_nothing_is_silent(app: Flask) -> None:
    client = app.test_client()
    state = state_of(app)
    client_queue = state.shell.broadcaster.register()
    try:
        _heartbeat(client, _VISITOR_IDENTITY)
        _heartbeat(client, _OWNER_IDENTITY)
        joined = _presence_messages(drain_messages(client_queue))
        assert len(joined) == 2
        assert [user["user_id"] for user in joined[1]["users"]] == ["user-bob-4471", "user-owner-9c21"]
        # A heartbeat that changes nothing about who is here broadcasts nothing.
        _heartbeat(client, _OWNER_IDENTITY)
        assert drain_messages(client_queue) == []

        # Bob's tab closed without a word: his file's mtime falls behind, and the next sweep notices.
        bob_path = state.presence.users_directory / "user-bob-4471.json"
        stale_at = utc_now() - PRESENCE_CONNECTED_WINDOW - timedelta(seconds=1)
        os.utime(bob_path, (stale_at.timestamp(), stale_at.timestamp()))
        assert state.presence_sweep.sweep_once(utc_now()) is True

        (departed,) = _presence_messages(drain_messages(client_queue))
        assert [user["user_id"] for user in departed["users"]] == ["user-owner-9c21"]
        assert bob_path.exists()
        assert state.presence_sweep.sweep_once(utc_now()) is False
    finally:
        state.shell.broadcaster.unregister(client_queue)


@pytest.mark.flaky
@pytest.mark.timeout(15)
def test_websocket_connect_sends_the_connected_users(app: Flask) -> None:
    client = app.test_client()
    _heartbeat(client, _VISITOR_IDENTITY)
    with serve_app(app) as served:
        ws = open_ws(served, "/api/ws")
        try:
            messages = [json.loads(ws.receive(timeout=_WS_RECEIVE_TIMEOUT)) for _ in range(5)]
        finally:
            close_ws(ws)

    # The connected users follow the apps, desktops, avatar status, and update notice.
    assert messages[4]["type"] == "presence_updated"
    assert [user["user_id"] for user in messages[4]["users"]] == ["user-bob-4471"]


@pytest.mark.flaky
@pytest.mark.timeout(15)
def test_a_windows_registration_and_its_disconnect_each_broadcast_clients_updated_and_an_unregistered_close_does_not(
    app: Flask,
) -> None:
    broadcaster = state_of(app).shell.broadcaster
    observer_queue = broadcaster.register()
    try:
        with serve_app(app) as served:
            unregistered = open_ws(served, "/api/ws")
            close_ws(unregistered)
            registered = open_ws(served, "/api/ws")
            registered.send(json.dumps(_moving_report("c-leaving", "home")))
            wait_for(
                lambda: "c-leaving" in broadcaster.connected_client_ids(),
                timeout=5.0,
                poll_interval=0.05,
                error_message="the window never registered",
            )
            wait_for(
                lambda: not observer_queue.empty(),
                timeout=5.0,
                poll_interval=0.05,
                error_message="the window's registration was never announced",
            )
            # The registration is announced once (beside the move its first report made); the unregistered
            # socket's close, handled by now, announced nothing.
            assert [message["type"] for message in drain_messages(observer_queue)] == [
                "active_desktop_changed",
                "clients_updated",
            ]
            close_ws(registered)
            # The broadcast follows the unregister, so the message is what is waited for, not the client's leaving.
            wait_for(
                lambda: not observer_queue.empty(),
                timeout=5.0,
                poll_interval=0.05,
                error_message="the window's disconnect was never announced",
            )
        assert [message["type"] for message in drain_messages(observer_queue)] == ["clients_updated"]
    finally:
        broadcaster.unregister(observer_queue)


def test_pruning_a_stale_client_broadcasts_clients_updated_and_pruning_none_is_silent(app: Flask) -> None:
    shell = state_of(app).shell
    long_ago = utc_now() - CLIENT_RETENTION - timedelta(days=1)
    shell.clients.record_report(client_report("c-stale", "home"), long_ago, is_redirected=False)
    client_queue = shell.broadcaster.register()
    try:
        shell.prune_unseen_clients()
        assert [message["type"] for message in drain_messages(client_queue)] == ["clients_updated"]
        shell.prune_unseen_clients()
        assert drain_messages(client_queue) == []
    finally:
        shell.broadcaster.unregister(client_queue)


def test_a_client_state_report_survives_an_unwritable_state_file(app: Flask) -> None:
    """The live registration is what the layout ops need; a state file the shell cannot write is logged, not fatal."""
    shell = state_of(app).shell
    (shell.state_directory / "clients.json").mkdir(parents=True)
    (shell.activity.events_path).mkdir(parents=True)
    client_queue = shell.broadcaster.register()
    try:
        report = _moving_report("c1", "home")
        assert _handle_client_state_message(json.dumps(report), client_queue, shell, is_first_report=True) is True
        switched = {**report, "active_desktop": "alpha", "previous_desktop": "home"}
        assert _handle_client_state_message(json.dumps(switched), client_queue, shell, is_first_report=False) is True
        assert shell.broadcaster.get_client_info(client_queue) == ConnectionRegistration(
            client_id="c1", active_desktop="alpha", is_pop_out=False
        )
    finally:
        shell.broadcaster.unregister(client_queue)


def test_client_state_reports_register_the_client_and_log_only_real_desktop_switches(app: Flask) -> None:
    """A report registers the connection with the broadcaster and records the client; a desktop_switch is
    logged only when the report names a previous desktop that differs; anything malformed is ignored."""
    shell = state_of(app).shell
    client_queue = shell.broadcaster.register()
    try:
        first = json.dumps(_moving_report("c1", "home"))
        assert _handle_client_state_message(first, client_queue, shell, is_first_report=True) is True
        assert shell.broadcaster.get_client_info(client_queue) == ConnectionRegistration(
            client_id="c1", active_desktop="home", is_pop_out=False
        )
        recorded = shell.clients.get_client("c1")
        assert recorded is not None
        assert recorded.active_desktop == "home"
        assert shell.activity.read_events() == []

        switched = json.dumps({**_moving_report("c1", "home"), "previous_desktop": "research"})
        assert _handle_client_state_message(switched, client_queue, shell, is_first_report=False) is True
        unchanged = json.dumps({**_moving_report("c1", "home"), "previous_desktop": "home"})
        assert _handle_client_state_message(unchanged, client_queue, shell, is_first_report=False) is True
        events = shell.activity.read_events()
        assert [(event["type"], event["from_desktop_id"], event["to_desktop_id"]) for event in events] == [
            ("desktop_switch", "research", "home")
        ]

        unordered = {key: value for key, value in _moving_report("c1", "home").items() if key != "revision"}
        for malformed in (
            "{",
            json.dumps({"type": "other"}),
            json.dumps({"type": "client_state", "client_id": "c1"}),
            json.dumps(unordered),
            json.dumps({**unordered, "is_following": True, "revision": 0}),
        ):
            assert _handle_client_state_message(malformed, client_queue, shell, is_first_report=False) is False
        assert shell.broadcaster.get_client_info(client_queue) == ConnectionRegistration(
            client_id="c1", active_desktop="home", is_pop_out=False
        )
    finally:
        shell.broadcaster.unregister(client_queue)


def test_a_pop_out_report_registers_its_connection_and_touches_neither_the_record_nor_the_log(app: Flask) -> None:
    """A pop-out's report makes its connection reachable by ops aimed at its client, and leaves the client's record,
    active desktop, and desktop switches to its main window."""
    shell = state_of(app).shell
    main_queue = shell.broadcaster.register()
    pop_out_queue = shell.broadcaster.register()
    try:
        main_report = json.dumps(_moving_report("c1", "home"))
        assert _handle_client_state_message(main_report, main_queue, shell, is_first_report=True) is True
        pop_out_report = json.dumps({"type": "client_state", "client_id": "c1", "is_pop_out": True})
        assert _handle_client_state_message(pop_out_report, pop_out_queue, shell, is_first_report=True) is True
        assert _handle_client_state_message(pop_out_report, pop_out_queue, shell, is_first_report=False) is True

        assert shell.broadcaster.get_client_info(pop_out_queue) == ConnectionRegistration(
            client_id="c1", active_desktop="", is_pop_out=True
        )
        recorded = shell.clients.get_client("c1")
        assert recorded is not None and recorded.active_desktop == "home"
        assert shell.activity.read_events() == []

        # A pop-out report carrying a desktop is not a pop-out's.
        with_desktop = json.dumps(
            {"type": "client_state", "client_id": "c1", "is_pop_out": True, "active_desktop": "x"}
        )
        assert _handle_client_state_message(with_desktop, pop_out_queue, shell, is_first_report=False) is False
    finally:
        shell.broadcaster.unregister(main_queue)
        shell.broadcaster.unregister(pop_out_queue)


@pytest.mark.frontend
def test_not_built_page_coordinate_regex_matches_the_canonical_one() -> None:
    """The placeholder derives a service origin, so it carries a copy of the rule.

    The shared library's ``app_contract.ts`` is canonical. The placeholder cannot import
    it -- it runs in the browser, in the one state where the bundle it lives in is
    missing -- so it holds its own copy, and this pins that copy to the source of
    truth. Without it the rule can be corrected in one place and silently rot in the
    page that only renders when everything else is broken.
    """
    app_contract_ts = Path(__file__).parents[4] / "libs" / "workspace_ui" / "src" / "app_contract.ts"
    canonical = re.search(r"WORKSPACE_COORDINATE_LABEL = (/.+/i);", app_contract_ts.read_text())
    assert canonical is not None, f"the canonical regex is no longer declared in {app_contract_ts}"

    page = render_recovery_page(INTERFACE_NOT_BUILT_PAGE, "terminal-x7k9q2w1", True, "")
    in_page = re.findall(r"(/\^\(\?:.+?/i)\.test\(", page)
    assert in_page == [canonical.group(1)], (
        f"the placeholder's coordinate regex has drifted from {app_contract_ts}: "
        f"page has {in_page}, app_contract.ts has {canonical.group(1)!r}"
    )


@pytest.mark.frontend
def test_the_tours_pictures_are_hosted_at_the_origin_the_page_policy_admits() -> None:
    """The shell page admits images from ``ONBOARDING_MEDIA_ORIGIN`` alone beyond its own origin, and the tour's
    cards hold their pictures' origin as a constant of their own, since the bundle cannot read ``tour.py``; a
    change to one and not the other would block the pictures with nothing but a blank card to show for it."""
    tour_cards_ts = Path(__file__).parents[2] / "frontend" / "src" / "model" / "tourCards.ts"
    source = tour_cards_ts.read_text()
    declared = re.search(r'const ONBOARDING_MEDIA_BASE_URL = "([^"]+)";', source)
    assert declared is not None, f"the pictures' origin is no longer declared in {tour_cards_ts}"
    assert declared.group(1) == ONBOARDING_MEDIA_ORIGIN, (
        f"the tour's picture origin in {tour_cards_ts} has drifted from tour.py: "
        f"{declared.group(1)!r} against {ONBOARDING_MEDIA_ORIGIN!r}"
    )
    picture_urls = re.findall(r"url: `([^`]+)`", source)
    assert picture_urls, f"no picture URLs found in {tour_cards_ts}"
    assert all(url.startswith("${ONBOARDING_MEDIA_BASE_URL}/") for url in picture_urls), picture_urls


def test_not_built_placeholder_answers_its_own_poll_cheaply(tmp_path: Path) -> None:
    """The poll reads a header, so HEAD must still carry it -- and nothing else.

    This is the page's only route back to the interface, so a HEAD that stopped
    reporting the marker would strand every open tab until someone reloaded by
    hand. It is also the request the page makes every ten seconds per tab for
    the length of an outage, so it must not re-render the page or re-read the
    app registry to answer.
    """
    empty_dir = tmp_path / "static"
    empty_dir.mkdir()

    state = build_test_state()
    state.static_directory = empty_dir
    test_client = create_application(state).test_client()
    head = test_client.head("/")
    get = test_client.get("/")

    assert head.headers[FRONTEND_BUILT_HEADER] == "false"
    assert get.headers[FRONTEND_BUILT_HEADER] == "false"
    # The GET is the one that renders; the HEAD carries no page to render.
    assert "needs to be rebuilt" in get.text
    assert head.text == ""


def test_a_report_of_a_deleted_desktop_lands_the_client_on_the_first_one_and_says_so_once(app: Flask) -> None:
    """A report naming a desktop that no longer exists lands the client on the first one, which its window is
    told once, and the switch is logged as the client reported it."""
    shell = state_of(app).shell
    shell.inventory.reload_registry()
    shell.list_desktops()
    client_queue = shell.broadcaster.register()
    try:
        first = json.dumps(_moving_report("c1", "home"))
        assert _handle_client_state_message(first, client_queue, shell, is_first_report=True) is True
        assert [message["type"] for message in drain_messages(client_queue)] == ["active_desktop_changed"]

        stale = json.dumps({**_moving_report("c1", "gone"), "previous_desktop": "home"})
        assert _handle_client_state_message(stale, client_queue, shell, is_first_report=False) is True
        landed = shell.clients.get_client("c1")
        assert landed is not None and landed.active_desktop == "home"
        events = shell.activity.read_events()
        assert [(event["type"], event["to_desktop_id"]) for event in events] == [("desktop_switch", "gone")]
        # The redirected window is told where it landed, once, at a revision newer than the one it already heard (the
        # stored desktop did not move); a report of that desktop then changes nothing.
        assert [
            (message["desktop_id"], message["revision"])
            for message in drain_messages(client_queue)
            if message["type"] == "active_desktop_changed"
        ] == [("home", 2)]
        settled = json.dumps(_moving_report("c1", "home"))
        assert _handle_client_state_message(settled, client_queue, shell, is_first_report=False) is True
        assert drain_messages(client_queue) == []
    finally:
        shell.broadcaster.unregister(client_queue)


def test_a_move_is_echoed_with_its_report_and_revision_and_a_following_report_moves_nothing(app: Flask) -> None:
    """A window's move is broadcast naming the report that made it and the revision it was written at. A window
    that followed a push reports the desktop it followed, which a later move may have replaced: that report
    registers its connection there and stamps the client as seen, and neither moves the client back nor broadcasts."""
    shell = state_of(app).shell
    shell.inventory.reload_registry()
    shell.list_desktops()
    work = shell.create_desktop("Work", "#123456", 1, ANONYMOUS_OWNER)
    client_queue = shell.broadcaster.register()
    try:
        first = json.dumps(_moving_report("c1", "home"))
        assert _handle_client_state_message(first, client_queue, shell, is_first_report=True) is True
        drain_messages(client_queue)
        move_body = {
            "type": "client_state",
            "client_id": "c1",
            "active_desktop": str(work.id),
            "previous_desktop": "home",
            "report_id": "report-0123456789abcdef",
            "page_id": TEST_PAGE_ID,
            "revision": 0,
        }
        unminted = json.dumps({**move_body, "report_id": "not-a-report-id"})
        assert _handle_client_state_message(unminted, client_queue, shell, is_first_report=False) is False
        assert drain_messages(client_queue) == []
        move = json.dumps(move_body)
        assert _handle_client_state_message(move, client_queue, shell, is_first_report=False) is True
        assert drain_messages(client_queue) == [
            {
                "type": "active_desktop_changed",
                "client_id": "c1",
                "desktop_id": str(work.id),
                "revision": 2,
                "report_id": "report-0123456789abcdef",
            }
        ]

        switches_logged = len(shell.activity.read_events())
        long_ago = utc_now() - timedelta(days=30)
        shell.clients.set_active_desktop(ClientId("c1"), work.id, long_ago)
        following = json.dumps(
            {
                "type": "client_state",
                "client_id": "c1",
                "active_desktop": "home",
                "previous_desktop": str(work.id),
                "is_following": True,
                "page_id": TEST_PAGE_ID,
            }
        )
        assert _handle_client_state_message(following, client_queue, shell, is_first_report=False) is True
        recorded = shell.clients.get_client("c1")
        assert recorded is not None and (recorded.active_desktop, recorded.desktop_revision) == (work.id, 2)
        assert recorded.last_seen > long_ago
        assert len(shell.activity.read_events()) == switches_logged
        assert drain_messages(client_queue) == []
        assert [info.active_desktop for info in shell.broadcaster.get_connected_client_infos()] == ["home"]

        # A client the shell has no record of (pruned or reset under an open page) still registers its connection.
        unrecorded = json.dumps(
            {
                "type": "client_state",
                "client_id": "c2",
                "active_desktop": "home",
                "is_following": True,
                "page_id": TEST_PAGE_ID,
            }
        )
        assert _handle_client_state_message(unrecorded, client_queue, shell, is_first_report=False) is True
        registered = shell.broadcaster.get_client_info(client_queue)
        assert registered is not None and (registered.client_id, registered.active_desktop) == ("c2", "home")
        assert shell.clients.get_client("c2") is None
        assert drain_messages(client_queue) == []
    finally:
        shell.broadcaster.unregister(client_queue)


def test_a_move_reported_before_its_page_heard_an_ops_move_is_not_recorded_and_its_window_is_told_the_op(
    app: Flask,
) -> None:
    """An op moves the client while a page's report, made at the revision before the op's, is still on its way: the
    report is not recorded, broadcast, or logged as a switch, so the op's move stands, and the reporting window alone is
    told the op's move again, which it follows. A report made at the op's revision moves the client as usual."""
    shell = state_of(app).shell
    shell.inventory.reload_registry()
    shell.list_desktops()
    work = shell.create_desktop("Work", "#123456", 1, ANONYMOUS_OWNER)
    client_queue = shell.broadcaster.register()
    other_queue = shell.broadcaster.register()
    shell.broadcaster.set_client_info(other_queue, "c1", "home")
    try:
        landing = {**_moving_report("c1", "home"), "report_id": "report-0000000000000001"}
        assert _handle_client_state_message(json.dumps(landing), client_queue, shell, is_first_report=True) is True
        assert shell.set_client_active_desktop(ClientId("c1"), work.id) is True
        drain_messages(client_queue)
        drain_messages(other_queue)
        switches_logged = len(shell.activity.read_events())

        made_before_the_op = {
            **landing,
            "active_desktop": "home",
            "previous_desktop": str(work.id),
            "report_id": "report-0000000000000002",
            "revision": 1,
        }
        assert (
            _handle_client_state_message(json.dumps(made_before_the_op), client_queue, shell, is_first_report=False)
            is True
        )
        recorded = shell.clients.get_client("c1")
        assert recorded is not None and (recorded.active_desktop, recorded.desktop_revision) == (work.id, 2)
        assert drain_messages(client_queue) == [
            {
                "type": "active_desktop_changed",
                "client_id": "c1",
                "desktop_id": str(work.id),
                "revision": 2,
                "report_id": None,
            }
        ]
        assert drain_messages(other_queue) == []
        assert len(shell.activity.read_events()) == switches_logged

        # One naming a desktop since deleted is not landed on the first desktop and announced either.
        deleted_before_the_op = {
            **made_before_the_op,
            "active_desktop": "gone",
            "report_id": "report-00000000000000a2",
        }
        assert (
            _handle_client_state_message(json.dumps(deleted_before_the_op), client_queue, shell, is_first_report=False)
            is True
        )
        recorded = shell.clients.get_client("c1")
        assert recorded is not None and (recorded.active_desktop, recorded.desktop_revision) == (work.id, 2)
        assert [(message["desktop_id"], message["report_id"]) for message in drain_messages(client_queue)] == [
            (str(work.id), None)
        ]
        assert drain_messages(other_queue) == []

        made_after_the_op = {**made_before_the_op, "report_id": "report-0000000000000003", "revision": 2}
        assert (
            _handle_client_state_message(json.dumps(made_after_the_op), client_queue, shell, is_first_report=False)
            is True
        )
        assert [
            (message["desktop_id"], message["revision"], message["report_id"])
            for message in drain_messages(client_queue)
            if message["type"] == "active_desktop_changed"
        ] == [("home", 3, "report-0000000000000003")]
        assert len(shell.activity.read_events()) == switches_logged + 1
    finally:
        shell.broadcaster.unregister(client_queue)
        shell.broadcaster.unregister(other_queue)


def test_the_shell_describes_its_api_the_way_its_manifest_says(client: FlaskClient) -> None:
    assert_api_schema_describes_app(client, Path(__file__).resolve().parents[2] / "app.toml")
