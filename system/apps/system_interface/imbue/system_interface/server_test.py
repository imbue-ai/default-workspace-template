"""Tests for the Flask server."""

import html
import json
import os
import re
import subprocess
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from flask import Flask
from flask.testing import FlaskClient
from workspace_layout.primitives import ClientId

from imbue.system_interface.app_context import state_of
from imbue.system_interface.avatar.primitives import DesignId
from imbue.system_interface.avatar.testing import png_size
from imbue.system_interface.config import Config
from imbue.system_interface.documents import FRONTEND_BUILT_HEADER
from imbue.system_interface.presence import PRESENCE_CONNECTED_WINDOW
from imbue.system_interface.presence import utc_now
from imbue.system_interface.profiles import ProfileResolver
from imbue.system_interface.server import _NOT_BUILT_REPAIR_ARGV
from imbue.system_interface.server import _NOT_BUILT_REPAIR_COMMAND
from imbue.system_interface.server import _NOT_BUILT_REPAIR_MNGR_COMMAND
from imbue.system_interface.server import _handle_client_state_message
from imbue.system_interface.server import create_application
from imbue.system_interface.server import render_frontend_not_built_page
from imbue.system_interface.shell.identity import RequestIdentity
from imbue.system_interface.shell.testing import TEST_PAGE_ID
from imbue.system_interface.shell.testing import drain_messages
from imbue.system_interface.shell.testing import identity_headers
from imbue.system_interface.testing import build_test_state
from imbue.system_interface.testing import close_ws
from imbue.system_interface.testing import open_ws
from imbue.system_interface.testing import serve_app
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
    assert '<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">' in page
    assert page.count('name="viewport"') == 1
    assert '<link rel="apple-touch-icon" href="/apple-touch-icon.png">' in page
    assert '<link rel="manifest" href="/manifest.webmanifest" crossorigin="use-credentials">' in page
    assert '<link rel="apple-touch-icon" href="/shell/apple-touch-icon.png">' in prefixed
    assert '<link rel="manifest" href="/shell/manifest.webmanifest" crossorigin="use-credentials">' in prefixed
    assert client.get("/api/inventory").get_json()["workspace_name"] == 'Tom & Jerry\'s <"Lab">'


def test_tags_the_build_already_carries_are_replaced_rather_than_repeated(tmp_path: Path) -> None:
    built = (
        "<html><head>"
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
        '<meta name="theme-color" content="#000000">'
        '<link rel="manifest" href="/stale.webmanifest">'
        "</head><body></body></html>"
    )
    page = _named_workspace_app(tmp_path, built, "Lab").test_client().get("/").text

    assert page.count('name="viewport"') == 1
    assert "width=device-width, initial-scale=1, viewport-fit=cover" in page
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
    assert _NOT_BUILT_REPAIR_COMMAND == "env -u TMUX " + _NOT_BUILT_REPAIR_MNGR_COMMAND


def test_not_built_repair_message_quotes_the_heading_the_reader_is_looking_at() -> None:
    """What the message quotes has to be what the page says, or it quotes nothing.

    The message's whole claim on the agent's attention is that it repeats the
    line the reader is looking at, so the two are one statement written twice.
    Nothing else notices when they part: reword the heading and the message still
    parses, still validates against the CLI, and still reads as a quotation --
    of a sentence that now appears nowhere. The comparison is case-insensitive
    because the message is in the reader's voice and the heading is a title.
    """
    message = _NOT_BUILT_REPAIR_ARGV[_NOT_BUILT_REPAIR_ARGV.index("--message") + 1]
    quoted = re.search(r'"(.*?)[,.?!]?"', message)
    assert quoted is not None, f"the message no longer quotes anything: {message}"

    heading = re.search(r"<h1>(.*?)</h1>", render_frontend_not_built_page(None), re.DOTALL)
    assert heading is not None, "the page no longer carries a heading"
    assert quoted.group(1).lower().startswith(heading.group(1).strip().lower())


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
    with patch("imbue.system_interface.server._NOT_BUILT_REPAIR_COMMAND", 'mngr create --message "a & b <c>"'):
        page = render_frontend_not_built_page(None)

    assert 'mngr create --message "a &amp; b &lt;c&gt;"' in page
    assert "<c>" not in page

    # And the escaping has to be transparent to the line that ships: undoing it
    # is what the browser does to fill ``textContent``, so this is the line the
    # reader reads and copies, and it has to be the one the CLI check and the
    # shell split validated. The assertions above only show that escaping
    # happens; this is what says the real command survives it.
    shown = _repair_line_shown_on(render_frontend_not_built_page(None))
    assert shown == _NOT_BUILT_REPAIR_COMMAND


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
        assert substitution not in _NOT_BUILT_REPAIR_MNGR_COMMAND, (
            f"the suggested line contains a command substitution ({substitution}), which the shell below "
            "would execute rather than report: word it out of the message"
        )

    printed_words = subprocess.run(
        ["sh", "-c", f'set -- {_NOT_BUILT_REPAIR_MNGR_COMMAND}\nprintf "%s\\n" "$@"'],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert printed_words.stdout.splitlines() == list(_NOT_BUILT_REPAIR_ARGV)


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


_VISITOR_IDENTITY = RequestIdentity(owner=False, user_id="user-bob-4471", email="bob@example.com")
_OWNER_IDENTITY = RequestIdentity(owner=True, user_id="user-owner-9c21", email="owner@example.com")


def _heartbeat(client: FlaskClient, identity: RequestIdentity | None) -> Any:
    headers = {} if identity is None else identity_headers(identity)
    return client.post("/api/presence/heartbeat", headers=headers)


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


def test_presence_heartbeat_records_the_requester_and_answers_their_identity(client: FlaskClient) -> None:
    response = _heartbeat(client, _VISITOR_IDENTITY)

    assert response.status_code == 200
    assert response.get_json() == {
        "identity": {"owner": False, "user_id": "user-bob-4471", "email": "bob@example.com"}
    }
    listed = client.get("/api/presence").get_json()
    (user,) = listed["users"]
    assert user["user_id"] == "user-bob-4471"
    assert user["owner"] is False
    # No connector to ask, so no profile; the record itself is complete without one.
    assert user["display_name"] is None and user["profile_picture_url"] is None
    assert set(user) == {"user_id", "email", "display_name", "profile_picture_url", "owner", "first_seen", "last_seen"}


def test_presence_heartbeat_ignores_whatever_body_an_older_page_still_sends(client: FlaskClient) -> None:
    response = client.post(
        "/api/presence/heartbeat", json={"session_id": "tab-0001-aaaa"}, headers=identity_headers(_VISITOR_IDENTITY)
    )
    assert response.status_code == 200


def test_presence_heartbeat_records_nothing_for_an_identity_without_a_user_id(client: FlaskClient) -> None:
    assert _heartbeat(client, None).status_code == 204
    assert _heartbeat(client, RequestIdentity(owner=True)).status_code == 204
    assert client.get("/api/presence").get_json() == {"users": []}


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
        (joined_bob, joined_owner) = drain_messages(client_queue)
        assert joined_bob["type"] == joined_owner["type"] == "presence_updated"
        assert [
            (user["user_id"], user["display_name"], user["profile_picture_url"]) for user in joined_owner["users"]
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
        joined = drain_messages(client_queue)
        assert [message["type"] for message in joined] == ["presence_updated", "presence_updated"]
        assert [user["user_id"] for user in joined[1]["users"]] == ["user-bob-4471", "user-owner-9c21"]
        # A heartbeat that changes nothing about who is here broadcasts nothing.
        _heartbeat(client, _OWNER_IDENTITY)
        assert drain_messages(client_queue) == []

        # Bob's tab closed without a word: his file's mtime falls behind, and the next sweep notices.
        bob_path = state.presence.users_directory / "user-bob-4471.json"
        stale_at = utc_now() - PRESENCE_CONNECTED_WINDOW - timedelta(seconds=1)
        os.utime(bob_path, (stale_at.timestamp(), stale_at.timestamp()))
        assert state.presence_sweep.sweep_once(utc_now()) is True

        (departed,) = drain_messages(client_queue)
        assert departed["type"] == "presence_updated"
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

    The shared library's ``origin.ts`` is canonical. The placeholder cannot import
    it -- it runs in the browser, in the one state where the bundle it lives in is
    missing -- so it holds its own copy, and this pins that copy to the source of
    truth. Without it the rule can be corrected in one place and silently rot in the
    page that only renders when everything else is broken.
    """
    origin_ts = Path(__file__).parents[4] / "libs" / "workspace_ui" / "src" / "origin.ts"
    canonical = re.search(r"WORKSPACE_COORDINATE_LABEL = (/.+/i);", origin_ts.read_text())
    assert canonical is not None, f"the canonical regex is no longer declared in {origin_ts}"

    page = render_frontend_not_built_page("terminal-x7k9q2w1")
    in_page = re.findall(r"(/\^\(\?:.+?/i)\.test\(", page)
    assert in_page == [canonical.group(1)], (
        f"the placeholder's coordinate regex has drifted from {origin_ts}: "
        f"page has {in_page}, origin.ts has {canonical.group(1)!r}"
    )


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
    work = shell.create_desktop("Work", "#123456", 1)
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
    work = shell.create_desktop("Work", "#123456", 1)
    client_queue = shell.broadcaster.register()
    other_queue = shell.broadcaster.register()
    shell.broadcaster.set_client_info(other_queue, "c1", "home")
    try:
        landing = {
            "type": "client_state",
            "client_id": "c1",
            "active_desktop": "home",
            "report_id": "report-0000000000000001",
            "page_id": "page-00000000000000aa",
            "revision": 0,
        }
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
